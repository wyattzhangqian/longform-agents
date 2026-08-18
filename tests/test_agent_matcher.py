"""agent_matcher 历史质量评分测试 — 时间衰减 + 最小样本数。

纯函数（historical_quality_score / merge_with_history）可单测；
load_agent_stats / score_agent 读 run_feedback 表（test DB）。
"""
import pytest

from core.orchestration.agent_matcher import (
    historical_quality_score,
    merge_with_history,
    load_agent_stats,
    score_agent,
    AgentMatch,
)


# ═══════════════════════════════════════════════════════════
# historical_quality_score（纯函数）
# ═══════════════════════════════════════════════════════════

def test_no_history_neutral():
    assert historical_quality_score({"sample_count": 0}) == 0.5


def test_insufficient_samples_shrink_to_neutral():
    """样本不足 → 向 0.5 收缩，避免一次成功占第一。"""
    # n=1, min_samples=5 → confidence=0.2
    # 0.5*0.8 + 1.0*0.2 = 0.6（不是 1.0）
    score = historical_quality_score(
        {"sample_count": 1, "success_rate": 1.0, "quality_gate_pass_rate": 1.0, "avg_recency_days": 0.0},
        min_samples=5,
    )
    assert score == pytest.approx(0.6)


def test_sufficient_samples_full_confidence():
    """样本充足 + 全成功 → 高分（不受收缩影响）。"""
    score = historical_quality_score(
        {"sample_count": 10, "success_rate": 1.0, "quality_gate_pass_rate": 1.0, "avg_recency_days": 0.0},
        min_samples=5,
    )
    assert score == pytest.approx(1.0)


def test_time_decay_halves_at_half_life():
    """时间衰减：avg_recency_days=half_life → 衰减 0.5。"""
    score = historical_quality_score(
        {"sample_count": 10, "success_rate": 1.0, "quality_gate_pass_rate": 1.0, "avg_recency_days": 14.0},
        min_samples=5, half_life_days=14.0,
    )
    assert score == pytest.approx(0.5)   # 1.0 * 0.5 * 1.0


def test_history_weighted_success_and_quality_gate():
    """基础分 = 0.6*success + 0.4*qg。"""
    score = historical_quality_score(
        {"sample_count": 10, "success_rate": 0.8, "quality_gate_pass_rate": 1.0, "avg_recency_days": 0.0},
        min_samples=5,
    )
    assert score == pytest.approx(0.6 * 0.8 + 0.4 * 1.0)


# ═══════════════════════════════════════════════════════════
# merge_with_history（纯函数）
# ═══════════════════════════════════════════════════════════

def test_merge_neutral_no_adjust():
    assert merge_with_history(80.0, 0.5) == pytest.approx(80.0)


def test_merge_high_history_boost():
    # 1.0 → +0.15*2*100*... 实际 +0.15*100 = +15
    assert merge_with_history(70.0, 1.0, 0.15) == pytest.approx(85.0)


def test_merge_low_history_penalty():
    assert merge_with_history(70.0, 0.0, 0.15) == pytest.approx(55.0)


def test_merge_clamped_to_0_100():
    assert merge_with_history(5.0, 0.0, 0.15) == pytest.approx(0.0)
    assert merge_with_history(95.0, 1.0, 0.15) == pytest.approx(100.0)


# ═══════════════════════════════════════════════════════════
# load_agent_stats / score_agent（读表）
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_load_agent_stats_empty():
    stats = await load_agent_stats("agent_no_history")
    assert stats["sample_count"] == 0


@pytest.mark.asyncio
async def test_score_agent_no_history_neutral():
    """无历史的 agent → 中性分，不调整基础分。"""
    match = await score_agent("phase_x", "agent_no_history", base_score=80.0)
    assert match.score == pytest.approx(80.0)
    assert match.level == "strong"
    assert match.reasons == []   # 无历史 → 无理由


@pytest.mark.asyncio
async def test_score_agent_with_history_boosts():
    """有历史且全成功的 agent → 加分 + 理由。"""
    from core.orchestration.feedback import RunFeedback, PhaseFeedback, record_run_feedback
    await record_run_feedback(RunFeedback(
        plan_id="p", revision=1, run_id="r",
        phase_results=[
            PhaseFeedback(phase_id="a", agent_id="agent_good", status="success", quality_gate_passed=True),
            PhaseFeedback(phase_id="b", agent_id="agent_good", status="success", quality_gate_passed=True),
            PhaseFeedback(phase_id="c", agent_id="agent_good", status="success", quality_gate_passed=True),
            PhaseFeedback(phase_id="d", agent_id="agent_good", status="success", quality_gate_passed=True),
            PhaseFeedback(phase_id="e", agent_id="agent_good", status="success", quality_gate_passed=True),
        ],
    ))
    match = await score_agent("phase_x", "agent_good", base_score=70.0)
    assert match.score > 70.0          # 全成功历史 → 加分
    assert any("成功率" in r for r in match.reasons)
