"""质量门超时语义（2026-08-10 P0-1）— 超时不能伪装成 passed，必须显式失败。

生产级原则：宁可显式 FAILED，也不要把失败伪装成成功。
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from core.graph.builder import GraphBuilder
from core.graph.runtime import GraphRuntime
from core.graph.types import GraphRunConfig, GraphState, NodeStatus


async def _hang_forever(*args, **kwargs):
    """挂起直到被取消（wait_for 超时会 cancel 它）。"""
    await asyncio.Event().wait()
    raise AssertionError("不应走到这里")


def _tiny_pipeline(qg_timeout: float):
    """agent → quality_gate（超时可配）"""
    b = GraphBuilder("test_qg_timeout")
    b.agent("writer", agent_id="builtin_writer", label="writer")
    b.quality_gate("qg", check_targets=["writer"])
    b.edge("writer", "qg")
    for n in b._nodes:
        if n.id == "writer":
            n.metadata["phase_id"] = "unit_1"
        if n.id == "qg":
            n.metadata["phase_id"] = "unit_1"
            n.metadata["timeout_seconds"] = qg_timeout
    return b.build()


@pytest.mark.asyncio
async def test_quality_gate_timeout_node_is_failed():
    """质量门节点执行超时 → 节点 FAILED（不是 COMPLETED），result.passed=False。"""
    graph = _tiny_pipeline(qg_timeout=0.1)
    runtime = GraphRuntime(graph)
    runtime.config = GraphRunConfig(project_id="proj_t", phase_specs=[])
    runtime.state = GraphState(values={"project_id": "proj_t"})
    runtime.scheduler.initialize(["writer"])

    # 让质量门执行挂起 → 外层 wait_for 超时
    with patch.object(
        runtime.node_executor, "_execute_quality_gate",
        new=_hang_forever,
    ):
        exec_result = await runtime.node_executor.execute(
            graph.get_node("qg"), runtime.state, runtime.config,
        )

    assert exec_result.status == NodeStatus.FAILED, f"质量门超时应标 FAILED，实际 {exec_result.status}"
    out = exec_result.output or {}
    assert out.get("passed") is False
    assert out.get("failure_code") == "quality_gate_timeout"


@pytest.mark.asyncio
async def test_quality_gate_timeout_emits_event():
    """质量门超时必发 node_timeout SSE 事件。"""
    graph = _tiny_pipeline(qg_timeout=0.1)
    runtime = GraphRuntime(graph)
    runtime.config = GraphRunConfig(project_id="proj_t", phase_specs=[])
    runtime.state = GraphState(values={"project_id": "proj_t"})
    runtime.scheduler.initialize(["writer"])

    events = []
    runtime.node_executor.set_event_emitter(lambda ev, data: events.append((ev, data)))

    with patch.object(
        runtime.node_executor, "_execute_quality_gate",
        new=_hang_forever,
    ):
        await runtime.node_executor.execute(
            graph.get_node("qg"), runtime.state, runtime.config,
        )

    assert any(ev == "node_timeout" for ev, _ in events), f"应发 node_timeout 事件，实际 {events}"
