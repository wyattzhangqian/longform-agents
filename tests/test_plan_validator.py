"""PR-3 计划结构确定性校验测试 — core/orchestration/plan_validator.py。

validator 只做确定性规则（phase_id 唯一 / 依赖存在 / 无环 / execution_layers 一致 /
agent 存在 / 高风险需确认），不调 LLM。
"""
from core.run.plan_spec import PlanSpec, PhasePlan, normalize_plan
from core.orchestration.plan_validator import validate_plan, ValidationResult


def _spec(phases, execution_layers=None):
    return PlanSpec(
        phases=[PhasePlan(**p) if isinstance(p, dict) else p for p in phases],
        execution_layers=execution_layers or [],
    )


# ═══════════════════════════════════════════════════════════
# 结构校验
# ═══════════════════════════════════════════════════════════

def test_rejects_duplicate_phase_id():
    spec = _spec([
        {"phase_id": "p1", "agent_id": "a"},
        {"phase_id": "p1", "agent_id": "b"},
    ])
    result = validate_plan(spec)
    assert not result.valid
    assert any("重复 phase_id" in e for e in result.errors)


def test_rejects_missing_dependency():
    spec = _spec([
        {"phase_id": "p1", "agent_id": "a"},
        {"phase_id": "p2", "agent_id": "b", "dependencies": ["not_exist"]},
    ])
    result = validate_plan(spec)
    assert not result.valid
    assert any("不存在的 phase" in e for e in result.errors)


def test_rejects_cycle():
    spec = _spec([
        {"phase_id": "p1", "agent_id": "a", "dependencies": ["p2"]},
        {"phase_id": "p2", "agent_id": "b", "dependencies": ["p1"]},
    ])
    result = validate_plan(spec)
    assert not result.valid
    assert any("循环依赖" in e for e in result.errors)


def test_rejects_invalid_execution_layers():
    # execution_layers 缺失 p2
    spec = _spec(
        [
            {"phase_id": "p1", "agent_id": "a"},
            {"phase_id": "p2", "agent_id": "b"},
        ],
        execution_layers=[["p1"]],
    )
    result = validate_plan(spec)
    assert not result.valid
    assert any("缺失 phase" in e for e in result.errors)


def test_rejects_unknown_agent():
    spec = _spec([
        {"phase_id": "p1", "agent_id": "ghost_agent"},
    ])
    result = validate_plan(spec, known_agent_ids={"builtin_researcher", "builtin_writer"})
    assert not result.valid
    assert any("未知 agent" in e for e in result.errors)


def test_requires_confirmation_for_high_risk_change():
    spec = _spec([
        {"phase_id": "p1", "agent_id": "a", "risk_level": "high"},
    ])
    result = validate_plan(spec)
    assert result.valid  # 结构合法
    assert result.requires_confirmation


# ═══════════════════════════════════════════════════════════
# 合法计划不误报
# ═══════════════════════════════════════════════════════════

def test_valid_plan_passes():
    spec = _spec(
        [
            {"phase_id": "p1", "agent_id": "a"},
            {"phase_id": "p2", "agent_id": "b", "dependencies": ["p1"]},
            {"phase_id": "p3", "agent_id": "c", "dependencies": ["p1"]},
        ],
        execution_layers=[["p1"], ["p2", "p3"]],
    )
    result = validate_plan(spec)
    assert result.valid, result.errors
    assert not result.requires_confirmation


def test_valid_plan_with_parallel_converge():
    """两阶段并行 + 汇聚（golden plan 的合法子结构）不误报。"""
    spec = _spec([
        {"phase_id": "collect", "agent_id": "a"},
        {"phase_id": "research", "agent_id": "b"},
        {"phase_id": "synthesize", "agent_id": "c", "dependencies": ["collect", "research"]},
    ])
    result = validate_plan(spec)
    assert result.valid, result.errors


def test_normalize_then_validate_round_trip():
    """normalize 后再 validate：title 依赖重写后依赖合法。"""
    spec = normalize_plan({
        "phases": [
            {"phase_id": "p1", "title": "调研"},
            {"phase_id": "p2", "title": "写作", "dependencies": ["调研"]},
        ],
    })
    result = validate_plan(spec)
    assert result.valid, result.errors
    assert spec.phases[1].dependencies == ["p1"]
