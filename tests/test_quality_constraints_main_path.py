"""P0-3 修复测试：协商约束覆盖校验接入主质量路径（DbQualityGateway）。

修复前 evaluate_constraints_async 只在无 domain_id 的回退路径执行，
带 domain_id 的主流（DbQualityGateway）完全旁路 constraints_coverage。
本测试验证：有 negotiation_ledger 时，主路径会执行约束校验并把违规并入、
重算通过态。
"""
import pytest
from unittest.mock import AsyncMock, patch

from core.graph.runtime import GraphRuntime
from core.graph.types import (
    ExecutionResult,
    GraphNode,
    GraphRunConfig,
    GraphState,
    NodeStatus,
    NodeType,
    WorkflowGraph,
)
from core.run.run_context import NegotiationEntry, RunContext


class _FakeReport:
    overall_passed = True
    violations = []

    def to_dict(self):
        return {"overall_passed": True, "violations": []}


def _make_fixture(constraint_text: str = "必须包含数据来源"):
    """构造主路径质量门测试环境。"""
    graph = WorkflowGraph(graph_id="p03_test")
    runtime = GraphRuntime(graph)
    # 测试环境不需要真实 SSE 广播（NodeExecutor._emit）
    runtime.node_executor.set_event_emitter(lambda *a, **k: None)

    ctx = RunContext(project_id="p03_proj", task="测试")
    ctx.negotiation_ledger.append(NegotiationEntry(
        id="c1", kind="constraint", text=constraint_text,
        status="confirmed", phase_id="phase_1",
    ))
    # 模拟运行中 live ctx
    runtime.node_executor.set_live_run_ctx(ctx)

    config = GraphRunConfig(project_id="p03_proj")
    runtime.config = config
    # 记录 SSE 发射（P1-9 单发断言用）：no-op 但捕获 quality_check
    captured: list = []
    runtime.node_executor.set_event_emitter(
        lambda et, data: captured.append((et, data)) if et == "quality_check" else None
    )
    _make_fixture.captured = captured

    node = GraphNode(
        id="gate", type=NodeType.QUALITY_GATE,
        metadata={
            "quality_check_targets": ["agent_x"],
            "phase_id": "phase_1",
            "domain_id": "web_novel",
        },
    )
    state = GraphState(graph_id="p03_test")
    state.set_node_output(
        "agent_x",
        ExecutionResult(node_id="agent_x", output="报告正文，含数据来源。", status=NodeStatus.COMPLETED),
    )
    return runtime, node, state, config


@pytest.mark.asyncio
async def test_main_path_runs_constraints_check_and_fails():
    """主路径（带 domain_id）在有协商台账时执行约束校验；error 级违规 → 门禁不通过。"""
    runtime, node, state, config = _make_fixture()

    with patch(
        "core.gateway.db_quality_gateway.DbQualityGateway.check",
        new=AsyncMock(return_value=_FakeReport()),
    ) as m_check, \
         patch(
             "core.run.negotiation.evaluate_constraints_async",
             new=AsyncMock(return_value=[{
                 "rule_id": "constraints_coverage",
                 "severity": "error",
                 "message": "未覆盖已确认约束: 必须包含数据来源",
                 "fix_hint": "补充数据来源",
             }]),
         ) as m_constraints, \
         patch(
             "core.run.collaboration_hooks.on_graph_status",
             new=AsyncMock(),
         ):
        result = await runtime.node_executor._execute_quality_gate(node, state, config)

    # 约束校验被调用（主路径不再旁路）
    m_constraints.assert_awaited_once()
    # 违规并入质量门禁
    assert state.get("quality_violations")
    assert any(v.get("rule_id") == "constraints_coverage" for v in state.get("quality_violations"))
    # error 级约束违规 → 门禁不通过
    assert state.get("quality_passed") is False
    assert result["passed"] is False


@pytest.mark.asyncio
async def test_main_path_no_ledger_skips_constraints():
    """无协商台账时主路径跳过约束校验（不误报）。"""
    runtime, node, state, config = _make_fixture()
    runtime.node_executor._live_run_ctx.negotiation_ledger = []

    with patch(
        "core.gateway.db_quality_gateway.DbQualityGateway.check",
        new=AsyncMock(return_value=_FakeReport()),
    ) as m_check, \
         patch(
             "core.run.negotiation.evaluate_constraints_async",
             new=AsyncMock(return_value=[]),
         ) as m_constraints, \
         patch(
             "core.run.collaboration_hooks.on_graph_status",
             new=AsyncMock(),
         ):
        result = await runtime.node_executor._execute_quality_gate(node, state, config)

    m_constraints.assert_not_called()
    assert result["passed"] is True


@pytest.mark.asyncio
async def test_main_path_passes_when_constraints_ok():
    """约束校验通过（无 error 违规）时门禁保持通过。"""
    runtime, node, state, config = _make_fixture()

    with patch(
        "core.gateway.db_quality_gateway.DbQualityGateway.check",
        new=AsyncMock(return_value=_FakeReport()),
    ), \
         patch(
             "core.run.negotiation.evaluate_constraints_async",
             new=AsyncMock(return_value=[{
                 "rule_id": "constraints_coverage",
                 "severity": "warning",  # 仅警告，不算不通过
                 "message": "部分约束未显式说明",
             }]),
         ) as m_constraints, \
         patch(
             "core.run.collaboration_hooks.on_graph_status",
             new=AsyncMock(),
         ):
        result = await runtime.node_executor._execute_quality_gate(node, state, config)

    m_constraints.assert_awaited_once()
    assert result["passed"] is True


@pytest.mark.asyncio
async def test_quality_check_emitted_exactly_once_with_shape():
    """P1-9: 同一质量门禁 SSE quality_check 只发一次，payload 结构完整（无双发）。"""
    runtime, node, state, config = _make_fixture()

    with patch(
        "core.gateway.db_quality_gateway.DbQualityGateway.check",
        new=AsyncMock(return_value=_FakeReport()),
    ), \
         patch(
             "core.run.negotiation.evaluate_constraints_async",
             new=AsyncMock(return_value=[]),
         ), \
         patch(
             "core.run.collaboration_hooks.on_graph_status",
             new=AsyncMock(),
         ):
        result = await runtime.node_executor._execute_quality_gate(node, state, config)

    # 恰好 1 次 quality_check（修复前 db_quality_gateway 内部 + graph 层各发一次）
    qc = [d for et, d in _make_fixture.captured if et == "quality_check"]
    assert len(qc) == 1
    payload = qc[0]
    assert payload["phase_id"] == "phase_1"
    assert payload["agent_id"] == "agent_x"
    assert payload["passed"] is True
    assert "violations_count" in payload
    assert "severity" in payload
    # 主路径附带 domain_id / report
    assert payload["domain_id"] == "web_novel"
    assert "report" in payload
