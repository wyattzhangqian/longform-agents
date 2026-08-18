"""P0-4 修复测试：workflow_events 过滤流式 token 事件 + 轻量保留策略。

修复前 agent_token_stream / agent_thinking 按 token 逐块落库，占表约 98% 行
（实测 22.5 万 + 7 万 行），且无任何保留策略导致 DB 无界增长（272MB）。
"""
import pytest

from core.observability.workflow_events import WorkflowEventStore, _SKIP_EVENTS


async def _ensure_project(pid: str) -> None:
    """workflow_events.project_id 有 FK → projects，先建项目行。"""
    from core.storage.database import get_db

    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
        (pid, f"p04 测试项目 {pid}"),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_token_events_skipped():
    """流式 token 事件（agent_token_stream / agent_thinking）不落库。"""
    assert "agent_token_stream" in _SKIP_EVENTS
    assert "agent_thinking" in _SKIP_EVENTS

    pid = "p04_skip"
    await _ensure_project(pid)
    await WorkflowEventStore.append(pid, "agent_token_stream", {"content": "token1"})
    await WorkflowEventStore.append(pid, "agent_thinking", {"content": "思考块"})
    _, total = await WorkflowEventStore.list_events(pid)
    assert total == 0


@pytest.mark.asyncio
async def test_milestone_event_persisted():
    """里程碑事件（node_completed）正常落库，不误伤。"""
    pid = "p04_mile"
    await _ensure_project(pid)
    await WorkflowEventStore.append(pid, "node_completed", {"node_id": "n1"})
    events, total = await WorkflowEventStore.list_events(pid, event_type="node_completed")
    assert total >= 1
    assert events[0]["event_type"] == "node_completed"


@pytest.mark.asyncio
async def test_prune_keeps_latest_n():
    """prune 保留最新 N 条，裁剪更旧的（控制 DB 无界增长）。"""
    pid = "p04_prune"
    await _ensure_project(pid)
    for i in range(12):
        await WorkflowEventStore.append(pid, "node_completed", {"idx": i})

    deleted = await WorkflowEventStore.prune(max_events=5)
    assert deleted > 0

    _, total = await WorkflowEventStore.list_events(pid, event_type="node_completed")
    assert total <= 5
