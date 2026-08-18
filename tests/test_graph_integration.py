"""Graph / recovery 集成冒烟 — M1 核心路径（不调用 LLM）"""

import json
import uuid

import pytest

import core.run  # noqa: F401 — 预热模块依赖

PID = "test_proj_graph"


async def _ensure_project(project_id: str = PID):
    from core.storage.database import get_db

    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name, status) VALUES (?, ?, ?)",
        (project_id, f"Graph 集成测试 {project_id}", "idle"),
    )
    await db.commit()


async def _reset_project(project_id: str = PID):
    from core.storage.database import get_db

    db = await get_db()
    await db.execute(
        "UPDATE projects SET status = 'idle', checkpoint_state = '', config = '{}' WHERE id = ?",
        (project_id,),
    )
    await db.commit()


@pytest.fixture(autouse=True)
async def _isolate_graph_project():
    await _ensure_project()
    await _reset_project()
    yield
    await _reset_project()
    from core.run import graph_registry

    graph_registry.remove_collaboration_graph(PID)
    graph_registry.mark_execution_finished(PID)


async def _save_paused_checkpoint(project_id: str = PID):
    """写入可冷恢复的 Graph checkpoint（paused 态）"""
    from core.agent.state_manager import StateManager
    from core.graph.runtime import GraphRuntime
    from core.run.run_context import RunContext
    from core.run.template_compiler import TemplateCompiler

    from core.graph.types import GraphRunConfig, GraphState

    graph, specs = TemplateCompiler.compile(
        agent_ids=["agent_smoke_a"],
        graph_id=f"smoke_{project_id}",
    )
    ctx = RunContext(
        run_id="run_smoke",
        project_id=project_id,
        task="graph 集成冒烟任务",
        phase_specs=[s.model_dump() for s in specs],
        config_snapshot={"mode": "sequential", "review_mode": False},
    )
    runtime = GraphRuntime(graph)
    runtime.config = GraphRunConfig(project_id=project_id, checkpoint_enabled=True)
    runtime.state = GraphState(values={"task": ctx.task, "project_id": project_id})
    ctx.capture_graph_resume(
        graph_def=graph.to_dict(),
        graph_state=runtime.state.snapshot(),
        scheduler=runtime.scheduler.snapshot(),
        runtime_status="paused",
        graph_run_config=runtime.config.model_dump(),
    )
    await StateManager.save_run_context(project_id, ctx)
    return graph, specs, ctx


@pytest.mark.asyncio
class TestGraphRegistry:
    async def test_register_get_remove(self):
        from core.run import graph_registry
        from core.run.collaboration_graph import CollaborationGraph
        from core.run.run_context import RunContext
        from core.run.template_compiler import TemplateCompiler

        graph, specs = TemplateCompiler.compile(agent_ids=["agent_reg"], graph_id="reg_test")
        ctx = RunContext(run_id="r1", project_id=PID, task="t")
        collab = CollaborationGraph(graph=graph, phase_specs=specs, run_context=ctx)
        graph_registry.set_collaboration_graph(PID, collab)
        assert graph_registry.get_collaboration_graph(PID) is collab
        assert graph_registry.is_execution_alive(PID)

        graph_registry.remove_collaboration_graph(PID)
        assert graph_registry.get_collaboration_graph(PID) is None
        assert not graph_registry.is_execution_alive(PID)

    async def test_execution_started_flag(self):
        from core.run import graph_registry

        assert not graph_registry.is_execution_alive(PID)
        graph_registry.mark_execution_started(PID)
        assert graph_registry.is_execution_alive(PID)
        graph_registry.mark_execution_finished(PID)
        assert not graph_registry.is_execution_alive(PID)


@pytest.mark.asyncio
class TestCollaborationGraphCheckpoint:
    async def test_from_checkpoint_rebuilds_graph(self):
        from core.run.collaboration_graph import CollaborationGraph

        graph, specs, ctx = await _save_paused_checkpoint()
        collab = await CollaborationGraph.from_checkpoint(PID)

        assert collab.run_context.project_id == PID
        assert collab.run_context.task == ctx.task
        assert len(collab.phase_specs) == len(specs)
        assert collab.runtime is not None
        assert collab.graph.graph_id == graph.graph_id

    async def test_ensure_collaboration_graph_cold_recovery(self):
        from core.run import graph_registry
        from core.run.graph_registry import ensure_collaboration_graph

        await _save_paused_checkpoint()
        graph_registry.remove_collaboration_graph(PID)

        collab = await ensure_collaboration_graph(PID)
        assert collab.run_context.project_id == PID
        assert collab.runtime is not None

    async def test_from_checkpoint_missing_raises(self):
        from core.run.collaboration_graph import CollaborationGraph

        with pytest.raises(ValueError, match="无 Graph checkpoint"):
            await CollaborationGraph.from_checkpoint("no_such_project_xyz")


@pytest.mark.asyncio
class TestOrphanRunRecovery:
    async def test_detect_and_reconcile_orphan(self):
        from core.project.manager import ProjectManager
        from core.run.run_recovery import (
            ORPHAN_REASON,
            detect_orphaned_run,
            reconcile_orphaned_run,
        )

        await _save_paused_checkpoint()
        await ProjectManager.update(PID, {"status": "running"})

        assert await detect_orphaned_run(PID)
        info = await reconcile_orphaned_run(PID)
        assert info is not None
        assert info["orphaned"] is True
        assert info["orphan_reason"] == ORPHAN_REASON

        project = await ProjectManager.get(PID)
        assert project.status == "failed"
        extra = project.config.extra if isinstance(project.config.extra, dict) else {}
        assert extra.get("recover_pending") is True

    async def test_reconcile_all_orphaned_runs_batch(self):
        from core.project.manager import ProjectManager
        from core.run.run_recovery import reconcile_all_orphaned_runs

        await _save_paused_checkpoint()
        await ProjectManager.update(PID, {"status": "queued"})

        count = await reconcile_all_orphaned_runs()
        assert count >= 1

        project = await ProjectManager.get(PID)
        assert project.status == "failed"

    async def test_clear_recover_pending(self):
        from core.project.manager import ProjectManager
        from core.run.run_recovery import clear_recover_pending, reconcile_orphaned_run

        await _save_paused_checkpoint()
        await ProjectManager.update(PID, {"status": "running"})
        await reconcile_orphaned_run(PID)

        await clear_recover_pending(PID)
        project = await ProjectManager.get(PID)
        extra = project.config.extra if isinstance(project.config.extra, dict) else {}
        assert "recover_pending" not in extra
        assert "orphan_reason" not in extra


@pytest.mark.asyncio
class TestInterjectApiPath:
    """协作 interject 服务层冒烟（不经 HTTP）"""

    async def test_interject_persisted_in_run_context(self):
        from core.agent.state_manager import StateManager
        from core.run.run_context import HumanInterjection, RunContext

        ctx = RunContext(run_id="r1", project_id=PID, task="t")
        ctx.human_interjections.append(
            HumanInterjection(content="请补充测试用例", target_agent="agent_smoke_a")
        )
        ctx.touch()
        await StateManager.save_run_context(PID, ctx)

        loaded = await StateManager.load_run_context(PID)
        assert len(loaded.human_interjections) == 1
        assert loaded.human_interjections[0].content == "请补充测试用例"
