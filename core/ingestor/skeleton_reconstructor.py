"""骨架反推 — 从已有内容反向构建骨架（续写模式使用）"""
from __future__ import annotations
from typing import Optional
from core.skeleton.models import Skeleton, ContinuityThread, UnitSpec
from core.skeleton.planner import SkeletonPlanner


class SkeletonReconstructor:
    """从已有内容分析结果反推骨架，然后调用 SkeletonPlanner.plan_continue() 续接"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    async def reconstruct_and_continue(
        self,
        analysis: dict,
        completed_units: int,
        total_units: int,
        content_type: str = "novel",
        intent_continuation: str = "",
    ) -> Optional[Skeleton]:
        """基于内容分析结果构建初始骨架，然后为后续单元续接规划。"""
        skeleton = Skeleton(
            content_type=content_type,
            total_units=total_units,
            premise=analysis.get("summary", intent_continuation),
        )

        # 用分析提取的线索初始化
        for ct in analysis.get("continuity_threads", []):
            try:
                skeleton.continuity_threads.append(ContinuityThread(**ct))
            except Exception:
                pass

        # 标记已完成单元
        for s in analysis.get("unit_summaries", []):
            try:
                skeleton.units.append(UnitSpec(
                    unit_number=s.get("unit_number", 1),
                    goal=str(s.get("summary", ""))[:100],
                    pacing="exposition",
                ))
            except Exception:
                pass

        # 续接规划
        planner = SkeletonPlanner(llm_client=self.llm)
        return await planner.plan_continue(
            existing_skeleton=skeleton,
            completed_units=completed_units,
            intent_continuation=intent_continuation,
        )
