"""PR-3 Golden Plan 全链路基准 — retry/review/decision/roundtable 控制点不静默降级。

Golden Plan（执行方案 7.2）：
    phase_collect ─┐
                   ├─ phase_synthesize (retry + expected artifact)
    phase_research ┘
                   ↓
    phase_review   (human review)
                   ↓
    phase_decision (decision)

验收：normalize → validate → compile_from_plan → graph 节点/边，控制点完整。
"""
import pytest

from core.run.plan_spec import normalize_plan
from core.orchestration.plan_validator import validate_plan
from core.run.template_compiler import TemplateCompiler


def _golden_phases():
    """两并行 → 汇聚(retry+artifact) → review → decision。"""
    return [
        {"phase_id": "phase_collect", "agent_id": "builtin_researcher", "label": "收集资料"},
        {"phase_id": "phase_research", "agent_id": "builtin_researcher", "label": "深度调研"},
        {
            "phase_id": "phase_synthesize", "agent_id": "builtin_writer", "label": "综合撰写",
            "dependencies": ["phase_collect", "phase_research"],
            "on_complete": "retry",
            "expected_artifacts": ["synthesis.md"],
            "expected_outputs": ["synthesis.md"],
        },
        {
            "phase_id": "phase_review", "agent_id": "builtin_reviewer", "label": "人工审核",
            "dependencies": ["phase_synthesize"],
            "on_complete": "review",
        },
        {
            "phase_id": "phase_decision", "agent_id": "builtin_general", "label": "最终决策",
            "dependencies": ["phase_review"],
            "on_complete": "decision",
        },
    ]


def _golden_layers():
    return [["phase_collect", "phase_research"], ["phase_synthesize"], ["phase_review"], ["phase_decision"]]


def test_golden_plan_validates_clean():
    """golden plan normalize + validate → 结构合法，无 error。"""
    spec = normalize_plan({"phases": _golden_phases(), "execution_layers": _golden_layers()})
    result = validate_plan(spec)
    assert result.valid, result.errors


def test_golden_plan_compiles_control_points():
    """compile_from_plan 后，retry/review/decision 控制点完整（不静默降级）。"""
    graph, specs = TemplateCompiler.compile_from_plan({
        "id": "plan_golden",
        "task": "golden",
        "domain_id": "",
        "mode": "sequential",
        "strategy": "",
        "phases": _golden_phases(),
    })
    node_ids = [n.id for n in graph.nodes]

    # retry 阶段 → 质量门 loop_back（on_fail=retry）
    synth = next(s for s in specs if s.id == "phase_synthesize")
    assert synth.exit.on_fail == "retry"
    assert synth.quality_profile.on_fail == "retry"

    # expected artifact → 注入 artifact_file 质量规则
    rule_types = [r.get("check_type") for r in synth.quality_profile.pack_rules]
    assert "artifact_file" in rule_types

    # review 阶段 → human review 节点
    assert "review_phase_review" in node_ids

    # decision 阶段 → decision 节点
    assert "decision_phase_decision" in node_ids

    # 依赖保留
    assert synth.metadata["dependencies"] == ["phase_collect", "phase_research"]


def test_golden_plan_roundtable_variant():
    """golden plan 的 roundtable 变体 → decision_roundtable + deliberative 协作。"""
    graph, specs = TemplateCompiler.compile_from_plan({
        "id": "plan_golden_rt",
        "task": "golden",
        "domain_id": "",
        "mode": "sequential",
        "strategy": "roundtable",
        "phases": _golden_phases(),
    })
    node_ids = [n.id for n in graph.nodes]
    assert "decision_roundtable" in node_ids
    assert "discussion_loop" in node_ids
    assert all(s.collaboration == "deliberative" for s in specs)


def test_golden_plan_save_normalize_compile_consistent():
    """保存格式 → normalize → compile 三处结构一致（phase_id 与依赖不漂移）。"""
    spec = normalize_plan({"phases": _golden_phases()})
    # 依赖只引用 phase_id
    synth = next(p for p in spec.phases if p.phase_id == "phase_synthesize")
    assert synth.dependencies == ["phase_collect", "phase_research"]

    # 用 normalize 后的 model_dump 重新 compile，phase_id 稳定
    graph, specs = TemplateCompiler.compile_from_plan({
        "id": "plan_golden2",
        "task": "golden",
        "domain_id": "",
        "mode": "sequential",
        "strategy": "",
        "phases": [p.model_dump() for p in spec.phases],
    })
    assert [s.id for s in specs] == ["phase_collect", "phase_research", "phase_synthesize", "phase_review", "phase_decision"]
