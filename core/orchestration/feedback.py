"""Feedback — 运行结果回流与统计 (Phase 3)

每次运行记录阶段成功/失败、质量门结果、重试、耗时、失败原因。Planner 只读取
**聚合指标**（成功率/质量门通过率/平均重试/平均耗时），不把任意历史输出塞进 prompt
（执行方案 6.3：历史反馈必须脱敏聚合，时间衰减和最小样本数由 agent_matcher 调用方处理）。

聚合是纯函数（可单测）；record 写 run_feedback 表。
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PhaseFeedback(BaseModel):
    """单个阶段的运行结果"""
    phase_id: str
    agent_id: str = ""               # 执行该阶段的 agent（用于按 agent 聚合历史指标）
    status: str = "success"          # success / failed / partial
    quality_gate_passed: bool = True
    retries: int = 0
    duration_s: float = 0.0
    failure_reason: str = ""


class RunFeedback(BaseModel):
    """一次运行的回流记录"""
    plan_id: str
    revision: int = 1
    run_id: str = ""
    phase_results: List[PhaseFeedback] = Field(default_factory=list)
    user_modified: bool = False
    final_rating: Optional[int] = None   # 1-5


def aggregate_phase_stats(feedbacks: List[RunFeedback]) -> Dict[str, Any]:
    """聚合多个 run 的指标（纯函数，可单测）。

    返回基础聚合：run 数 / phase 总数 / 成功率 / 质量门通过率 / 平均重试 / 平均耗时。
    时间衰减与最小样本数不在此处（避免把一次性成功当第一名，见 agent_matcher）。
    """
    phase_results = [p for fb in feedbacks for p in fb.phase_results]
    if not phase_results:
        return {"runs": 0, "phases": 0, "success_rate": 0.0, "quality_gate_pass_rate": 0.0,
                "avg_retries": 0.0, "avg_duration_s": 0.0}

    total = len(phase_results)
    success = sum(1 for p in phase_results if p.status == "success")
    qg_pass = sum(1 for p in phase_results if p.quality_gate_passed)
    retries = sum(p.retries for p in phase_results)
    duration = sum(p.duration_s for p in phase_results)

    return {
        "runs": len(feedbacks),
        "phases": total,
        "success_rate": round(success / total, 4),
        "quality_gate_pass_rate": round(qg_pass / total, 4),
        "avg_retries": round(retries / total, 4),
        "avg_duration_s": round(duration / total, 4),
    }


async def record_run_feedback(fb: RunFeedback) -> None:
    """记录一次运行的反馈到 run_feedback 表（失败不阻断主流程；同一 run_id 幂等）。"""
    import uuid
    try:
        from core.storage.database import get_db
        db = await get_db()
        # 幂等：同一 run_id 只写一次（避免重复统计）
        if fb.run_id:
            cursor = await db.execute(
                "SELECT 1 FROM run_feedback WHERE run_id = ? LIMIT 1",
                (fb.run_id,),
            )
            if await cursor.fetchone():
                return
        for p in fb.phase_results:
            await db.execute(
                """INSERT INTO run_feedback
                   (id, plan_id, revision, run_id, phase_id, agent_id, status, quality_gate_passed,
                    retries, duration_s, failure_reason, user_modified, final_rating)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"fb_{uuid.uuid4().hex[:12]}",
                    fb.plan_id, fb.revision, fb.run_id, p.phase_id, p.agent_id, p.status,
                    1 if p.quality_gate_passed else 0,
                    p.retries, p.duration_s, p.failure_reason,
                    1 if fb.user_modified else 0, fb.final_rating,
                ),
            )
        await db.commit()
    except Exception:
        pass  # 反馈记录非致命，失败静默（诊断层可另接）
