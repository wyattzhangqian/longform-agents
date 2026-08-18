"""产物检查三类状态 + run_id 归属（2026-08-10 P0-3）。

生产级：CHECK_FAILED 不能等同于 MISSING；旧 Run 产物不能被新 Run 误判。
"""

import types

import pytest
from unittest.mock import AsyncMock, patch

from core.graph.types import GraphRunConfig, GraphState, NodeType
from core.run.run_context import RunContext
from core.vault.project_artifact_service import ArtifactCheckResult, ProjectArtifactService


def _fake_graph():
    g = types.SimpleNamespace(graph_id="g1", nodes=[], edges=[])
    g.get_node = lambda nid: None
    g.get_predecessors = lambda nid: []
    g.get_successors = lambda nid: []
    g.get_edge = lambda a, b: None
    return g


def _fake_definition():
    return types.SimpleNamespace(
        id="fake_writer", name="写手", emoji="✍️", role="writer",
        model="deepseek-v4-flash", temperature=0.7, max_tokens=1000,
        system_prompt="写手", tool_ids=["file_write"], skill_ids=[],
        tools=[], model_profile=types.SimpleNamespace(image=None, video=None, music=None),
        extra={},
    )


def _node():
    return types.SimpleNamespace(
        id="unit_1", type=NodeType.AGENT, agent_id="fake_writer",
        label="unit_1", metadata={"phase_id": "unit_1"},
    )


async def _run_agent(check_result):
    from core.agent.base import BaseAgent
    from core.graph.runtime import NodeExecutor

    run_ctx = RunContext(run_id="run_x", project_id="p1", task="t")
    executor = NodeExecutor(graph=_fake_graph())
    executor.set_live_run_ctx(run_ctx)
    fake_def = _fake_definition()
    fake_agent = BaseAgent(definition=fake_def)
    fake_agent.execute = AsyncMock(return_value={"result": "正文", "raw": ""})
    executor._agent_cache["fake_writer"] = fake_agent
    state = GraphState(values={"project_id": "p1"})
    config = GraphRunConfig(project_id="p1", phase_specs=[])
    with patch("core.agent.registry.get_registry") as m_reg, \
         patch("core.vault.project_artifact_service.ProjectArtifactService.check_phase_artifact",
               new=AsyncMock(return_value=check_result)), \
         patch.object(executor, "_build_agent_input", new=AsyncMock(return_value={})), \
         patch.object(executor, "_persist_agent_output", new=AsyncMock(return_value=None)):
        m_reg.return_value.get = AsyncMock(return_value=fake_def)
        return await executor._execute_agent(_node(), state, config), run_ctx


@pytest.mark.asyncio
async def test_present_not_failed():
    result, _ = await _run_agent(ArtifactCheckResult.PRESENT)
    assert result.get("status") != "error"


@pytest.mark.asyncio
async def test_missing_marks_failed_no_output():
    result, run_ctx = await _run_agent(ArtifactCheckResult.MISSING)
    assert result.get("status") == "error"
    assert result.get("error_kind") == "no_output"
    assert run_ctx.diagnostics["has_critical"] is True


@pytest.mark.asyncio
async def test_check_failed_marks_failed_not_missing():
    """CHECK_FAILED（查询异常）→ FAILED + artifact_check_failed，不能当成 missing。"""
    result, run_ctx = await _run_agent(ArtifactCheckResult.CHECK_FAILED)
    assert result.get("status") == "error"
    assert result.get("error_kind") == "artifact_check_failed"
    assert run_ctx.diagnostics["has_critical"] is True


@pytest.mark.asyncio
async def test_run_id_ownership_isolates_runs():
    """旧 Run 的产物（run_id 不同）不被新 Run 的 has_phase_artifact 匹配。"""
    from core.storage.database import get_db

    db = await get_db()
    await db.execute("DELETE FROM artifacts WHERE project_id = 'p_own'")
    # 父表行（满足 FK）
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name, status) VALUES (?, ?, 'idle')",
        ("p_own", "归属测试"),
    )
    await db.execute(
        "INSERT OR IGNORE INTO agents (id, name, type, status, emoji, role, capabilities, model) "
        "VALUES (?, ?, 'custom', 'active', '🤖', 'writer', '[]', 'platform_default')",
        ("fake_writer", "写手"),
    )
    await db.execute(
        """INSERT INTO artifacts (id, project_id, agent_id, type, name, file_path, metadata, run_id)
           VALUES (?, ?, ?, 'file', 'old.md', 'p_own/old.md', '{}', ?)""",
        ("art_old_1", "p_own", "fake_writer", "run_old"),
    )
    await db.commit()

    # 新 Run（run_new）检查 → 不应匹配旧 Run 产物
    has = await ProjectArtifactService.has_phase_artifact(
        "p_own", "fake_writer", "unit_1", run_id="run_new",
    )
    assert has is False, "旧 Run 产物不应被新 Run 匹配"

    # 同 run_id → 匹配
    has2 = await ProjectArtifactService.has_phase_artifact(
        "p_own", "fake_writer", "unit_1", run_id="run_old",
    )
    assert has2 is True

    await db.execute("DELETE FROM artifacts WHERE project_id = 'p_own'")
    await db.commit()
