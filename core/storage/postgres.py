"""PostgreSQL 异步连接池 — 基于 asyncpg

提供：
- 连接池管理（min/max size）
- 自动重连
- SQLite 方言兼容层（? / :name / INSERT OR IGNORE / datetime）
- Row 工厂（dict-like）+ lastrowid（INSERT RETURNING id）
"""

from __future__ import annotations
import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import asyncpg

from config import POSTGRES_CONFIG

_logger = logging.getLogger(__name__)

Params = Union[tuple, dict]

# ============================================================
# 连接池
# ============================================================

_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


async def get_postgres_pool() -> asyncpg.Pool:
    """获取 PostgreSQL 连接池（单例）"""
    global _pool

    if _pool is not None:
        return _pool

    async with _pool_lock:
        if _pool is not None:
            return _pool

        cfg = POSTGRES_CONFIG
        if not cfg.get("enabled"):
            raise RuntimeError("PostgreSQL 未启用，请设置 POSTGRES_ENABLED=true")

        _pool = await asyncpg.create_pool(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            min_size=cfg.get("min_size", 2),
            max_size=cfg.get("max_size", 10),
            command_timeout=cfg.get("command_timeout", 30),
            server_settings={
                "application_name": "agent-platform",
                "timezone": "Asia/Shanghai",
            },
        )
        _logger.info(
            "PostgreSQL 连接池已创建 (min=%d, max=%d)",
            cfg.get("min_size", 2),
            cfg.get("max_size", 10),
        )
        return _pool


async def close_postgres_pool():
    """关闭 PostgreSQL 连接池"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        _logger.info("PostgreSQL 连接池已关闭")


async def postgres_health() -> dict:
    """连接池健康探测"""
    if not POSTGRES_CONFIG.get("enabled"):
        return {"enabled": False, "status": "disabled"}
    try:
        pool = await get_postgres_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1")
        return {"enabled": True, "status": "connected" if val == 1 else "degraded"}
    except Exception as e:
        return {"enabled": True, "status": "error", "error": str(e)}


# ============================================================
# SQL 方言转换
# ============================================================

_NOW_SQL = "to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')"


def _convert_named_params(sql: str, params: dict) -> Tuple[str, tuple]:
    order: List[str] = []

    def _repl(match: re.Match) -> str:
        order.append(match.group(1))
        return "?"

    converted = re.sub(r":(\w+)", _repl, sql)
    return converted, tuple(params[name] for name in order)


def _convert_qmark_params(sql: str, params: tuple) -> Tuple[str, tuple]:
    if "$1" in sql or not params:
        return sql, params
    idx = [0]

    def _repl(_match: re.Match) -> str:
        idx[0] += 1
        return f"${idx[0]}"

    return re.sub(r"\?", _repl, sql), params


def _convert_sqlite_dialect(sql: str) -> str:
    s = sql
    s = s.replace("datetime('now', 'localtime')", _NOW_SQL)
    s = re.sub(
        r"INSERT\s+OR\s+IGNORE\s+INTO",
        "INSERT INTO",
        s,
        flags=re.IGNORECASE,
    )
    # pending_tool_approvals / artifacts 等 UPSERT
    if re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+pending_tool_approvals", s, re.I):
        s = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + (
                " ON CONFLICT (tool_call_id) DO UPDATE SET "
                "project_id = EXCLUDED.project_id, "
                "payload = EXCLUDED.payload, "
                "created_at = EXCLUDED.created_at"
            )
    elif re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+artifacts", s, re.I):
        s = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + (
                " ON CONFLICT (id) DO UPDATE SET "
                "project_id = EXCLUDED.project_id, "
                "agent_id = EXCLUDED.agent_id, "
                "type = EXCLUDED.type, "
                "name = EXCLUDED.name, "
                "file_path = EXCLUDED.file_path, "
                "metadata = EXCLUDED.metadata"
            )
    elif re.search(r"INSERT\s+OR\s+REPLACE\s+INTO", s, re.I):
        s = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
    s = s.replace("REAL", "DOUBLE PRECISION")
    return s


def _should_returning_id(sql: str) -> bool:
    upper = sql.strip().upper()
    if not upper.startswith("INSERT"):
        return False
    if "RETURNING" in upper:
        return False
    if "INTO CONVERSATIONS" in upper or "INTO DECISIONS" in upper:
        return True
    if "INTO WORKFLOW_EVENTS" in upper or "INTO LLM_USAGE" in upper:
        return True
    if "INTO AGENT_MEMORIES" in upper or "INTO AGENT_LEARNINGS" in upper:
        return True
    return False


def prepare_sql(sql: str, params: Params = ()) -> Tuple[str, tuple]:
    """SQLite 风格 SQL → PostgreSQL 可执行语句"""
    if isinstance(params, dict):
        sql, params = _convert_named_params(sql, params)
    sql = _convert_sqlite_dialect(sql)
    sql, params = _convert_qmark_params(sql, params if isinstance(params, tuple) else ())
    return sql, params


# ============================================================
# 查询执行器（兼容 aiosqlite 风格接口）
# ============================================================


class PostgresCursor:
    """模拟 aiosqlite Cursor"""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._rows: List[asyncpg.Record] = []
        self.lastrowid: Optional[int] = None

    async def execute(self, sql: str, params: Params = ()):
        converted_sql, converted_params = prepare_sql(sql, params)
        self.lastrowid = None

        if _should_returning_id(converted_sql):
            converted_sql = converted_sql.rstrip().rstrip(";") + " RETURNING id"

        async with self._pool.acquire() as conn:
            upper = converted_sql.strip().upper()
            if upper.startswith("INSERT") and "RETURNING" in upper:
                row = await conn.fetchrow(converted_sql, *converted_params)
                self._rows = [row] if row else []
                if row and "id" in row:
                    self.lastrowid = int(row["id"])
            elif upper.startswith("SELECT") or " RETURNING " in upper:
                self._rows = await conn.fetch(converted_sql, *converted_params)
                if self._rows and "id" in self._rows[0]:
                    self.lastrowid = int(self._rows[0]["id"])
            else:
                status = await conn.execute(converted_sql, *converted_params)
                self._rows = []
                if status.upper().startswith("INSERT") and "RETURNING" not in converted_sql.upper():
                    pass
        return self

    async def fetchone(self):
        if self._rows:
            return dict(self._rows[0])
        return None

    async def fetchall(self):
        return self._rows

    async def fetch(self, n: int = -1):
        if n < 0 or n >= len(self._rows):
            return self._rows
        return self._rows[:n]


class PostgresConnection:
    """模拟 aiosqlite.Connection"""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def execute(self, sql: str, params: Params = ()) -> PostgresCursor:
        cursor = PostgresCursor(self._pool)
        await cursor.execute(sql, params)
        return cursor

    async def executemany(self, sql: str, params_list: list):
        converted_sql, _ = prepare_sql(sql, ())
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for params in params_list:
                    _, converted_params = prepare_sql(sql, params)
                    await conn.execute(converted_sql, *converted_params)

    async def executescript(self, sql: str):
        async with self._pool.acquire() as conn:
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                if stmt.startswith("--"):
                    continue
                await conn.execute(stmt)

    async def commit(self):
        pass

    async def close(self):
        pass

    async def fetch_one(self, sql: str, params: Params = ()):
        converted_sql, converted_params = prepare_sql(sql, params)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(converted_sql, *converted_params)
            if row:
                return dict(row)
            return None

    async def fetch_all(self, sql: str, params: Params = ()):
        converted_sql, converted_params = prepare_sql(sql, params)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(converted_sql, *converted_params)
            return [dict(r) for r in rows]


# ============================================================
# 数据库初始化
# ============================================================


async def init_postgres_schema():
    """初始化 PostgreSQL Schema（base + extras）"""
    pool = await get_postgres_pool()
    conn = PostgresConnection(pool)

    schema_sql = _get_postgres_schema()
    await conn.executescript(schema_sql)

    extras_path = Path(__file__).parent.parent.parent / "db" / "schema.pg.extras.sql"
    if extras_path.exists():
        await conn.executescript(extras_path.read_text(encoding="utf-8"))

    _logger.info("PostgreSQL Schema 初始化完成")


def _get_postgres_schema() -> str:
    """返回 PostgreSQL 兼容的 Schema"""
    root = Path(__file__).parent.parent.parent
    schema_path = root / "db" / "schema.pg.sql"
    if schema_path.exists():
        return schema_path.read_text(encoding="utf-8")

    sqlite_schema = (root / "db" / "schema.sql").read_text(encoding="utf-8")

    replacements = [
        ("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"),
        (
            "CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(\n"
            "    content,\n"
            "    content_rowid='rowid',\n"
            "    tokenize='unicode61'\n"
            ");",
            "-- FTS: PostgreSQL 使用 GIN 索引（见 schema.pg.extras.sql）",
        ),
        ("datetime('now', 'localtime')", _NOW_SQL),
        ("REAL", "DOUBLE PRECISION"),
    ]

    for old, new in replacements:
        sqlite_schema = sqlite_schema.replace(old, new)

    # system_settings 种子：INSERT OR IGNORE → ON CONFLICT
    sqlite_schema = re.sub(
        r"INSERT OR IGNORE INTO system_settings",
        "INSERT INTO system_settings",
        sqlite_schema,
        flags=re.I,
    )
    if "ON CONFLICT (key)" not in sqlite_schema:
        sqlite_schema = sqlite_schema.replace(
            "('cache_enabled',       'true',                          'cache',  '缓存开关');",
            "('cache_enabled',       'true',                          'cache',  '缓存开关')\n"
            "ON CONFLICT (key) DO NOTHING;",
        )

    return sqlite_schema
