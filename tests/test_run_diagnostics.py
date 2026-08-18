"""RunDiagnostics 降级追踪 + 节点全超时覆盖测试（2026-08-09 P0）

覆盖：
  1. RunDiagnostics.record/summary/is_degraded/has_critical
  2. RunContext.record_degradation 在线累加 + checkpoint 恢复重建
  3. _resolve_node_timeout 按节点类型解析（AGENT 不重复包裹）
  4. _node_timeout_result QUALITY_GATE 放行、其余标 error
"""

import types

import pytest

import core.run  # noqa: F401 — 先初始化 run 包，规避循环依赖


def _node(node_type, metadata=None):
    return types.SimpleNamespace(type=node_type, metadata=metadata or {}, id="n1")


def _executor():
    from core.graph.runtime import NodeExecutor
    return NodeExecutor(graph=types.SimpleNamespace())


# ── 1. RunDiagnostics 基础 ──

def test_diagnostics_record_and_summary():
    from core.run.diagnostics import RunDiagnostics, Severity

    diag = RunDiagnostics()
    diag.record(Severity.CRITICAL, "skeleton_planner", "骨架规划失败", "无骨架执行")
    diag.record(Severity.INFO, "evolution", "进化日志失败", "")
    s = diag.summary()

    assert s["is_degraded"] is True
    assert s["has_critical"] is True
    assert s["critical_count"] == 1
    assert s["info_count"] == 1
    assert len(s["entries"]) == 2
    assert s["entries"][0]["component"] == "skeleton_planner"


def test_diagnostics_clean_run_not_degraded():
    from core.run.diagnostics import RunDiagnostics, Severity

    assert RunDiagnostics().summary()["is_degraded"] is False
    diag = RunDiagnostics()
    diag.record(Severity.INFO, "evolution", "x", "")
    assert diag.is_degraded is False


def test_diagnostics_warning_counts_as_degraded():
    from core.run.diagnostics import RunDiagnostics, Severity

    diag = RunDiagnostics()
    diag.record(Severity.WARNING, "knowledge", "知识未注入", "")
    assert diag.is_degraded is True
    assert diag.has_critical is False


# ── 2. RunContext.record_degradation ──

def test_run_context_record_degradation():
    from core.run.run_context import RunContext
    from core.run.diagnostics import Severity

    ctx = RunContext(run_id="r1", project_id="p1")
    ctx.record_degradation(Severity.WARNING, "materialize", "兜底推断文件名", "写入推断文件")
    assert ctx.diagnostics is not None
    assert ctx.diagnostics["is_degraded"] is True
    assert ctx.diagnostics["warning_count"] == 1


def test_run_context_record_degradation_accepts_string_severity():
    from core.run.run_context import RunContext

    ctx = RunContext(run_id="r1", project_id="p1")
    ctx.record_degradation("critical", "quality_gate", "质量门超时放行", "放行")
    assert ctx.diagnostics["has_critical"] is True
    assert ctx.diagnostics["entries"][0]["severity"] == "critical"


def test_run_context_rebuild_from_checkpoint():
    """checkpoint 恢复：从 diagnostics 字段重建在线累加器，后续记录不丢失历史。"""
    from core.run.run_context import RunContext

    ctx = RunContext(run_id="r1", project_id="p1")
    ctx.record_degradation("warning", "a", "第一处降级", "")
    dumped = ctx.model_dump()

    restored = RunContext.model_validate(dumped)
    restored.record_degradation("warning", "b", "第二处降级", "")

    assert restored.diagnostics is not None
    assert restored.diagnostics["warning_count"] == 2
    components = [e["component"] for e in restored.diagnostics["entries"]]
    assert "a" in components and "b" in components


# ── 3. 节点超时解析 ──

def test_resolve_node_timeout_agent_not_wrapped():
    """AGENT 已有预算化内部超时，外层不重复包裹（返回 None）。"""
    from core.graph.types import NodeType

    ex = _executor()
    assert ex._resolve_node_timeout(_node(NodeType.AGENT)) is None


def test_resolve_node_timeout_human_not_wrapped():
    """人机节点语义特殊（暂停等用户输入），不设硬超时。"""
    from core.graph.types import NodeType

    ex = _executor()
    assert ex._resolve_node_timeout(_node(NodeType.HUMAN_IN_THE_LOOP)) is None


def test_resolve_node_timeout_quality_gate_default():
    from core.graph.types import NodeType

    ex = _executor()
    assert ex._resolve_node_timeout(_node(NodeType.QUALITY_GATE)) == 300.0


def test_resolve_node_timeout_explicit_metadata_wins():
    from core.graph.types import NodeType

    ex = _executor()
    node = _node(NodeType.QUALITY_GATE, metadata={"timeout_seconds": 60})
    assert ex._resolve_node_timeout(node) == 60.0


# ── 4. 节点超时结果 ──

def test_quality_gate_timeout_result_fails():
    """质量门超时不能 passed=True，必须显式失败（生产级语义）。"""
    from core.graph.types import NodeType

    ex = _executor()
    result = ex._node_timeout_result(_node(NodeType.QUALITY_GATE), 60)
    assert result["passed"] is False
    assert result["status"] == "timeout"
    assert result["failure_code"] == "quality_gate_timeout"
    assert result["timeout"] is True
    assert result["violations_count"] == 0


def test_other_node_timeout_result_marks_error():
    from core.graph.types import NodeType

    ex = _executor()
    result = ex._node_timeout_result(_node(NodeType.ROUTER), 60)
    assert result["status"] == "error"
    assert result["error_kind"] == "timeout"


def test_record_degradation_structured_fields():
    """P0-3：record_degradation 自动填 run_id，并透传 phase/node/code/fallback。"""
    from core.run.run_context import RunContext

    ctx = RunContext(run_id="r_struct", project_id="p1", task="t")
    ctx.record_degradation(
        "critical", "agent_output", "无落盘产物", "节点 FAILED",
        phase_id="unit_1", node_id="n1",
        code="agent_no_output", fallback_used="node_failed",
        source="runtime._execute_agent",
    )
    assert ctx.diagnostics is not None
    entry = ctx.diagnostics["entries"][0]
    assert entry["run_id"] == "r_struct"        # 自动填充
    assert entry["phase_id"] == "unit_1"
    assert entry["node_id"] == "n1"
    assert entry["code"] == "agent_no_output"
    assert entry["fallback_used"] == "node_failed"
    assert entry["source"] == "runtime._execute_agent"
    assert ctx.diagnostics["fallback_count"] == 1
