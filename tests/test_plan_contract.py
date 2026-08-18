"""PR-1 计划契约测试 — core/run/plan_spec.py 的 PlanSpec 契约 + normalizer。

Phase 0 核心：Planner 输出 / 旧 plans.phases / SSE pipeline 三种来源，经 normalize_plan
后得到同一份稳定 PlanSpec —— phase_id 稳定、dependencies 只引用 phase_id、字段无损。

本文件只测契约层（normalize + round-trip），不测编译/执行（那是 PR-2/PR-3）。
"""
import json
import pytest

from core.run.plan_spec import (
    PlanSpec,
    PhasePlan,
    PlanConstraints,
    DeliverableSpec,
    normalize_plan,
)


# ═══════════════════════════════════════════════════════════
# test_stream_plan_contains_stable_phase_ids
# ═══════════════════════════════════════════════════════════

def test_stream_plan_contains_stable_phase_ids():
    """SSE pipeline 输出（无 phase_id，dependencies 用 title）→ normalize 后每个 phase 有稳定 ID。"""
    sse_output = {
        "pipeline": [
            {"name": "资料调研", "title": "资料调研", "agent_id": "agent_researcher"},
            {"name": "撰写报告", "title": "撰写报告", "agent_id": "agent_writer"},
        ],
        "strategy": "pipeline",
        "domain_id": "platform:research_report",
    }
    spec = normalize_plan(sse_output)
    assert len(spec.phases) == 2
    for p in spec.phases:
        assert p.phase_id, "每个 phase 必须有稳定 phase_id"
    # phase_id 唯一
    ids = [p.phase_id for p in spec.phases]
    assert len(set(ids)) == len(ids), f"phase_id 必须唯一，got {ids}"


def test_stream_plan_phase_id_stable_across_calls():
    """同一个旧数据 normalize 两次，phase_id 必须一致（确定性，不随调用变化）。"""
    sse_output = {
        "pipeline": [
            {"title": "调研", "agent_id": "agent_researcher"},
            {"title": "写作", "agent_id": "agent_writer", "dependencies": ["调研"]},
        ],
    }
    s1 = normalize_plan(sse_output)
    s2 = normalize_plan(sse_output)
    assert [p.phase_id for p in s1.phases] == [p.phase_id for p in s2.phases]


# ═══════════════════════════════════════════════════════════
# test_dependencies_reference_phase_ids_only
# ═══════════════════════════════════════════════════════════

def test_dependencies_reference_phase_ids_only():
    """dependencies 用 title → normalize 后重写为 phase_id。"""
    legacy = {
        "phases": [
            {"phase_id": "phase_collect", "title": "收集资料"},
            {"phase_id": "phase_research", "title": "深度调研", "dependencies": ["收集资料"]},
            {"phase_id": "phase_write", "title": "撰写", "dependencies": ["收集资料", "深度调研"]},
        ],
    }
    spec = normalize_plan(legacy)
    by_id = {p.phase_id: p for p in spec.phases}
    assert by_id["phase_research"].dependencies == ["phase_collect"]
    assert set(by_id["phase_write"].dependencies) == {"phase_collect", "phase_research"}


def test_dependencies_already_phase_ids_preserved():
    """dependencies 已是 phase_id → 原样保留，不二次改写。"""
    legacy = {
        "phases": [
            {"phase_id": "a", "title": "A"},
            {"phase_id": "b", "title": "B", "dependencies": ["a"]},
        ],
    }
    spec = normalize_plan(legacy)
    assert spec.phases[1].dependencies == ["a"]


# ═══════════════════════════════════════════════════════════
# test_plan_phase_round_trip_preserves_all_fields
# ═══════════════════════════════════════════════════════════

def _full_phase_dict():
    return {
        "phase_id": "phase_research",
        "agent_id": "agent_researcher",
        "label": "资料调研",
        "objective": "完成资料调研",
        "description": "检索并交叉验证核心事实",
        "dependencies": [],
        "expected_inputs": ["brief.md"],
        "expected_outputs": ["research_notes.md"],
        "expected_artifacts": ["research_notes.md"],
        "acceptance_criteria": ["每个核心结论至少两个来源"],
        "dialogue_policy": "shared_thread",
        "on_complete": "retry",
        "collaboration": "pipeline",
        "quality_domain_ids": ["research_quality"],
        "quality_profile": {"rule_set_ref": "research"},
        "knowledge_packs": ["platform:research_report"],
        "knowledge_top_k": 7,
        "tool_policy": "on_demand",
        "tool_bindings": [{"tool": "web_search"}],
        "max_retries": 5,
        "budget": {"timeout": 120},
        "risk_level": "high",
        "metadata": {"custom": "x"},
    }


def test_plan_phase_round_trip_preserves_all_fields():
    """完整 PhasePlan → dump → normalize → load，全字段无损。"""
    raw = _full_phase_dict()
    plan = {"phases": [raw], "strategy": "dag"}
    spec = normalize_plan(plan)
    p = spec.phases[0]

    # 直接字段
    for key in (
        "phase_id", "agent_id", "label", "objective", "description",
        "expected_inputs", "expected_outputs", "expected_artifacts",
        "acceptance_criteria", "dialogue_policy", "on_complete",
        "collaboration", "quality_domain_ids", "quality_profile",
        "knowledge_packs", "knowledge_top_k", "tool_policy",
        "tool_bindings", "max_retries", "budget", "risk_level",
    ):
        assert getattr(p, key) == raw[key], f"字段 {key} 丢失: {getattr(p, key)!r} != {raw[key]!r}"

    # 计划级字段
    assert spec.strategy == "dag"

    # dump 后再 load 仍无损
    dumped = spec.model_dump()
    spec2 = PlanSpec.model_validate(dumped)
    assert spec2.phases[0].expected_artifacts == ["research_notes.md"]
    assert spec2.phases[0].acceptance_criteria == ["每个核心结论至少两个来源"]


def test_plan_spec_top_level_round_trip():
    """PlanSpec 顶层字段（constraints/deliverables/revision）round-trip。"""
    spec = PlanSpec(
        plan_id="plan_x",
        task="写报告",
        domain_id="platform:research_report",
        task_type="research",
        confidence=0.86,
        strategy="roundtable",
        mode="sequential",
        revision=3,
        constraints=PlanConstraints(
            hard=["不超预算"], soft=["优先中文"], assumptions=["数据可得"],
            open_questions=["输出格式"],
        ),
        deliverables=[DeliverableSpec(id="d1", type="report", format="markdown")],
        phases=[PhasePlan(phase_id="p1", agent_id="agent_researcher")],
    )
    dumped = spec.model_dump()
    spec2 = PlanSpec.model_validate(dumped)
    assert spec2.revision == 3
    assert spec2.strategy == "roundtable"
    assert spec2.constraints.hard == ["不超预算"]
    assert spec2.deliverables[0].format == "markdown"


# ═══════════════════════════════════════════════════════════
# test_legacy_plan_is_normalized_without_failure
# ═══════════════════════════════════════════════════════════

def test_legacy_plan_is_normalized_without_failure():
    """旧 plans.phases（缺大量字段、on_complete 用旧值、phases 是 JSON 字符串）→ 不崩且补默认值。"""
    legacy = {
        "task": "写一份产品说明",
        "mode": "sequential",
        "phases": json.dumps([
            {"agent_id": "agent_researcher", "label": "调研"},
            {"title": "撰写", "agent_id": "agent_writer", "on_complete": "review"},
        ]),
        "execution_layers": [],
    }
    spec = normalize_plan(legacy)
    assert len(spec.phases) == 2
    # 每个 phase 补默认值
    for p in spec.phases:
        assert p.dialogue_policy in ("none", "handoff_only", "shared_thread")
        assert p.on_complete in ("continue", "retry", "review", "decision", "revise_loop")
        assert isinstance(p.max_retries, int)
    # 旧值 review 保留（不是空串也不是非法值）
    assert spec.phases[1].on_complete == "review"


def test_legacy_plan_empty_and_missing_keys():
    """空 phases / 缺 phases 键 / phases 为 None → 不崩，返回空 PlanSpec。"""
    for data in ({}, {"phases": None}, {"phases": []}, {"pipeline": []}):
        spec = normalize_plan(data)
        assert spec.phases == []


def test_legacy_invalid_on_complete_coerced():
    """非法 on_complete / dialogue_policy → 降级到合法默认值，不崩。"""
    legacy = {
        "phases": [
            {"phase_id": "p1", "agent_id": "a", "on_complete": "bogus_value", "dialogue_policy": "weird"},
        ],
    }
    spec = normalize_plan(legacy)
    assert spec.phases[0].on_complete == "continue"
    assert spec.phases[0].dialogue_policy == "shared_thread"


def test_normalize_accepts_phaseplan_objects():
    """normalize 接受已构造的 PhasePlan 对象列表（幂等）。"""
    phases = [PhasePlan(phase_id="p1", agent_id="a"), PhasePlan(phase_id="p2", agent_id="b")]
    spec = normalize_plan({"phases": phases})
    assert [p.phase_id for p in spec.phases] == ["p1", "p2"]
