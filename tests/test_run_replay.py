"""Checkpoint Replay 单元测试

覆盖场景：
1. 正常回放 — reset_from_phase 正确重置目标+下游，保留上游
2. 中间节点回放 — 从非首个 phase 回放
3. 带 modifications — inject_phase_context 注入 revision context
4. 无效 phase_id 报错
5. _collect_downstream 正确收集下游节点
6. StateManager.partial_reset — 部分重置持久化状态
7. RunContext.inject_phase_context / get_phase_context
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.graph.builder import GraphBuilder
from core.graph.types import WorkflowGraph, NodeType, GraphRunConfig, GraphState
from core.run.run_context import RunContext
from core.run.collaboration_graph import CollaborationGraph


def _build_test_graph() -> WorkflowGraph:
    """构建测试用 3-agent 顺序 pipeline:
    agent_a → qg_a → agent_b → qg_b → agent_c → qg_c
    """
    builder = GraphBuilder("test_replay")
    builder.agent("agent_a", agent_id="agent_a", label="Writer")
    builder.agent("agent_b", agent_id="agent_b", label="Editor")
    builder.agent("agent_c", agent_id="agent_c", label="Reviewer")
    builder.edge("agent_a", "agent_b")
    builder.edge("agent_b", "agent_c")
    return builder.build()


def _make_collab(graph: WorkflowGraph, completed: list = None) -> CollaborationGraph:
    """构建测试用 CollaborationGraph"""
    ctx = RunContext(
        run_id="test_run_001",
        project_id="test_project",
        task="Test task",
        phase_specs=[
            {"id": "agent_a", "agent_id": "agent_a", "label": "Writer"},
            {"id": "agent_b", "agent_id": "agent_b", "label": "Editor"},
            {"id": "agent_c", "agent_id": "agent_c", "label": "Reviewer"},
        ],
        config_snapshot={"mode": "sequential"},
    )
    ctx.completed_phase_ids = completed or []
    return CollaborationGraph(graph, [], ctx, mode="sequential")


# ============================================================
# 1. 正常回放 — reset_from_phase 重置目标+下游，保留上游
# ============================================================

def test_reset_from_phase_resets_target_and_downstream():
    """从 agent_b 回放 → agent_b 和 agent_c 被重置，agent_a 保留"""
    graph = _build_test_graph()
    collab = _make_collab(graph, completed=["agent_a", "agent_b", "agent_c"])

    # 模拟 agent outputs
    collab.run_context.set_agent_result("agent_a", {"text": "written"})
    collab.run_context.set_agent_result("agent_b", {"text": "edited"})
    collab.run_context.set_agent_result("agent_c", {"text": "reviewed"})

    reset_nodes = collab.reset_from_phase("agent_b")

    assert "agent_b" in reset_nodes
    assert "agent_c" in reset_nodes
    assert "agent_a" not in reset_nodes

    # agent_a 保留
    assert "agent_a" in collab.run_context.agent_outputs
    assert "agent_a" in collab.run_context.completed_phase_ids

    # agent_b 和 agent_c 被重置
    assert "agent_b" not in collab.run_context.agent_outputs
    assert "agent_c" not in collab.run_context.agent_outputs
    assert "agent_b" not in collab.run_context.completed_phase_ids
    assert "agent_c" not in collab.run_context.completed_phase_ids


# ============================================================
# 2. 中间节点回放
# ============================================================

def test_reset_from_first_phase():
    """从 agent_a 回放 → 全部被重置"""
    graph = _build_test_graph()
    collab = _make_collab(graph, completed=["agent_a", "agent_b", "agent_c"])
    collab.run_context.set_agent_result("agent_a", {"text": "written"})
    collab.run_context.set_agent_result("agent_b", {"text": "edited"})

    reset_nodes = collab.reset_from_phase("agent_a")

    assert "agent_a" in reset_nodes
    assert "agent_b" in reset_nodes
    assert "agent_c" in reset_nodes
    assert len(collab.run_context.agent_outputs) == 0
    assert len(collab.run_context.completed_phase_ids) == 0


def test_reset_from_last_phase():
    """从 agent_c 回放 → 只有 agent_c 被重置"""
    graph = _build_test_graph()
    collab = _make_collab(graph, completed=["agent_a", "agent_b", "agent_c"])
    collab.run_context.set_agent_result("agent_a", {"text": "written"})
    collab.run_context.set_agent_result("agent_b", {"text": "edited"})
    collab.run_context.set_agent_result("agent_c", {"text": "reviewed"})

    reset_nodes = collab.reset_from_phase("agent_c")

    assert reset_nodes == ["agent_c"]
    assert "agent_a" in collab.run_context.agent_outputs
    assert "agent_b" in collab.run_context.agent_outputs
    assert "agent_c" not in collab.run_context.agent_outputs


# ============================================================
# 3. 带 modifications — inject_phase_context
# ============================================================

def test_inject_phase_context():
    """inject_phase_context 正确注入 revision context"""
    graph = _build_test_graph()
    collab = _make_collab(graph, completed=["agent_a", "agent_b"])
    collab.run_context.set_agent_result("agent_b", {"text": "original output"})

    collab.run_context.inject_phase_context("agent_b", "Make it more concise")

    ctx = collab.run_context.get_phase_context("agent_b")
    assert ctx is not None
    assert ctx["mode"] == "revision"
    assert ctx["instructions"] == "Make it more concise"
    assert "original output" in ctx["previous_output"]


def test_inject_phase_context_no_previous_output():
    """无先前输出时 previous_output 为空"""
    graph = _build_test_graph()
    collab = _make_collab(graph, completed=[])

    collab.run_context.inject_phase_context("agent_a", "Start fresh")

    ctx = collab.run_context.get_phase_context("agent_a")
    assert ctx is not None
    assert ctx["instructions"] == "Start fresh"
    assert ctx["previous_output"] == ""


def test_get_phase_context_none():
    """无注入时 get_phase_context 返回 None"""
    graph = _build_test_graph()
    collab = _make_collab(graph)

    assert collab.run_context.get_phase_context("agent_a") is None


# ============================================================
# 4. 无效 phase_id 报错
# ============================================================

def test_reset_from_invalid_phase_raises():
    """无效 phase_id → ValueError"""
    graph = _build_test_graph()
    collab = _make_collab(graph)

    with pytest.raises(ValueError, match="not found"):
        collab.reset_from_phase("nonexistent_phase")


# ============================================================
# 5. _collect_downstream 正确收集下游节点
# ============================================================

def test_collect_downstream():
    """BFS 正确收集所有下游节点"""
    graph = _build_test_graph()
    collab = _make_collab(graph)

    # 从 agent_a 开始（含自身）
    downstream = collab._collect_downstream("agent_a", include_self=True)
    assert downstream == {"agent_a", "agent_b", "agent_c"}

    # 从 agent_b 开始（含自身）
    downstream = collab._collect_downstream("agent_b", include_self=True)
    assert downstream == {"agent_b", "agent_c"}

    # 从 agent_c 开始（不含自身）
    downstream = collab._collect_downstream("agent_c", include_self=False)
    assert downstream == set()

    # 从 agent_c 开始（含自身）
    downstream = collab._collect_downstream("agent_c", include_self=True)
    assert downstream == {"agent_c"}


# ============================================================
# 6. StateManager.partial_reset
# ============================================================

@pytest.mark.asyncio
async def test_partial_reset():
    """StateManager.partial_reset 部分重置持久化状态"""
    from core.agent.state_manager import StateManager

    # Mock load_run_context 和 save_run_context
    mock_ctx = RunContext(
        run_id="test",
        project_id="proj_1",
        task="task",
        completed_phase_ids=["phase_1", "phase_2", "phase_3"],
    )
    mock_ctx.set_agent_result("phase_1", {"text": "out1"})
    mock_ctx.set_agent_result("phase_2", {"text": "out2"})
    mock_ctx.set_agent_result("phase_3", {"text": "out3"})

    with patch.object(StateManager, 'load_run_context', new_callable=AsyncMock, return_value=mock_ctx):
        with patch.object(StateManager, 'save_run_context', new_callable=AsyncMock) as mock_save:
            await StateManager.partial_reset("proj_1", {"phase_2", "phase_3"})

            # 验证 phase_1 保留，phase_2/3 被删除
            assert "phase_1" in mock_ctx.completed_phase_ids
            assert "phase_2" not in mock_ctx.completed_phase_ids
            assert "phase_3" not in mock_ctx.completed_phase_ids
            assert "phase_1" in mock_ctx.agent_outputs
            assert "phase_2" not in mock_ctx.agent_outputs
            assert "phase_3" not in mock_ctx.agent_outputs

            # 验证 save 被调用
            mock_save.assert_called_once()


@pytest.mark.asyncio
async def test_partial_reset_no_checkpoint():
    """无 checkpoint 时 partial_reset 不报错"""
    from core.agent.state_manager import StateManager

    with patch.object(StateManager, 'load_run_context', new_callable=AsyncMock, return_value=None):
        # 不应抛出异常
        await StateManager.partial_reset("nonexistent", {"phase_1"})


# ============================================================
# 7. ReplayStartedEvent
# ============================================================

def test_replay_started_event():
    """ReplayStartedEvent 正确序列化"""
    from core.run.events import ReplayStartedEvent

    event = ReplayStartedEvent(
        run_id="replay_001",
        from_phase_id="agent_b",
        phases_to_rerun=["agent_b", "agent_c"],
    )
    assert event.event_type == "replay_started"
    assert event.run_id == "replay_001"
    assert event.from_phase_id == "agent_b"
    assert event.phases_to_rerun == ["agent_b", "agent_c"]

    # 序列化
    data = event.model_dump()
    assert data["event_type"] == "replay_started"
    assert data["phases_to_rerun"] == ["agent_b", "agent_c"]
