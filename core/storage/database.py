"""数据库连接管理 — 支持 SQLite + PostgreSQL 双后端

生产环境推荐 PostgreSQL（连接池 + 并发写入），
开发/单机环境默认 SQLite（零配置）。

切换方式：
    POSTGRES_ENABLED=true python main.py

SQLite 模式说明:
    使用 WAL journal_mode（读写并发，崩溃安全）。
    同步级别设为 NORMAL，wal_autocheckpoint=1000 自动 checkpoint 避免 WAL 文件过大。
    注：早期用 DELETE mode 但 macOS 系统 SQLite 3.9.6 在高并发 SSE 流下触发
    sqlite3WhereBegin/sqlite3Select 内存访问错误（EXC_BAD_ACCESS），切换 WAL 后稳定。
"""

from __future__ import annotations
import asyncio
import time
from typing import List, Optional, Union
import aiosqlite
from pathlib import Path
from config import DB_PATH, POSTGRES_ENABLED

_logger = __import__("logging").getLogger(__name__)


# SQLite 连接（写单例 + 读连接池）
# DELETE 模式下读写均走单写连接（aiosqlite 协程串行）。原独立读池已废弃（P1-1 泄漏修复）
_db: Optional[aiosqlite.Connection] = None
_read_pool: List[aiosqlite.Connection] = []
_read_pool_lock = asyncio.Lock()
_READ_POOL_SIZE = 3
_active_read_conns: set = set()  # 跟踪活跃的读连接（用于 close_db 统一清理）
_db_loop_id: Optional[int] = None
_db_last_health_check: float = 0.0
# 连接创建/重建互斥锁（防并发 get_db 时多 task 同时重建连接产生竞态）
_conn_lock: "Optional[asyncio.Lock]" = None


def _get_conn_lock() -> "asyncio.Lock":
    """懒初始化连接锁（asyncio.Lock 需在事件循环内创建）"""
    global _conn_lock
    if _conn_lock is None:
        _conn_lock = asyncio.Lock()
    return _conn_lock

# PostgreSQL 连接
_pg_conn = None  # 延迟导入

# ── SQLite 优化 PRAGMA ───────────────────────────────────
# 这些设置在每次创建连接时自动应用
SQLITE_PRAGMA = [
    "PRAGMA journal_mode=WAL",         # WAL 模式：读写并发，崩溃安全（修复 DELETE 模式 sqlite3 内存崩溃）
    "PRAGMA foreign_keys=ON",          # 外键约束
    "PRAGMA busy_timeout=30000",       # 30 秒忙等待
    "PRAGMA cache_size=-8000",         # 8MB 页面缓存
    "PRAGMA temp_store=MEMORY",        # 临时表放内存
    "PRAGMA wal_autocheckpoint=1000",  # 自动 checkpoint（避免 WAL 文件膨胀）
    # 注意：synchronous=NORMAL 在事务中设置会报错 "cannot change synchronous within a transaction"
    # WAL mode 下 synchronous 默认就是 NORMAL，不需要显式设置
]


async def _apply_pragma(conn: aiosqlite.Connection) -> None:
    """对连接应用优化 PRAGMA —— journal_mode 失败必须中断，其余降级为 warning"""
    for pragma in SQLITE_PRAGMA:
        try:
            await conn.execute(pragma)
        except Exception as e:
            if "journal_mode" in pragma:
                # journal_mode 失败意味着 SQLite 不可用，必须中断
                _logger.error("PRAGMA journal_mode 失败（致命）[%s]: %s", pragma, e)
                raise
            _logger.warning("PRAGMA 执行失败 [%s]: %s", pragma, e)


async def get_db():
    """获取数据库连接（读写）

    Returns:
        SQLite: aiosqlite.Connection
        PostgreSQL: PostgresConnection（兼容接口）
    """
    if POSTGRES_ENABLED:
        return await _get_postgres_conn()
    return await _get_sqlite_conn()


async def get_read_db():
    """只读连接 — API 查询路径使用

    P1-1 修复：改走主写连接（_get_sqlite_conn）。原独立读池在 DELETE 模式下无意义
    且 _release_sqlite_read_conn 全仓无人调用导致连接句柄永久泄漏（18 处调用 0 归还）。
    aiosqlite 单连接协程串行，DELETE 模式读写同连接安全。
    """
    if POSTGRES_ENABLED:
        return await _get_postgres_conn()
    return await _get_sqlite_conn()


async def get_read_db_dep():
    """FastAPI 依赖：获取只读连接并在请求结束后自动归还到连接池。

    用法：路由签名加 `db = Depends(get_read_db_dep)`
    解决 get_read_db() 连接不归还的句柄泄漏问题。
    """
    if POSTGRES_ENABLED:
        # PG 连接池自管理，直接 yield
        yield await _get_postgres_conn()
        return
    conn = await _get_sqlite_read_conn()
    try:
        yield conn
    finally:
        await _release_sqlite_read_conn(conn)


async def _reset_sqlite_on_loop_change(current_loop_id: int) -> None:
    global _db, _read_pool, _active_read_conns, _db_loop_id
    if _db_loop_id is None or _db_loop_id == current_loop_id:
        return
    for conn in _read_pool:
        try:
            await conn.close()
        except Exception:
            pass
    _read_pool.clear()
    for conn in _active_read_conns:
        try:
            await conn.close()
        except Exception:
            pass
    _active_read_conns.clear()
    if _db is not None:
        try:
            await _db.close()
        except Exception:
            pass
    _db = None
    _db_loop_id = None


async def _liveness_check(conn: Optional[aiosqlite.Connection]) -> bool:
    """异步探测连接存活（走 worker 线程，线程安全）。

    修复（2026-08-08）：旧实现同步调用底层 sqlite3 `raw.execute("SELECT 1")`，
    绕过 aiosqlite 的 worker 线程，与并发写冲突 → 抛异常 → 误判连接"断开" →
    _db 置 None → 大量并发 get_db 各自重建连接（无锁竞态）→ 连接重建风暴 +
    写入失败 + CPU 100% → event loop 卡死 → health 超时 → keeper 重启 run 中断。
    """
    if conn is None:
        return False
    try:
        cur = await conn.execute("SELECT 1")
        await cur.fetchone()
        return True
    except Exception:
        return False


async def _get_sqlite_conn() -> aiosqlite.Connection:
    """获取 SQLite 写连接（单例，带健康检查自动恢复）

    DELETE 模式下读写均串行（SQLite 数据库级锁 + 单连接协程队列），单连接即可。
    """
    global _db, _db_loop_id, _db_last_health_check

    current_loop_id = id(asyncio.get_running_loop())
    await _reset_sqlite_on_loop_change(current_loop_id)

    now = time.monotonic()
    if _db is not None and now - _db_last_health_check > 120:
        ok = await _liveness_check(_db)  # 异步安全探测（走 worker 线程）
        _db_last_health_check = now
        if not ok:
            _logger.warning("SQLite 写连接不可用，重建中…")
            try:
                await _db.close()
            except Exception:
                pass
            _db = None

    if _db is None:
        # 加锁 + 双重检查：防大量并发 get_db 时多 task 同时重建连接产生竞态
        async with _get_conn_lock():
            if _db is None:
                try:
                    _db = await aiosqlite.connect(str(DB_PATH), timeout=30.0)
                    _db.row_factory = aiosqlite.Row
                    await _apply_pragma(_db)
                except Exception:
                    _db = None
                    raise
                _db_loop_id = current_loop_id
                _db_last_health_check = now
                _jm = "WAL"
                try:
                    _cur = await _db.execute("PRAGMA journal_mode")
                    _row = await _cur.fetchone()
                    _jm = str(_row[0] if _row else "?")
                except Exception:
                    pass
                _logger.info("SQLite 写连接已建立 (journal_mode=%s)", _jm)
    return _db


async def _get_sqlite_read_conn() -> aiosqlite.Connection:
    """从读连接池获取只读连接（WAL 下允许多个读连接并发）

    注意：调用方应在使用完毕后调用 _release_sqlite_read_conn 归还连接。
    为防止泄漏，池满时新连接会被创建但池会自动回收空闲连接。
    """
    global _read_pool, _db_loop_id, _active_read_conns

    current_loop_id = id(asyncio.get_running_loop())
    await _reset_sqlite_on_loop_change(current_loop_id)

    async with _read_pool_lock:
        while _read_pool:
            conn = _read_pool.pop()
            if await _liveness_check(conn):
                _db_loop_id = current_loop_id
                _active_read_conns.add(conn)
                return conn
            try:
                await conn.close()
            except Exception:
                pass

    conn = await aiosqlite.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = aiosqlite.Row
    await _apply_pragma(conn)
    _db_loop_id = current_loop_id
    _active_read_conns.add(conn)
    return conn


async def _release_sqlite_read_conn(conn: aiosqlite.Connection) -> None:
    """归还连接到读连接池"""
    global _read_pool, _active_read_conns
    _active_read_conns.discard(conn)
    async with _read_pool_lock:
        if len(_read_pool) < _READ_POOL_SIZE and await _liveness_check(conn):
            _read_pool.append(conn)
        else:
            try:
                await conn.close()
            except Exception:
                pass


async def _get_postgres_conn():
    """获取 PostgreSQL 连接"""
    from core.storage.postgres import get_postgres_pool, PostgresConnection

    pool = await get_postgres_pool()
    return PostgresConnection(pool)



async def _ensure_schema_version_table(db) -> None:
    """创建 schema_version 表（幂等），用于追踪已应用的 migration"""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version TEXT PRIMARY KEY,"
        "  applied_at TEXT DEFAULT (datetime('now', 'localtime')),"
        "  checksum TEXT DEFAULT ''"
        ")"
    )
    await db.commit()


async def _get_applied_versions(db) -> set:
    """返回已应用的 migration 版本号集合"""
    await _ensure_schema_version_table(db)
    cursor = await db.execute("SELECT version FROM schema_version")
    rows = await cursor.fetchall()
    return {r["version"] for r in rows}


async def _run_migrations():
    """数据驱动 migration 执行器 — 扫描 db/migrations/ 目录，按版本号顺序执行。

    每个 migration 文件命名格式: NNN_description.sql（如 003_quality_domains.sql）。
    已执行的 migration 记录在 schema_version 表中，不会重复执行。
    失败时记录错误但不阻塞平台启动（非致命）。
    """
    import hashlib
    from pathlib import Path as _Path

    db = await _get_sqlite_conn()
    applied = await _get_applied_versions(db)
    migrations_dir = _Path(__file__).parent.parent.parent / "db" / "migrations"
    if not migrations_dir.exists():
        return

    # 收集并排序 migration 文件
    files = sorted(
        [f for f in migrations_dir.iterdir() if f.suffix == ".sql"],
        key=lambda f: f.name,
    )
    if not files:
        return

    # 条件迁移：只有旧结构存在时才执行（全新部署 schema.sql 已是新结构，跳过）
    # value = 要检查的旧结构标志列
    _CONDITIONAL_MIGRATIONS = {
        "026_asset_versions_unify.sql": {"table": "asset_versions", "old_column": "id"},
    }

    for fpath in files:
        version = fpath.name  # e.g. "003_quality_domains.sql"
        if version in applied:
            continue

        _logger.info("应用 migration: %s", version)
        try:
            sql = fpath.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
            # 条件迁移：旧结构标志列不存在（已是新结构）→ 跳过，标记已应用
            cond = _CONDITIONAL_MIGRATIONS.get(version)
            if cond:
                cursor = await db.execute(
                    f"SELECT 1 FROM pragma_table_info('{cond['table']}') WHERE name = ?",
                    (cond["old_column"],),
                )
                if not await cursor.fetchone():
                    await db.execute(
                        "INSERT OR REPLACE INTO schema_version (version, checksum) VALUES (?, ?)",
                        (version, checksum),
                    )
                    await db.commit()
                    _logger.info("✅ migration 条件跳过（已是新结构）: %s", version)
                    continue
            await db.executescript(sql)
            await db.execute(
                "INSERT OR REPLACE INTO schema_version (version, checksum) VALUES (?, ?)",
                (version, checksum),
            )
            await db.commit()
            _logger.info("✅ migration 完成: %s", version)
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "duplicate column name" in msg:
                # 幂等：schema.sql 基线已含该列（全新库），或已由更早迁移加过 → 视为成功
                await db.execute(
                    "INSERT OR REPLACE INTO schema_version (version, checksum) VALUES (?, ?)",
                    (version, hashlib.sha256(fpath.read_text(encoding="utf-8").encode()).hexdigest()[:16]),
                )
                await db.commit()
                _logger.info("✅ migration 幂等跳过（列已存在）: %s", version)
            else:
                _logger.warning("⚠️ migration 失败（非致命，下次启动重试）: %s → %s", version, e)

    # 文件驱动 migration 已完成，不再执行后续内联迁移（已全部迁移到 db/migrations/）
    return


async def init_db():
    """初始化数据库"""
    if POSTGRES_ENABLED:
        _logger.info("📦 使用 PostgreSQL 后端")
        from core.storage.postgres import get_postgres_pool, init_postgres_schema

        await get_postgres_pool()
        await init_postgres_schema()

        from core.skills.registry import get_skill_registry
        try:
            await get_skill_registry().seed_platform_skills()
        except Exception as e:
            _logger.warning("平台 Skill 种子数据写入失败: %s", e)

        from core.skills.tool_materializer import reload_all_skill_tools
        try:
            n = await reload_all_skill_tools()
            if n:
                _logger.info("✅ 已恢复 %d 个 Skill 自定义工具", n)
        except Exception as e:
            _logger.warning("Skill 工具恢复失败（非致命）: %s", e)

        from core.skills.kind import repair_all_skill_kinds
        try:
            k = await repair_all_skill_kinds()
            if k:
                _logger.info("✅ 已补全 %d 个 Skill 的 kind 字段", k)
        except Exception as e:
            _logger.warning("Skill kind 补全失败（非致命）: %s", e)

        # 质量规则种子数据初始化 (PostgreSQL)
        try:
            from core.gateway.domain_registry import seed_platform_domains
            await seed_platform_domains()
        except Exception as e:
            _logger.warning("质量规则种子初始化失败（非致命）: %s", e)

        # Agent 模板种子初始化 (PostgreSQL)
        try:
            from core.agent.seed_templates import ensure_seed_templates
            from core.agent.template import get_template_registry
            from core.agent.registry import get_registry
            await ensure_seed_templates()
            t_registry = get_template_registry()
            agent_registry = get_registry()
            for tmpl in await t_registry.list(source="platform"):
                agent_id = f"agent_{tmpl.id}"
                existing = await agent_registry.get(agent_id)
                if existing is None:
                    agent_dict = await t_registry.instantiate(tmpl.id)
                    agent_dict["id"] = agent_id
                    agent_dict["type"] = "domain"
                    try:
                        await agent_registry.register(agent_dict)
                    except Exception:
                        pass
        except Exception as e:
            _logger.warning("Agent 模板初始化失败（非致命）: %s", e)

        _logger.info("✅ PostgreSQL 初始化完成")
        return

    db = await _get_sqlite_conn()
    schema_path = Path(__file__).parent.parent.parent / "db" / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    # 顺序修正（2026-08-10 P1-4）：先 schema 基线建表，再跑幂等 migration。
    # 之前 migrations 先于 schema.sql → 007/011/012/023/029 等 ALTER 基础表的迁移在
    # 全新库上必失败（表不存在），需二次启动 schema.sql 兜底建表后才成功。
    await db.executescript(schema_sql)  # 新库基线（IF NOT EXISTS 幂等）
    await _run_migrations()
    await db.commit()

    # 质量规则种子数据初始化
    try:
        from core.gateway.domain_registry import seed_platform_domains
        await seed_platform_domains()
    except Exception as e:
        _logger.warning("质量规则种子初始化失败（非致命）: %s", e)

    # 领域知识库种子数据初始化
    try:
        from core.knowledge.seed_knowledge import seed_platform_knowledge
        await seed_platform_knowledge()
    except Exception as e:
        _logger.warning("知识库种子初始化失败（非致命）: %s", e)

    from core.skills.registry import get_skill_registry
    await get_skill_registry().seed_platform_skills()

    from core.skills.tool_materializer import reload_all_skill_tools
    try:
        n = await reload_all_skill_tools()
        if n:
            _logger.info("✅ 已恢复 %d 个 Skill 自定义工具", n)
    except Exception as e:
        _logger.warning("Skill 工具恢复失败（非致命）: %s", e)

    from core.skills.kind import repair_all_skill_kinds
    try:
        k = await repair_all_skill_kinds()
        if k:
            _logger.info("✅ 已补全 %d 个 Skill 的 kind 字段", k)
    except Exception as e:
        _logger.warning("Skill kind 补全失败（非致命）: %s", e)

    # Agent 模板系统种子数据初始化
    try:
        from core.agent.seed_templates import ensure_seed_templates
        from core.agent.template import get_template_registry
        from core.agent.registry import get_registry

        # 1. 注册 38 个 platform 模板
        n = await ensure_seed_templates()
        if n:
            _logger.info("✅ 已注册 %d 个新的 Agent 模板", n)

        # 2. 从模板创建 Agent 实例（幂等）
        t_registry = get_template_registry()
        agent_registry = get_registry()
        agent_count = 0
        for tmpl in await t_registry.list(source="platform"):
            agent_id = f"agent_{tmpl.id}"
            existing = await agent_registry.get(agent_id)
            if existing is None:
                agent_dict = await t_registry.instantiate(tmpl.id)
                agent_dict["id"] = agent_id
                agent_dict["type"] = "domain"
                try:
                    await agent_registry.register(agent_dict)
                    agent_count += 1
                except Exception:
                    pass
        if agent_count:
            _logger.info("✅ 从模板创建了 %d 个 Agent 实例", agent_count)
    except Exception as e:
        _logger.warning("Agent 模板初始化失败（非致命）: %s", e)

    # 修复：所有 agent 缺少 skill_ids 的补充默认技能
    try:
        db = await get_db()
        cursor = await db.execute(
            "UPDATE agents SET skill_ids = ? WHERE skill_ids IS NULL OR skill_ids = '' OR skill_ids = '[]' OR skill_ids = '[\"file_read\", \"file_write\", \"web_search\"]'",
            ('["skill_platform_file_read", "skill_platform_file_write", "skill_platform_web_search"]',),
        )
        fixed = cursor.rowcount if cursor else 0
        if fixed:
            await db.commit()
            _logger.info("✅ 已为 %d 个 Agent 补充默认技能", fixed)
    except Exception as e:
        _logger.warning("Agent 技能修复失败（非致命）: %s", e)

    # 修复：模板实例化的存量 agent 同步模板运行时配置（幂等）。
    # 种子只在 agent 不存在时实例化，模板后续新增字段（max_tool_iterations/skill_ids）
    # 不会回流存量 agent —— 此处启动时合并式同步，缺失才补，不覆盖用户修改。
    try:
        import json as _json

        db = await get_db()
        tmpl_rows = await db.execute(
            "SELECT id, skill_ids, max_tool_iterations FROM agent_templates"
        )
        tmpl_cfg = {
            r[0]: (_json.loads(r[1] or "[]"), r[2])
            for r in await tmpl_rows.fetchall()
        }
        agent_rows = await db.execute(
            "SELECT id, extra, skill_ids FROM agents WHERE extra LIKE '%template_id%'"
        )
        synced = 0
        for aid, extra_raw, sids_raw in await agent_rows.fetchall():
            try:
                extra = _json.loads(extra_raw or "{}")
            except (ValueError, TypeError):
                continue
            t_sids, t_mti = tmpl_cfg.get(extra.get("template_id"), (None, None))
            if t_sids is None:
                continue
            changed = False
            if t_mti and not extra.get("max_tool_iterations"):
                extra["max_tool_iterations"] = t_mti
                changed = True
            cur_sids = set(_json.loads(sids_raw or "[]"))
            merged = sorted(cur_sids | set(t_sids))
            if merged != sorted(cur_sids):
                changed = True
            if changed:
                await db.execute(
                    "UPDATE agents SET extra = ?, skill_ids = ? WHERE id = ?",
                    (_json.dumps(extra, ensure_ascii=False),
                     _json.dumps(merged, ensure_ascii=False), aid),
                )
                synced += 1
        if synced:
            await db.commit()
            _logger.info("✅ 已同步 %d 个模板实例 Agent 的运行时配置", synced)
    except Exception as e:
        _logger.warning("模板实例配置同步失败（非致命）: %s", e)


    # 修复：model_profile 中残留的 claude-sonnet-4-6 → deepseek-v4-flash
    try:
        db = await get_db()
        cursor = await db.execute(
            "UPDATE agents SET model_profile = replace(model_profile, 'claude-sonnet-4-6', 'deepseek-v4-flash') "
            "WHERE model_profile LIKE '%claude-sonnet-4-6%'"
        )
        if cursor.rowcount:
            await db.commit()
    except Exception:
        pass

    # 多模态模型绑定：生图/生视频/Judge agent 绑定独立模型
    try:
        import json as _json
        db = await get_db()

        # 生图 agent → image 模型
        image_profile = _json.dumps({
            "llm": {"model_id": "deepseek-v4-flash", "temperature": 0.3, "max_tokens": 2048},
            "image": {"model_id": "__custom__image__"},
        })
        await db.execute(
            "UPDATE agents SET model_profile = ? WHERE (id LIKE '%images%' OR id LIKE '%char_ref%' OR name LIKE '%生图%') AND (model_profile = '{}' OR model_profile IS NULL OR model_profile NOT LIKE '%image%')",
            (image_profile,),
        )

        # 生视频 agent → video 模型
        video_profile = _json.dumps({
            "llm": {"model_id": "deepseek-v4-flash", "temperature": 0.3, "max_tokens": 2048},
            "video": {"model_id": "__custom__video__"},
        })
        await db.execute(
            "UPDATE agents SET model_profile = ? WHERE (id LIKE '%video%' OR name LIKE '%视频%') AND (model_profile = '{}' OR model_profile IS NULL OR model_profile NOT LIKE '%video%')",
            (video_profile,),
        )

        # 评审/Judge agent → 推理模型
        judge_profile = _json.dumps({
            "llm": {"model_id": "deepseek-v4-flash", "temperature": 0.2, "max_tokens": 4096},
            "judge": {"model_id": "deepseek-v4-pro", "temperature": 0.1, "max_tokens": 4096},
        })
        await db.execute(
            "UPDATE agents SET model_profile = ? WHERE (id LIKE '%reviewer%' OR id LIKE '%judge%' OR name LIKE '%审阅%' OR name LIKE '%审核%') AND (model_profile = '{}' OR model_profile IS NULL OR model_profile NOT LIKE '%judge%')",
            (judge_profile,),
        )

        await db.commit()
    except Exception:
        pass

    _logger.info("✅ 数据库初始化完成: %s", DB_PATH)


async def close_db():
    """关闭数据库连接"""
    if POSTGRES_ENABLED:
        from core.storage.postgres import close_postgres_pool
        await close_postgres_pool()
        return

    try:
        from core.observability.workflow_events import WorkflowEventStore
        await WorkflowEventStore.flush_pending()
    except Exception:
        pass

    global _db, _read_pool, _active_read_conns, _db_loop_id
    # 关闭写连接
    if _db:
        try:
            await _db.close()
        except Exception:
            pass
    _db = None
    # 关闭读连接池 + 活跃读连接（修复 _read_db 未定义的 NameError bug）
    all_read_conns = list(_read_pool) + list(_active_read_conns)
    for conn in all_read_conns:
        try:
            await conn.close()
        except Exception:
            pass
    _read_pool.clear()
    _active_read_conns.clear()
    _db_loop_id = None
