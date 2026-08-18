"""质量门控失败后应回环重试 Agent，而非死锁/终止"""

import pytest

from core.graph.builder import GraphBuilder
from core.graph.runtime import GraphRuntime, Router
from core.graph.types import (
    ConditionType,
    EdgeType,
    ExecutionResult,
    GraphCondition,
    GraphRunConfig,
    GraphState,
    NodeStatus,
    NodeType,
)


def _mini_quality_pipeline():
    """agent → quality_gate ↺ agent | pass → next"""
    b = GraphBuilder("test_qg_retry")
    b.agent("characters", agent_id="fixture_agent_comic_character", label="characters")
    b.quality_gate("qg_characters", check_targets=["characters"])
    b.agent("char_ref", agent_id="fixture_agent_comic_char_ref", label="char_ref")
    b.edge("characters", "qg_characters")
    b.loop_back(
        "qg_characters",
        "characters",
        condition=GraphCondition(
            condition_type=ConditionType.EXPRESSION,
            expression="not values.get('quality_passed', True)",
        ),
    )
    b.conditional_edge(
        "qg_characters",
        "char_ref",
        "values.get('quality_passed', True)",
    )
    for n in b._nodes:
        if n.id == "qg_characters":
            n.metadata["phase_id"] = "characters"
            n.metadata["quality_profile"] = {
                "max_retries": 2,
                "phase_id": "characters",
                "pack_rules": [],
            }
        if n.id == "characters":
            n.metadata["phase_id"] = "characters"
    return b.build()


@pytest.mark.asyncio
async def test_quality_fail_requeues_agent_not_deadlock():
    graph = _mini_quality_pipeline()
    runtime = GraphRuntime(graph)
    runtime.config = GraphRunConfig(project_id="proj_test")
    runtime.state = GraphState(values={"project_id": "proj_test"})
    runtime.scheduler.initialize(["characters"])

    runtime.scheduler.completed.add("characters")
    runtime.state.completed_nodes.append("characters")
    runtime.scheduler.completed.add("qg_characters")
    runtime.state.completed_nodes.append("qg_characters")
    runtime.state.set("quality_passed", False)
    runtime.state.set("quality_violations", [
        {"message": "缺少期望产物: characters.json", "severity": "error"},
    ])

    router = Router(graph)
    exec_result = ExecutionResult(
        node_id="qg_characters",
        status=NodeStatus.COMPLETED,
        output={"passed": False},
    )
    next_nodes = await router.resolve("qg_characters", exec_result, runtime.state)
    assert "characters" in next_nodes

    runtime._schedule_next("qg_characters", next_nodes)

    assert "characters" in runtime.scheduler.ready
    assert "characters" not in runtime.scheduler.completed
    assert "qg_characters" not in runtime.scheduler.completed
    assert runtime.state.values.get("quality_retry:qg_characters") == 1
    assert runtime.state.termination_reason is None


@pytest.mark.asyncio
async def test_quality_retry_exhausted_only_in_review_mode():
    graph = _mini_quality_pipeline()
    runtime = GraphRuntime(graph)
    runtime.config = GraphRunConfig(project_id="proj_test", review_mode=True)
    runtime.state = GraphState(values={"project_id": "proj_test"})
    runtime.state.set("quality_retry:qg_characters", 2)
    runtime.state.set("quality_passed", False)
    runtime.state.set("quality_violations", [
        {"message": "缺少期望产物: characters.json", "severity": "error"},
    ])

    runtime._requeue_quality_retry("qg_characters", "characters")

    assert runtime.state.termination_reason == "error"
    assert "characters.json" in runtime.state.termination_message
    assert "characters" not in runtime.scheduler.ready


@pytest.mark.asyncio
async def test_autonomous_mode_retries_within_limit():
    """自主模式下重试次数在限制内时应正常重新入队"""
    graph = _mini_quality_pipeline()
    runtime = GraphRuntime(graph)
    runtime.config = GraphRunConfig(project_id="proj_test", review_mode=False)
    runtime.state = GraphState(values={"project_id": "proj_test"})
    runtime.state.set("quality_retry:qg_characters", 0)  # 未超限
    runtime.state.set("quality_passed", False)
    runtime.state.set("quality_violations", [
        {"message": "缺少期望产物: characters.json", "severity": "error"},
    ])

    runtime._requeue_quality_retry("qg_characters", "characters")

    assert runtime.state.termination_reason is None
    assert "characters" in runtime.scheduler.ready
    assert runtime.state.values.get("quality_retry:qg_characters") == 1


@pytest.mark.asyncio
async def test_autonomous_mode_skips_when_retry_exhausted():
    """自主模式下超过 autonomous_max_retries 后跳过阶段继续执行"""
    graph = _mini_quality_pipeline()
    runtime = GraphRuntime(graph)
    runtime.config = GraphRunConfig(project_id="proj_test", review_mode=False)
    runtime.state = GraphState(values={"project_id": "proj_test"})
    runtime.state.set("quality_retry:qg_characters", 99)  # 远超 max_retries=2
    runtime.state.set("quality_passed", False)

    runtime._requeue_quality_retry("qg_characters", "characters")

    # 跳过阶段：不应设置终止原因，但 agent 和 gate 标记完成，不重新入队
    assert runtime.state.termination_reason is None
    assert "characters" not in runtime.scheduler.ready
    assert "characters" in runtime.scheduler.completed
    assert "qg_characters" in runtime.scheduler.completed
