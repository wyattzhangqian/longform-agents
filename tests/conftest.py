"""Pytest 共享 fixtures — agent-platform 通用层测试基础设施

设计原则:
  - 所有 fixture 属于 core/ 通用层，不引用任何 DomainAdapter 业务实现
  - 测试数据使用中性词汇，不含任何领域业务语义
  - 业务适配器（如 ComicAdapter）的测试放在 adapters/ 子目录
"""

import asyncio
import sys
import os
import tempfile

# 测试环境隔离：必须在导入 config 之前设置
if "DATABASE_PATH" not in os.environ:
    _tmp_db = os.path.join(tempfile.mkdtemp(prefix="agent_platform_test_"), "test.db")
    os.environ["DATABASE_PATH"] = _tmp_db
# 单测默认关闭认证，避免每个用例携带 JWT；auth 行为见 tests/test_api_smoke.py
if "AUTH_ENABLED" not in os.environ:
    os.environ["AUTH_ENABLED"] = "false"

import pytest
import pytest_asyncio

# 确保 project root 在 sys.path 中（允许 from core.xxx import）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _close_db_with_timeout(timeout: float = 5.0) -> None:
    """限时关闭 DB 连接。

    close_db 内部会 flush 挂起的事件写入任务；这些任务可能创建于已被
    pytest-asyncio 关闭的事件循环上，跨循环 await 会永久悬挂——必须设超时。
    """
    from core.storage.database import close_db

    async def _close():
        try:
            await asyncio.wait_for(close_db(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    try:
        asyncio.run(_close())
    except Exception:
        pass


@pytest.fixture(scope="session")
def event_loop():
    """会话级事件循环 — 全部 async 测试共享一个 loop。

    aiosqlite 连接绑定创建时的 loop；per-test loop 会导致跨 loop 关闭连接
    悬挂、worker 线程（非 daemon）阻塞进程退出。单一 session loop 根治。
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _init_test_db():
    """会话级初始化测试数据库（schema + 迁移），独立于业务 DB"""
    from core.storage.database import init_db, close_db, get_db

    await init_db()
    # 预置测试项目（conversations 等表存在 FK 约束）
    db = await get_db()
    for pid in ("test_proj_001", "test_proj_002", "test_proj_003"):
        await db.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
            (pid, f"测试项目 {pid}"),
        )
    await db.commit()

    yield

    # 在同一 loop 上关闭连接，确保 aiosqlite worker 线程干净退出
    try:
        await asyncio.wait_for(close_db(), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass


def pytest_sessionfinish(session, exitstatus):
    """会话结束时关闭 aiosqlite 连接，避免线程 teardown 悬挂"""
    _close_db_with_timeout()


# ============================================================
# ConversationBus fixtures
# ============================================================

@pytest.fixture
def bus():
    """创建一个干净的 ConversationBus 实例"""
    from core.communication.bus import ConversationBus
    return ConversationBus(project_id="test_proj_001")


@pytest_asyncio.fixture
async def bus_with_messages():
    """包含已发送消息的 ConversationBus（需要 async）

    使用中性消息内容，无任何业务语义。
    """
    from core.communication.bus import ConversationBus

    b = ConversationBus(project_id="test_proj_002")
    await b.send_status("系统启动")
    await b.send_task("agent_a", "agent_b", "请处理任务 A")
    await b.send_handoff("agent_a", "agent_b", "任务 A 已完成，交接给你")
    return b


# ============================================================
# EventBus fixtures
# ============================================================

@pytest_asyncio.fixture
async def event_bus():
    """创建干净的 EventBus 实例（async fixture 确保有运行中的事件循环）"""
    from core.events import EventBus
    eb = EventBus()
    return eb


# ============================================================
# QualityGateway fixtures
# ============================================================

@pytest.fixture
def empty_gateway():
    """无规则的 QualityGateway"""
    from core.gateway import QualityGateway
    return QualityGateway()


@pytest.fixture
def sample_gateway():
    """带 3 条示例规则的 QualityGateway（中性规则，无业务语义）"""
    from core.gateway import QualityGateway, QualityRule

    gw = QualityGateway()
    gw.add_rules([
        QualityRule(
            rule_id="req_title",
            name="标题非空检查",
            description="产出必须包含 title 字段",
            check_type="structure",
            severity="error",
            config={"required_fields": ["title"]},
        ),
        QualityRule(
            rule_id="min_content",
            name="内容长度检查",
            description="文本内容至少 10 个字符",
            check_type="generic",
            severity="warning",
            config={"min_length": 10},
        ),
        QualityRule(
            rule_id="consistency_chk",
            name="一致性占位",
            description="一致性检查占位规则",
            check_type="consistency",
            severity="info",
        ),
    ])
    return gw


@pytest.fixture
def custom_fn_gateway():
    """带自定义检查函数的 QualityGateway"""
    from core.gateway import QualityGateway, QualityRule

    def length_check(rule, data):
        text = data.text_content or ""
        min_len = rule.config.get("min_length", 20)
        if len(text) < min_len:
            return {
                "rule_id": rule.rule_id,
                "severity": rule.severity,
                "message": f"内容长度 {len(text)} < 要求的 {min_len}",
            }
        return None

    gw = QualityGateway()
    gw.add_rules([
        QualityRule(
            rule_id="custom_len",
            name="自定义长度检查",
            check_type="custom",
            severity="error",
            config={"min_length": 50},
            check_fn=length_check,
        )
    ])
    return gw


# ============================================================
# Neutral test data helpers（纯通用层测试数据）
# ============================================================

@pytest.fixture
def sample_output():
    """模拟 Agent 产出：通过所有结构检查（中性数据，无业务字段）"""
    return {
        "title": "输出结果 A",
        "content": "这是一段足够长的文本内容用于测试质量检查规则的通过条件。",
        "metadata": {"version": 1},
    }


@pytest.fixture
def bad_output():
    """模拟 Agent 产出：违反多条规则（缺字段 + 内容过短）"""
    return {
        "content": "短",
    }


@pytest.fixture
def sample_agent_result(sample_output):
    """别名 — 历史用例使用 sample_agent_result 命名"""
    return sample_output


@pytest.fixture
def bad_agent_result(bad_output):
    """别名 — 历史用例使用 bad_agent_result 命名"""
    return bad_output
