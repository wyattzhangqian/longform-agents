"""AgentMatcher — 统一 Agent 匹配 + 历史质量评分 (Phase 1 4.5 + Phase 3 6.3)

评分维度（执行方案 4.5）：
    能力匹配 + 语义匹配 + 领域匹配 + 工具匹配
    + 历史质量表现 + 成本 + 延迟 + 当前可用性

本模块负责**历史质量表现**这一维（能力/语义/领域匹配由 TemplateMatcher 承担）：
读 run_feedback 表聚合每个 agent 的成功率/质量门通过率，应用时间衰减 + 最小样本数，
合成一个 0-1 的历史质量分，再合并进基础匹配分。

历史指标原则（执行方案 4.5）：
- 时间衰减：越新的运行权重越高（指数 half-life）；
- 最小样本数：样本 < min_samples 时向中性 0.5 收缩，避免新 Agent 永远没机会，
  也避免一次成功占据第一名。
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AgentMatch(BaseModel):
    """统一的 Agent 匹配结果"""
    phase_id: str
    agent_id: str
    score: float = 0.0                 # 0-100（与 TemplateMatcher 阈值一致）
    level: str = "none"                # strong/acceptable/weak/none
    reasons: List[str] = Field(default_factory=list)
    missing_capabilities: List[str] = Field(default_factory=list)
    estimated_cost: Optional[float] = None
    estimated_latency: Optional[float] = None


def historical_quality_score(
    stats: Dict[str, Any],
    min_samples: int = 5,
    half_life_days: float = 14.0,
) -> float:
    """历史质量评分（0-1），时间衰减 + 最小样本数（纯函数，可单测）。

    Args:
        stats: {success_rate, quality_gate_pass_rate, sample_count, avg_recency_days}
        min_samples: 达到该样本数后不再向中性收缩
        half_life_days: 时间衰减半衰期（天）

    Returns:
        0-1 的历史质量分；0.5 = 中性（无历史或样本不足）。
    """
    n = int(stats.get("sample_count") or 0)
    if n == 0:
        return 0.5  # 无历史 → 中性，不给新 Agent 惩罚

    base = 0.6 * float(stats.get("success_rate") or 0.0) \
        + 0.4 * float(stats.get("quality_gate_pass_rate") or 0.0)

    # 时间衰减：越新权重越高
    decay = 0.5 ** (float(stats.get("avg_recency_days") or 0.0) / half_life_days)

    # 最小样本数：样本少时向中性 0.5 收缩（置信度）
    confidence = min(1.0, n / min_samples)

    return 0.5 * (1.0 - confidence) + base * decay * confidence


def merge_with_history(
    base_score: float,
    history_score: float,
    history_weight: float = 0.15,
) -> float:
    """把历史质量分（0-1，0.5 中性）合并进基础匹配分（0-100）。

    history_score 偏离 0.5 时线性调整 base_score（weight 控制幅度），
    0.5 中性 → 不调整；1.0 → +weight*100；0.0 → -weight*100。
    """
    adjustment = (history_score - 0.5) * 2.0 * history_weight * 100.0
    return max(0.0, min(100.0, base_score + adjustment))


async def load_agent_stats(agent_id: str) -> Dict[str, Any]:
    """读 run_feedback 表聚合单个 agent 的历史质量指标。

    返回 {sample_count, success_rate, quality_gate_pass_rate, avg_retries,
    avg_duration_s, avg_recency_days}；无记录返回全 0。
    """
    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute(
        """SELECT
               COUNT(*) AS sample_count,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS success_rate,
               SUM(quality_gate_passed) * 1.0 / COUNT(*) AS quality_gate_pass_rate,
               AVG(retries) AS avg_retries,
               AVG(duration_s) AS avg_duration_s,
               AVG(julianday('now') - julianday(created_at)) AS avg_recency_days
           FROM run_feedback WHERE agent_id = ?""",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if not row or row["sample_count"] == 0:
        return {"sample_count": 0, "success_rate": 0.0, "quality_gate_pass_rate": 0.0,
                "avg_retries": 0.0, "avg_duration_s": 0.0, "avg_recency_days": 0.0}
    return {
        "sample_count": int(row["sample_count"]),
        "success_rate": float(row["success_rate"] or 0.0),
        "quality_gate_pass_rate": float(row["quality_gate_pass_rate"] or 0.0),
        "avg_retries": float(row["avg_retries"] or 0.0),
        "avg_duration_s": float(row["avg_duration_s"] or 0.0),
        "avg_recency_days": float(row["avg_recency_days"] or 0.0),
    }


async def score_agent(
    phase_id: str,
    agent_id: str,
    base_score: float,
    history_weight: float = 0.15,
    min_samples: int = 5,
    half_life_days: float = 14.0,
) -> AgentMatch:
    """把历史质量分合并进基础匹配分，输出统一 AgentMatch（接入点）。"""
    stats = await load_agent_stats(agent_id)
    history = historical_quality_score(stats, min_samples=min_samples, half_life_days=half_life_days)
    merged = merge_with_history(base_score, history, history_weight=history_weight)

    level = "none"
    if merged >= 80.0:
        level = "strong"
    elif merged >= 60.0:
        level = "acceptable"
    elif merged >= 40.0:
        level = "weak"

    reasons = []
    if stats["sample_count"] > 0:
        reasons.append(
            f"历史 {stats['sample_count']} 次运行：成功率 {stats['success_rate']:.0%}，"
            f"质量门通过率 {stats['quality_gate_pass_rate']:.0%}"
        )

    return AgentMatch(
        phase_id=phase_id,
        agent_id=agent_id,
        score=round(merged, 1),
        level=level,
        reasons=reasons,
    )
