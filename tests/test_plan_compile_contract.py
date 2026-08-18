"""PR-2 编译契约测试 — compile_from_plan 无损构建 PhaseSpec + strategy 贯通。

Phase 0 核心：保存的完整 phase dict（expected_inputs/outputs/artifacts/acceptance/
dialogue_policy/on_complete/dependencies）经 compile_from_plan 转换到 PhaseSpec 后，
字段不丢、控制点（retry/review/decision/roundtable）不静默降级。
"""
import pytest

from core.run.template_compiler import TemplateCompiler
from core.run.phase_spec import PhaseSpec


def _plan_data(phases, strategy="", mode="sequential"):
    return {
        "id": "plan_test",
        "task": "测试任务",
        "domain_id": "",
        "mode": mode,
        "strategy": strategy,
        "phases": phases,
    }


def _compile(phases, strategy="", mode="sequential"):
    graph, specs = TemplateCompiler.compile_from_plan(
        _plan_data(phases, strategy=strategy, mode=mode)
    )
    return graph, specs


# ═══════════════════════════════════════════════════════════
# 字段保真
# ═══════════════════════════════════════════════════════════

def test_compile_from_plan_preserves_dialogue_policy():
    graph, specs = _compile([
        {"phase_id": "p1", "agent_id": "builtin_researcher", "dialogue_policy": "shared_thread"},
    ])
    assert specs[0].dialogue_policy == "shared_thread"


def test_compile_from_plan_preserves_on_complete_retry():
    graph, specs = _compile([
        {"phase_id": "p1", "agent_id": "builtin_researcher", "on_complete": "retry"},
    ])
    assert specs[0].exit.on_fail == "retry"


def test_compile_from_plan_preserves_expected_outputs():
    graph, specs = _compile([
        {"phase_id": "p1", "agent_id": "builtin_researcher", "expected_outputs": ["notes.md"]},
    ])
    assert specs[0].expected_outputs == ["notes.md"]


def test_compile_from_plan_preserves_expected_inputs():
    graph, specs = _compile([
        {"phase_id": "p1", "agent_id": "builtin_researcher", "expected_inputs": ["brief.md"]},
    ])
    assert specs[0].expected_inputs == ["brief.md"]


def test_compile_from_plan_preserves_dependencies():
    graph, specs = _compile([
        {"phase_id": "p1", "agent_id": "builtin_researcher"},
        {"phase_id": "p2", "agent_id": "builtin_writer", "dependencies": ["p1"]},
    ])
    assert specs[1].metadata["dependencies"] == ["p1"]


def test_compile_from_plan_injects_artifact_rule_when_explicit():
    graph, specs = _compile([
        {"phase_id": "p1", "agent_id": "builtin_writer", "expected_artifacts": ["report.md"]},
    ])
    spec = specs[0]
    # 期望产物 → quality_profile 注入 artifact_file 规则 + on_fail=retry
    assert spec.quality_profile.on_fail == "retry"
    rule_types = [r.get("check_type") for r in spec.quality_profile.pack_rules]
    assert "artifact_file" in rule_types
    artifact_rule = next(r for r in spec.quality_profile.pack_rules if r.get("check_type") == "artifact_file")
    assert "report.md" in artifact_rule["config"]["expected_files"]


# ═══════════════════════════════════════════════════════════
# 控制点保真
# ═══════════════════════════════════════════════════════════

def test_compile_from_plan_preserves_review_and_decision():
    graph, specs = _compile([
        {"phase_id": "p1", "agent_id": "builtin_writer", "on_complete": "review"},
        {"phase_id": "p2", "agent_id": "builtin_reviewer", "on_complete": "decision"},
    ])
    assert specs[0].exit.on_fail == "human_review"   # review → human_review（运行时语义）
    assert specs[1].exit.on_fail == "decision"


def test_compile_from_plan_preserves_knowledge_and_tool_fields():
    graph, specs = _compile([
        {
            "phase_id": "p1", "agent_id": "builtin_researcher",
            "knowledge_packs": ["platform:research_report"], "knowledge_top_k": 7,
            "tool_policy": "on_demand", "tool_bindings": [{"tool": "web_search"}],
        },
    ])
    spec = specs[0]
    assert spec.knowledge_profile.packs == ["platform:research_report"]
    assert spec.knowledge_profile.top_k == 7
    assert spec.tool_policy == "on_demand"
    assert spec.tool_bindings == [{"tool": "web_search"}]


# ═══════════════════════════════════════════════════════════
# strategy 贯通
# ═══════════════════════════════════════════════════════════

def test_compile_from_plan_passes_roundtable_strategy():
    graph, specs = _compile(
        [
            {"phase_id": "p1", "agent_id": "builtin_researcher"},
            {"phase_id": "p2", "agent_id": "builtin_writer"},
        ],
        strategy="roundtable",
    )
    node_ids = [n.id for n in graph.nodes]
    # roundtable 编译后必有 decision 节点 + discussion_loop
    assert "decision_roundtable" in node_ids
    assert "discussion_loop" in node_ids
    # 所有 spec 标记为 deliberative 协作
    assert all(s.collaboration == "deliberative" for s in specs)


def test_compile_from_plan_defaults_pipeline_strategy():
    """无 strategy → 走默认 pipeline（不崩）。"""
    graph, specs = _compile(
        [{"phase_id": "p1", "agent_id": "builtin_researcher"}],
        strategy="",
    )
    assert len(specs) == 1


# ═══════════════════════════════════════════════════════════
# 兼容性
# ═══════════════════════════════════════════════════════════

def test_compile_from_plan_legacy_phase_without_phase_id():
    """旧 phase 缺 phase_id → 用 phase_{i} 兜底，不崩。"""
    graph, specs = _compile([
        {"agent_id": "builtin_researcher", "label": "调研"},
    ])
    assert specs[0].id == "phase_0"


def test_compile_from_plan_phase_without_agent_id_falls_back():
    """缺 agent_id → 兜底 builtin_general。"""
    graph, specs = _compile([
        {"phase_id": "p1", "label": "无 Agent 阶段"},
    ])
    assert specs[0].agent_id == "builtin_general"
