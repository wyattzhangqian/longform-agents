"""决策状态一致性测试 — 三处状态源（DB / RunContext / 推断）的恢复路径（P1）"""

import json

import pytest

import core.run  # noqa: F401 — 预热模块依赖，避免循环导入

PID = "test_proj_003"


async def _clean(project_id: str):
    from core.storage.database import get_db
    db = await get_db()
    await db.execute("DELETE FROM decisions WHERE project_id = ?", (project_id,))
    await db.execute(
        "UPDATE projects SET checkpoint_state = '', status = 'idle' WHERE id = ?",
        (project_id,),
    )
    await db.commit()


@pytest.fixture(autouse=True)
async def _isolate():
    await _clean(PID)
    yield
    await _clean(PID)


async def _insert_decision(project_id: str, question: str = "选哪个方案？") -> int:
    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO decisions (project_id, phase, question, options, escalated_by) VALUES (?, ?, ?, ?, ?)",
        (project_id, "roundtable", question,
         json.dumps([{"id": "a", "label": "方案 A"}, {"id": "b", "label": "方案 B"}]), "agent_x"),
    )
    await db.commit()
    return int(cursor.lastrowid)


async def _save_ctx_with_pending(project_id: str, decision_id: int):
    from core.agent.state_manager import StateManager
    from core.run.run_context import RunContext

    ctx = RunContext(run_id="r1", project_id=project_id, task="测试任务")
    ctx.control.pending_decision_id = decision_id
    ctx.control.pause_reason = "decision_required"
    await StateManager.save_run_context(project_id, ctx)
    return ctx


async def _set_status(project_id: str, status: str):
    from core.storage.database import get_db
    db = await get_db()
    await db.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
    await db.commit()


@pytest.mark.asyncio
class TestPendingDecisionSources:
    async def test_source_database(self):
        """DB 有未解决决策 → source=database，优先级最高"""
        from core.communication.decision_service import load_pending_decision_raw

        did = await _insert_decision(PID)
        decision, source = await load_pending_decision_raw(PID)
        assert source == "database"
        assert decision["id"] == did
        assert len(decision["options"]) == 2

    async def test_source_run_context(self):
        """DB 无记录但 RunContext 有 pending_decision_id → source=run_context"""
        from core.communication.decision_service import load_pending_decision_raw

        await _save_ctx_with_pending(PID, 42)
        decision, source = await load_pending_decision_raw(PID)
        assert source == "run_context"
        assert decision["id"] == 42
        assert decision["options"]  # 默认选项兜底

    async def test_source_inferred_from_paused(self):
        """DB 和 RunContext 都没有，但项目 paused → source=inferred"""
        from core.communication.decision_service import load_pending_decision_raw

        await _set_status(PID, "paused")
        decision, source = await load_pending_decision_raw(PID)
        assert source == "inferred"
        assert decision["id"] == 0

    async def test_no_pending_anywhere(self):
        from core.communication.decision_service import load_pending_decision_raw

        decision, source = await load_pending_decision_raw(PID)
        assert decision is None and source is None

    async def test_database_priority_over_run_context(self):
        """两处都有时以 DB 为准（唯一真相）"""
        from core.communication.decision_service import load_pending_decision_raw

        did = await _insert_decision(PID)
        await _save_ctx_with_pending(PID, 9999)
        decision, source = await load_pending_decision_raw(PID)
        assert source == "database"
        assert decision["id"] == did

    async def test_resolved_decision_not_returned(self):
        """已解决的决策不再算 pending"""
        from core.storage.database import get_db
        from core.communication.decision_service import load_pending_decision_raw

        did = await _insert_decision(PID)
        db = await get_db()
        await db.execute(
            "UPDATE decisions SET resolved_at = datetime('now'), chosen_option = 'a' WHERE id = ?",
            (did,),
        )
        await db.commit()

        decision, source = await load_pending_decision_raw(PID)
        assert decision is None


@pytest.mark.asyncio
class TestEnsureDecisionPersisted:
    async def test_inferred_decision_gets_persisted(self):
        """推断出的决策（id=0）必须落库并回写 RunContext — 三处状态收敛到 DB"""
        from core.agent.state_manager import StateManager
        from core.communication.decision_service import (
            ensure_decision_persisted,
            load_pending_decision_raw,
        )
        from core.run.run_context import RunContext

        # 项目 paused 且有 checkpoint（无 pending id）
        ctx = RunContext(run_id="r1", project_id=PID, task="t")
        await StateManager.save_run_context(PID, ctx)
        await _set_status(PID, "paused")

        decision, source = await load_pending_decision_raw(PID)
        assert source == "inferred"

        persisted = await ensure_decision_persisted(PID, decision)
        assert persisted["id"] > 0

        # 再次加载：source 已收敛为 database
        decision2, source2 = await load_pending_decision_raw(PID)
        assert source2 == "database"
        assert decision2["id"] == persisted["id"]

        # RunContext 也被回写
        ctx2 = await StateManager.load_run_context(PID)
        assert ctx2.control.pending_decision_id == persisted["id"]

    async def test_existing_db_decision_unchanged(self):
        """已在 DB 的决策不重复落库"""
        from core.communication.decision_service import ensure_decision_persisted
        from core.storage.database import get_db

        did = await _insert_decision(PID)
        result = await ensure_decision_persisted(PID, {"id": did, "question": "q", "options": []})
        assert result["id"] == did

        db = await get_db()
        cursor = await db.execute("SELECT COUNT(*) AS c FROM decisions WHERE project_id = ?", (PID,))
        row = await cursor.fetchone()
        assert row["c"] == 1


@pytest.mark.asyncio
class TestResolveDecisionGuards:
    async def test_resolve_nonexistent_raises(self):
        from core.communication.decision_service import resolve_decision_and_resume

        with pytest.raises(ValueError, match="不存在"):
            await resolve_decision_and_resume(999999, "a")

    async def test_resolve_already_resolved_raises(self):
        from core.storage.database import get_db
        from core.communication.decision_service import resolve_decision_and_resume

        did = await _insert_decision(PID)
        db = await get_db()
        await db.execute(
            "UPDATE decisions SET resolved_at = datetime('now'), chosen_option = 'a' WHERE id = ?",
            (did,),
        )
        await db.commit()

        with pytest.raises(ValueError, match="已解决"):
            await resolve_decision_and_resume(did, "b")


@pytest.mark.asyncio
class TestOrphanedRunRecovery:
    async def test_detect_orphaned_running(self):
        """DB 状态 running 但进程内无活跃执行 → 判定为僵尸"""
        from core.run.run_recovery import detect_orphaned_run

        await _set_status(PID, "running")
        assert await detect_orphaned_run(PID) is True

    async def test_completed_not_orphaned(self):
        from core.run.run_recovery import detect_orphaned_run

        await _set_status(PID, "completed")
        assert await detect_orphaned_run(PID) is False

    async def test_alive_execution_not_orphaned(self):
        from core.run.graph_registry import mark_execution_started, mark_execution_finished
        from core.run.run_recovery import detect_orphaned_run

        await _set_status(PID, "running")
        mark_execution_started(PID)
        try:
            assert await detect_orphaned_run(PID) is False
        finally:
            mark_execution_finished(PID)

    async def test_reconcile_marks_failed_and_recoverable(self):
        from core.project.manager import ProjectManager
        from core.run.run_recovery import (
            clear_recover_pending,
            is_recover_pending,
            reconcile_orphaned_run,
        )

        await _set_status(PID, "running")
        meta = await reconcile_orphaned_run(PID)
        assert meta and meta["orphaned"] is True

        project = await ProjectManager.get(PID)
        assert project.status == "failed"
        assert is_recover_pending(project) is True

        await clear_recover_pending(PID)
        project = await ProjectManager.get(PID)
        assert is_recover_pending(project) is False
