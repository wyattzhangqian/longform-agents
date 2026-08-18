"""产物落盘强制检查（2026-08-09 P1 动作6）：Agent 完成但无任何落盘产物 → 节点 FAILED。

"宁可让 run 显式 FAILED，也不要让 run 看起来 COMPLETED 但实际残缺。"
"""

import types

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.graph.types import GraphRunConfig, GraphState, NodeType
from core.run.run_context import RunContext


def _fake_graph():
    g = types.SimpleNamespace(
        graph_id="g1",
        nodes=[],
        edges=[],
    )
    g.get_node = lambda nid: None
    g.get_predecessors = lambda nid: []
    g.get_successors = lambda nid: []
    g.get_edge = lambda a, b: None
    return g


def _fake_definition():
    return types.SimpleNamespace(
        id="fake_writer",
        name="写手",
        emoji="✍️",
        role="writer",
        model="deepseek-v4-flash",
        temperature=0.7,
        max_tokens=1000,
        system_prompt="写手",
        tool_ids=["file_write"],
        skill_ids=[],
        tools=[],
        model_profile=types.SimpleNamespace(image=None, video=None, music=None),
        extra={},
    )


def _node():
    return types.SimpleNamespace(
        id="unit_1", type=NodeType.AGENT, agent_id="fake_writer",
        label="unit_1", metadata={"phase_id": "unit_1"},
    )


@pytest.mark.asyncio
async def test_agent_no_output_marks_failed():
    """agent 完成但 result 为空且无落盘产物 → 返回 status:error（节点标 FAILED）+ CRITICAL 降级。"""
    from core.agent.base import BaseAgent
    from core.graph.runtime import NodeExecutor

    run_ctx = RunContext(run_id="r1", project_id="p1", task="t")
    executor = NodeExecutor(graph=_fake_graph())
    executor.set_live_run_ctx(run_ctx)

    fake_def = _fake_definition()
    fake_agent = BaseAgent(definition=fake_def)
    fake_agent.execute = AsyncMock(return_value={"result": "", "raw": ""})
    executor._agent_cache["fake_writer"] = fake_agent

    state = GraphState(values={"project_id": "p1"})
    config = GraphRunConfig(project_id="p1", phase_specs=[])

    from core.vault.project_artifact_service import ArtifactCheckResult

    with patch("core.agent.registry.get_registry") as m_reg, \
         patch("core.vault.project_artifact_service.ProjectArtifactService.check_phase_artifact",
               new=AsyncMock(return_value=ArtifactCheckResult.MISSING)), \
         patch.object(executor, "_build_agent_input", new=AsyncMock(return_value={})), \
         patch.object(executor, "_persist_agent_output", new=AsyncMock(return_value=None)):
        m_reg.return_value.get = AsyncMock(return_value=fake_def)
        result = await executor._execute_agent(_node(), state, config)

    # 不静默放行：返回 status:error → 节点标 FAILED
    assert result.get("status") == "error", f"空产出应标 FAILED，实际 {result}"
    assert result.get("error_kind") == "no_output"

    # 降级已记录（CRITICAL agent_output）
    assert run_ctx.diagnostics is not None
    assert run_ctx.diagnostics["has_critical"] is True
    components = [e["component"] for e in run_ctx.diagnostics["entries"]]
    assert "agent_output" in components


@pytest.mark.asyncio
async def test_agent_with_artifact_not_failed():
    """agent 有落盘产物 → 不标 FAILED、无 CRITICAL 降级。"""
    from core.agent.base import BaseAgent
    from core.graph.runtime import NodeExecutor

    run_ctx = RunContext(run_id="r1", project_id="p1", task="t")
    executor = NodeExecutor(graph=_fake_graph())
    executor.set_live_run_ctx(run_ctx)

    fake_def = _fake_definition()
    fake_agent = BaseAgent(definition=fake_def)
    fake_agent.execute = AsyncMock(return_value={"result": "第一章正文……", "raw": ""})
    executor._agent_cache["fake_writer"] = fake_agent

    state = GraphState(values={"project_id": "p1"})
    config = GraphRunConfig(project_id="p1", phase_specs=[])

    from core.vault.project_artifact_service import ArtifactCheckResult

    with patch("core.agent.registry.get_registry") as m_reg, \
         patch("core.vault.project_artifact_service.ProjectArtifactService.check_phase_artifact",
               new=AsyncMock(return_value=ArtifactCheckResult.PRESENT)), \
         patch.object(executor, "_build_agent_input", new=AsyncMock(return_value={})), \
         patch.object(executor, "_persist_agent_output", new=AsyncMock(return_value=None)):
        m_reg.return_value.get = AsyncMock(return_value=fake_def)
        result = await executor._execute_agent(_node(), state, config)

    assert result.get("status") != "error"
    assert not (run_ctx.diagnostics or {}).get("has_critical", False)


def test_agent_result_has_content_detects_empty():
    """reasoning 空产出检测：空 result / 只有 error 状态不算空。"""
    from core.graph.runtime import agent_result_has_content

    # 空产出（reasoning 预算吃光）
    assert agent_result_has_content({"agent_id": "x", "result": "", "raw": ""}) is False
    assert agent_result_has_content({"agent_id": "x", "result": "  "}) is False
    assert agent_result_has_content(None) is False
    assert agent_result_has_content("") is False
    # 有内容
    assert agent_result_has_content({"agent_id": "x", "result": "第一章正文……"}) is True
    assert agent_result_has_content("正文内容") is True
    # 显式 status:error 不重试（避免掩盖真实错误）
    assert agent_result_has_content({"agent_id": "x", "status": "error", "result": ""}) is True
