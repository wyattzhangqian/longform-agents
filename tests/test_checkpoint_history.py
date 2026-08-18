"""P1-10 修复测试：checkpoint 历史保留（run_checkpoints 表）。

修复前 checkpoint 每项目单槽，新 run/replay 直接覆盖，历史丢失。
修复后 save_run_context 额外写入 immutable 历史行，供回放基准与审计。
"""
import pytest

from core.agent.state_manager import StateManager
from core.run.run_context import RunContext


async def _ensure_project(pid: str) -> None:
    from core.storage.database import get_db

    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
        (pid, f"p110 测试项目 {pid}"),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_save_run_context_writes_history():
    """每次保存 RunContext 都追加历史行（含版本递增）。"""
    pid = "p110_history"
    await _ensure_project(pid)

    v1 = await StateManager.save_run_context(
        pid, RunContext(project_id=pid, run_id="run_1", task="第一版")
    )
    v2 = await StateManager.save_run_context(
        pid, RunContext(project_id=pid, run_id="run_1", task="第二版")
    )

    history = await StateManager.list_run_checkpoints(pid)
    assert len(history) >= 2
    assert history[0]["version"] == v2  # 最新在前
    assert history[0]["run_id"] == "run_1"
    assert v2 > v1


@pytest.mark.asyncio
async def test_load_run_checkpoint_restores_ctx():
    """从历史行恢复 RunContext（含 task/project_id）。"""
    pid = "p110_restore"
    await _ensure_project(pid)
    await StateManager.save_run_context(
        pid, RunContext(project_id=pid, run_id="run_9", task="原始任务")
    )

    history = await StateManager.list_run_checkpoints(pid)
    assert history
    restored = await StateManager.load_run_checkpoint(pid, history[0]["id"])
    assert restored is not None
    assert restored.task == "原始任务"
    assert restored.project_id == pid
    assert restored.run_id == "run_9"


@pytest.mark.asyncio
async def test_load_run_checkpoint_unknown_returns_none():
    """未知历史 checkpoint 返回 None（不崩溃）。"""
    assert await StateManager.load_run_checkpoint("p110_none", 999999) is None
