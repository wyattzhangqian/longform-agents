"""PR-7 局部重规划 diff + 反馈统计测试 — plan_revision.diff + feedback。

diff 输出结构化 revision diff（不静默替换整张图）；高风险变更需用户确认。
feedback 聚合是纯函数。
"""
import pytest

from core.run.plan_spec import normalize_plan
from core.orchestration.plan_revision import diff_plan_revisions, PlanRevisionDiff
from core.orchestration.feedback import (
    RunFeedback,
    PhaseFeedback,
    aggregate_phase_stats,
    record_run_feedback,
)


def _spec(phases, revision=1):
    return normalize_plan({"plan_id": "p", "task": "t", "revision": revision, "phases": phases})


# ═══════════════════════════════════════════════════════════
# diff_plan_revisions
# ═══════════════════════════════════════════════════════════

def test_diff_added_and_removed_phases():
    base = _spec([
        {"phase_id": "p1", "agent_id": "a", "label": "调研"},
        {"phase_id": "p2", "agent_id": "b", "label": "撰写", "dependencies": ["p1"]},
    ])
    new = _spec([
        {"phase_id": "p1", "agent_id": "a", "label": "调研"},
        {"phase_id": "p3", "agent_id": "c", "label": "审核", "dependencies": ["p1"]},
    ], revision=2)
    diff = diff_plan_revisions(base, new)
    assert [p.phase_id for p in diff.added_phases] == ["p3"]
    assert diff.removed_phase_ids == ["p2"]
    assert diff.unchanged_phase_ids == ["p1"]
    assert diff.updated_phases == []
    assert diff.base_revision == 1
    assert diff.new_revision == 2


def test_diff_updated_phase_detected():
    base = _spec([{"phase_id": "p1", "agent_id": "a", "label": "调研"}])
    new = _spec([{"phase_id": "p1", "agent_id": "a", "label": "深度调研"}], revision=2)
    diff = diff_plan_revisions(base, new)
    assert [p.phase_id for p in diff.updated_phases] == ["p1"]
    assert diff.unchanged_phase_ids == []


def test_diff_unchanged_no_change():
    base = _spec([{"phase_id": "p1", "agent_id": "a", "label": "调研"}])
    new = _spec([{"phase_id": "p1", "agent_id": "a", "label": "调研"}], revision=2)
    diff = diff_plan_revisions(base, new)
    assert diff.added_phases == []
    assert diff.removed_phase_ids == []
    assert diff.updated_phases == []
    assert diff.unchanged_phase_ids == ["p1"]


def test_diff_removed_phase_requires_confirmation():
    """删除阶段是不可逆操作 → 需用户确认（P5）。"""
    base = _spec([{"phase_id": "p1", "agent_id": "a"}, {"phase_id": "p2", "agent_id": "b"}])
    new = _spec([{"phase_id": "p1", "agent_id": "a"}], revision=2)
    diff = diff_plan_revisions(base, new)
    assert diff.requires_user_confirmation is True


def test_diff_high_risk_added_requires_confirmation():
    base = _spec([{"phase_id": "p1", "agent_id": "a"}])
    new = _spec([
        {"phase_id": "p1", "agent_id": "a"},
        {"phase_id": "p2", "agent_id": "b", "risk_level": "high"},
    ], revision=2)
    diff = diff_plan_revisions(base, new)
    assert diff.requires_user_confirmation is True


def test_diff_pure_label_change_no_confirmation():
    """仅改 label（非高风险）→ 不需确认。"""
    base = _spec([{"phase_id": "p1", "agent_id": "a", "label": "A"}])
    new = _spec([{"phase_id": "p1", "agent_id": "a", "label": "B"}], revision=2)
    diff = diff_plan_revisions(base, new)
    assert diff.requires_user_confirmation is False
    assert [p.phase_id for p in diff.updated_phases] == ["p1"]


# ═══════════════════════════════════════════════════════════
# feedback 聚合
# ═══════════════════════════════════════════════════════════

def test_aggregate_phase_stats_empty():
    assert aggregate_phase_stats([])["runs"] == 0
    assert aggregate_phase_stats([])["phases"] == 0


def test_aggregate_phase_stats_basic():
    fbs = [
        RunFeedback(plan_id="p", run_id="r1", phase_results=[
            PhaseFeedback(phase_id="a", status="success", quality_gate_passed=True, retries=1, duration_s=10.0),
            PhaseFeedback(phase_id="b", status="failed", quality_gate_passed=False, retries=3, duration_s=30.0),
        ]),
        RunFeedback(plan_id="p", run_id="r2", phase_results=[
            PhaseFeedback(phase_id="a", status="success", quality_gate_passed=True, retries=0, duration_s=8.0),
        ]),
    ]
    stats = aggregate_phase_stats(fbs)
    assert stats["runs"] == 2
    assert stats["phases"] == 3
    assert stats["success_rate"] == round(2 / 3, 4)
    assert stats["quality_gate_pass_rate"] == round(2 / 3, 4)
    assert stats["avg_retries"] == round((1 + 3 + 0) / 3, 4)
    assert stats["avg_duration_s"] == round((10 + 30 + 8) / 3, 4)


@pytest.mark.asyncio
async def test_record_run_feedback_writes_db():
    """record 写 run_feedback 表（test DB，migration 032 建表）。"""
    fb = RunFeedback(plan_id="p", revision=1, run_id="r", phase_results=[
        PhaseFeedback(phase_id="a", status="success"),
    ])
    await record_run_feedback(fb)  # 不抛异常即通过

    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM run_feedback WHERE plan_id = ?", ("p",))
    row = await cursor.fetchone()
    assert row[0] >= 1


@pytest.mark.asyncio
async def test_record_run_feedback_idempotent():
    """同一 run_id 写两次 → 只写一次（幂等，不重复统计）。"""
    import uuid
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    fb = RunFeedback(plan_id="p", revision=1, run_id=run_id, phase_results=[
        PhaseFeedback(phase_id="a", status="success", quality_gate_passed=True),
        PhaseFeedback(phase_id="b", status="failed", quality_gate_passed=False),
    ])
    await record_run_feedback(fb)
    await record_run_feedback(fb)  # 第二次应被幂等跳过

    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM run_feedback WHERE run_id = ?", (run_id,))
    row = await cursor.fetchone()
    assert row[0] == 2  # 只写了一次（2 个 phase），不是 4
