"""PR-5 计划语义审查 + 修订测试 — plan_critic.py + plan_refiner.py。

critic 输出结构化问题列表（不修改计划），refiner 生成新 revision。
prompt 构建 + 响应解析是纯函数，可单测；LLM 调用失败降级（空 critique / None）。
"""
import json
import pytest

from core.run.plan_spec import PlanSpec, normalize_plan
from core.orchestration.plan_critic import (
    build_critique_prompt,
    parse_critique_response,
    PlanCritique,
    CritiqueIssue,
)
from core.orchestration.plan_refiner import build_refine_prompt, parse_refine_response


def _spec():
    return normalize_plan({
        "plan_id": "plan_x",
        "task": "调研并写报告",
        "strategy": "dag",
        "phases": [
            {"phase_id": "p1", "agent_id": "a", "label": "调研", "dependencies": []},
            {"phase_id": "p2", "agent_id": "b", "label": "撰写", "dependencies": ["p1"]},
        ],
    })


# ═══════════════════════════════════════════════════════════
# plan_critic
# ═══════════════════════════════════════════════════════════

def test_build_critique_prompt_contains_structure():
    prompt = build_critique_prompt(_spec())
    assert "调研并写报告" in prompt
    assert "p1「调研」" in prompt
    assert "p2「撰写」" in prompt
    assert "missing_step" in prompt   # kind 枚举写进 prompt


def test_parse_critique_response_extracts_issues():
    resp = json.dumps({
        "issues": [
            {"phase_id": "p2", "kind": "missing_step", "message": "缺审核阶段"},
            {"phase_id": "", "kind": "unnecessary_serial", "message": "p1 和 p3 可并行"},
        ],
        "overall": "结构基本合理",
    })
    critique = parse_critique_response(resp)
    assert len(critique.issues) == 2
    assert critique.issues[0].phase_id == "p2"
    assert critique.issues[0].kind == "missing_step"
    assert critique.overall == "结构基本合理"


def test_parse_critique_response_unknown_kind_coerced():
    resp = json.dumps({"issues": [{"phase_id": "p1", "kind": "bogus_kind", "message": "x"}]})
    critique = parse_critique_response(resp)
    assert critique.issues[0].kind == "uncovered_risk"   # 未知类型不静默丢弃


def test_parse_critique_response_invalid_json_returns_empty():
    assert parse_critique_response("not json").issues == []
    assert parse_critique_response("").issues == []


@pytest.mark.asyncio
async def test_critique_plan_failure_returns_empty():
    """LLM 异常 → 空 critique，不阻断。"""
    from core.orchestration.plan_critic import critique_plan

    class Boom:
        async def chat(self, *a, **k):
            raise RuntimeError("boom")

    critique = await critique_plan(_spec(), Boom())
    assert critique.issues == []


# ═══════════════════════════════════════════════════════════
# plan_refiner
# ═══════════════════════════════════════════════════════════

def test_build_refine_prompt_contains_issues():
    critique = PlanCritique(issues=[CritiqueIssue(phase_id="p2", kind="missing_step", message="缺审核")])
    prompt = build_refine_prompt(_spec(), critique)
    assert "缺审核" in prompt
    assert "missing_step" in prompt
    assert "phase_id 保持稳定" in prompt


def test_parse_refine_response_generates_new_revision():
    base = _spec()
    assert base.revision == 1
    resp = json.dumps({"phases": [
        {"phase_id": "p1", "agent_id": "a", "label": "调研", "dependencies": []},
        {"phase_id": "p2", "agent_id": "b", "label": "撰写", "dependencies": ["p1"]},
        {"phase_id": "p3", "agent_id": "c", "label": "审核", "dependencies": ["p2"], "on_complete": "review"},
    ]})
    refined = parse_refine_response(resp, base)
    assert refined is not None
    assert refined.revision == 2            # revision+1
    assert refined.plan_id == "plan_x"      # 计划级字段保留
    assert refined.strategy == "dag"
    assert [p.phase_id for p in refined.phases] == ["p1", "p2", "p3"]


def test_parse_refine_response_rewrites_title_deps_to_id():
    base = _spec()
    # LLM 用 label 做依赖（历史坏习惯），refiner 兜底重写为 phase_id
    resp = json.dumps({"phases": [
        {"phase_id": "p1", "agent_id": "a", "label": "调研", "dependencies": []},
        {"phase_id": "p2", "agent_id": "b", "label": "撰写", "dependencies": ["调研"]},
    ]})
    refined = parse_refine_response(resp, base)
    assert refined.phases[1].dependencies == ["p1"]


def test_parse_refine_response_invalid_returns_none():
    assert parse_refine_response("not json", _spec()) is None
    assert parse_refine_response(json.dumps({"phases": []}), _spec()) is None


@pytest.mark.asyncio
async def test_refine_plan_failure_returns_none():
    """LLM 异常 → None，调用方保留原计划。"""
    from core.orchestration.plan_refiner import refine_plan

    class Boom:
        async def chat(self, *a, **k):
            raise RuntimeError("boom")

    result = await refine_plan(_spec(), PlanCritique(), Boom())
    assert result is None
