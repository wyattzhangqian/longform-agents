"""多遍骨架规划器 — 为长内容项目产出全局结构蓝图"""
from __future__ import annotations
import json
import logging
from typing import Optional

from core.skeleton.models import (
    Skeleton, Arc, UnitSpec, UnitDefinition,
    ContinuityThread, RhythmPoint,
)

logger = logging.getLogger(__name__)

# --- Prompt 模板 ---

PASS1_PROMPT = """你是一个内容结构规划专家。请为以下创作意图生成全局骨架。

创作意图：{intent}
内容类型：{content_type}
预计总单元数：{total_units}

请输出 JSON（严格遵循格式）：
{{
  "premise": "一句话前提",
  "theme": "核心主题",
  "arcs": [
    {{
      "id": "arc_1",
      "title": "第一卷标题",
      "unit_range": [1, 25],
      "core_conflict": "本卷核心冲突",
      "resolution": "本卷如何收尾",
      "character_arcs": [{{"character": "角色名", "from": "起始状态", "to": "结束状态"}}]
    }}
  ],
  "main_continuity_threads": [
    {{
      "id": "ct_001",
      "type": "plot_foreshadow",
      "content": "描述",
      "significance": "为什么重要",
      "introduce_at": 3,
      "reference_at": [8, 12],
      "resolve_at": 20,
      "scope": "global"
    }}
  ],
  "rhythm_curve_key_points": [
    {{"unit_number": 1, "pacing": "exposition", "tension": 0.2}},
    {{"unit_number": 15, "pacing": "climax", "tension": 0.9}}
  ]
}}
"""

PASS2_PROMPT = """基于以下全局骨架，细化第 {arc_id}（单元 {start}-{end}）。

全局骨架：
{skeleton_summary}

本 Arc 核心冲突：{core_conflict}
本 Arc 收尾：{resolution}

请为本 Arc 的每个单元输出一句话摘要和节奏标注。同时列出本 Arc 内部的局部连续性线索。

输出 JSON：
{{
  "units": [
    {{
      "unit_number": {start},
      "goal": "一句话目标",
      "pacing": "exposition",
      "critical": false
    }}
  ],
  "local_continuity_threads": [
    {{
      "id": "ct_local_001",
      "type": "plot_foreshadow",
      "content": "...",
      "significance": "...",
      "introduce_at": N,
      "reference_at": [M],
      "resolve_at": K,
      "scope": "arc"
    }}
  ]
}}
"""

PASS3_PROMPT = """基于以下信息，为单元 {start}-{end} 生成详细 Brief。

全局设定：{premise}
本 Arc：{arc_title}（{arc_conflict}）
前后单元摘要：
{surrounding_context}

连续性线索（本批次需处理的）：
{continuity_obligations}

请输出 JSON：
{{
  "units": [
    {{
      "unit_number": N,
      "goal": "详细目标（2-3句）",
      "pacing": "rising",
      "critical": false,
      "continuity_introduce": ["ct_id"],
      "continuity_reference": ["ct_id"],
      "continuity_resolve": ["ct_id"],
      "atmosphere_target": "紧张→爆发",
      "type_specific": {{}}
    }}
  ]
}}
"""


class SkeletonPlanner:
    """多遍骨架规划器"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    @staticmethod
    def _clamp_units_to_total(skeleton: "Skeleton", total_units: int) -> None:
        """强制单元数与请求一致（2026-08-09 P0）。

        LLM Pass2 可能超发 unit spec：3 章请求实测返回 25 个 unit，不 clamp 会让
        骨架扩展成 25 章，用户请求的 3 章骨架共享 run 直接失效（25 章跑不完）。
        顺带把 arc.unit_range 收敛到 [1, total_units]。
        """
        skeleton.units = sorted(
            (u for u in skeleton.units if 1 <= u.unit_number <= total_units),
            key=lambda u: u.unit_number,
        )
        skeleton.total_units = total_units
        for arc in skeleton.arcs:
            low, high = arc.unit_range
            arc.unit_range = [
                max(1, min(total_units, low)),
                max(1, min(total_units, high)),
            ]

    async def plan(
        self,
        intent: str,
        content_type: str = "novel",
        total_units: int = 100,
        unit_definition: Optional[UnitDefinition] = None,
    ) -> Skeleton:
        """
        完整多遍规划：Pass1 全局 → Pass2 分Arc → Pass3 每单元Brief

        如果 LLM 不可用，返回简化骨架（单 Arc、均匀节奏）。
        """
        if not self.llm:
            return self._fallback_skeleton(intent, content_type, total_units)

        # --- Pass 1: 全局骨架 ---
        skeleton = await self._pass1_global(intent, content_type, total_units)
        if not skeleton:
            return self._fallback_skeleton(intent, content_type, total_units)

        if unit_definition:
            skeleton.unit_definition = unit_definition

        # --- Pass 2: 分 Arc 细化 ---
        for arc in skeleton.arcs:
            await self._pass2_arc_detail(skeleton, arc)

        self._clamp_units_to_total(skeleton, total_units)

        # --- Pass 3: 每单元 Brief ---
        batch_size = 5
        for i in range(0, skeleton.total_units, batch_size):
            start = i + 1
            end = min(i + batch_size, skeleton.total_units)
            await self._pass3_unit_briefs(skeleton, start, end)

        return skeleton

    async def plan_outline(
        self,
        intent: str,
        content_type: str = "novel",
        total_units: int = 100,
    ) -> Skeleton:
        """预规划（规划阶段预览用）— 只做 Pass1 全局 + Pass2 Arc 细化，跳过 Pass3 单章详细 Brief。

        目的：规划界面只需展示"骨架 Arc 结构 + 每章一句话 goal"，不需要 Pass3 的 2-3 句详细 Brief。
        Pass3 100 章要 20 次 LLM 调用（~5 min），规划阶段用户等不起；执行阶段才做完整 Pass1+2+3。
        """
        if not self.llm:
            return self._fallback_skeleton(intent, content_type, total_units)
        skeleton = await self._pass1_global(intent, content_type, total_units)
        if not skeleton:
            return self._fallback_skeleton(intent, content_type, total_units)
        for arc in skeleton.arcs:
            await self._pass2_arc_detail(skeleton, arc)
        self._clamp_units_to_total(skeleton, total_units)
        return skeleton

    async def plan_continue(
        self,
        existing_skeleton: Skeleton,
        completed_units: int,
        intent_continuation: str = "",
    ) -> Skeleton:
        """
        续写模式：基于已有骨架，从 completed_units+1 开始规划后续。
        保留已有 Arc 和 Unit 不动，只规划新的部分。
        """
        # 保留已完成的 units
        skeleton = existing_skeleton.model_copy(deep=True)

        # 对未完成的 arcs 重新 Pass 2
        for arc in skeleton.arcs:
            if arc.unit_range[1] > completed_units:
                await self._pass2_arc_detail(skeleton, arc, start_from=completed_units + 1)

        # 对未完成的 units 重新 Pass 3
        batch_size = 5
        start = completed_units + 1
        for i in range(start, skeleton.total_units + 1, batch_size):
            end = min(i + batch_size - 1, skeleton.total_units)
            await self._pass3_unit_briefs(skeleton, i, end)

        skeleton.version += 1
        return skeleton

    async def adjust(
        self,
        skeleton: Skeleton,
        deviation_report: dict,
        current_unit: int,
    ) -> Skeleton:
        """
        运行时微调：基于偏离报告调整后续单元的 Brief。
        只调整 current_unit+1 之后的内容，已完成的不动。
        """
        # 简化处理：重新跑 Pass 3 对受影响的 5 个后续单元
        start = current_unit + 1
        end = min(start + 4, skeleton.total_units)
        await self._pass3_unit_briefs(skeleton, start, end)
        skeleton.version += 1
        return skeleton

    # === 内部实现 ===

    async def _pass1_global(self, intent: str, content_type: str, total_units: int) -> Optional[Skeleton]:
        prompt = PASS1_PROMPT.format(
            intent=intent, content_type=content_type, total_units=total_units
        )
        try:
            raw = await self.llm.chat(prompt, system_prompt="你是内容结构规划专家。只输出JSON。")
            data = self._parse_json(raw)
            if not data:
                return None

            skeleton = Skeleton(
                content_type=content_type,
                total_units=total_units,
                premise=data.get("premise", intent),
                theme=data.get("theme", ""),
            )

            for a in data.get("arcs", []):
                skeleton.arcs.append(Arc(
                    id=a["id"],
                    title=a.get("title", ""),
                    unit_range=a.get("unit_range", [1, total_units]),
                    core_conflict=a.get("core_conflict", ""),
                    resolution=a.get("resolution", ""),
                    character_arcs=a.get("character_arcs", []),
                ))

            for ct in data.get("main_continuity_threads", []):
                skeleton.continuity_threads.append(ContinuityThread(
                    id=ct["id"],
                    type=ct.get("type", "plot_foreshadow"),
                    content=ct.get("content", ""),
                    significance=ct.get("significance", ""),
                    introduce_at=ct.get("introduce_at") or 1,
                    reference_at=ct.get("reference_at") or [],
                    resolve_at=ct.get("resolve_at") or total_units,
                    scope=ct.get("scope", "global"),
                ))

            for rp in data.get("rhythm_curve_key_points", []):
                skeleton.rhythm_curve.append(RhythmPoint(
                    unit_number=rp["unit_number"],
                    pacing=rp.get("pacing", "exposition"),
                    tension=rp.get("tension", 0.5),
                ))

            return skeleton
        except Exception as e:
            logger.warning(f"Pass1 failed: {e}")
            return None

    async def _pass2_arc_detail(self, skeleton: Skeleton, arc: Arc, start_from: int = 0):
        """Pass 2: 细化单个 Arc"""
        start, end = arc.unit_range
        if start_from > start:
            start = start_from

        prompt = PASS2_PROMPT.format(
            arc_id=arc.id,
            start=start,
            end=end,
            skeleton_summary=f"前提: {skeleton.premise}\n主题: {skeleton.theme}",
            core_conflict=arc.core_conflict,
            resolution=arc.resolution,
        )

        try:
            raw = await self.llm.chat(prompt, system_prompt="你是内容结构规划专家。只输出JSON。")
            data = self._parse_json(raw)
            if not data:
                return

            for u in data.get("units", []):
                unit_number = u.get("unit_number", start)
                # 更新或新增
                existing = skeleton.get_unit(unit_number)
                if existing:
                    existing.goal = u.get("goal", existing.goal)
                    existing.pacing = u.get("pacing", existing.pacing)
                    existing.critical = u.get("critical", existing.critical)
                else:
                    skeleton.units.append(UnitSpec(
                        unit_number=unit_number,
                        goal=u.get("goal", ""),
                        pacing=u.get("pacing", "exposition"),
                        critical=u.get("critical", False),
                    ))

            for ct in data.get("local_continuity_threads", []):
                skeleton.continuity_threads.append(ContinuityThread(
                    id=ct["id"],
                    type=ct.get("type", "plot_foreshadow"),
                    content=ct.get("content", ""),
                    significance=ct.get("significance", ""),
                    introduce_at=ct.get("introduce_at") or start,
                    reference_at=ct.get("reference_at") or [],
                    resolve_at=ct.get("resolve_at") or end,
                    scope="arc",
                ))
        except Exception as e:
            logger.warning(f"Pass2 for {arc.id} failed: {e}")

    async def _pass3_unit_briefs(self, skeleton: Skeleton, start: int, end: int):
        """Pass 3: 为一批单元生成详细 Brief"""
        arc = skeleton.get_arc_for_unit(start)

        # 构建前后文
        surrounding = []
        for u in skeleton.units:
            if start - 3 <= u.unit_number <= end + 3:
                surrounding.append(f"  单元{u.unit_number}: {u.goal}")
        surrounding_text = "\n".join(surrounding) if surrounding else "（暂无）"

        # 构建连续性义务
        obligations = []
        for t in skeleton.continuity_threads:
            if start <= t.introduce_at <= end:
                obligations.append(f"  引入: [{t.id}] {t.content}")
            if start <= t.resolve_at <= end:
                obligations.append(f"  解决: [{t.id}] {t.content}")
            for ref_at in t.reference_at:
                if start <= ref_at <= end:
                    obligations.append(f"  呼应: [{t.id}] {t.content} (单元{ref_at})")
        obligations_text = "\n".join(obligations) if obligations else "（无）"

        prompt = PASS3_PROMPT.format(
            start=start,
            end=end,
            premise=skeleton.premise,
            arc_title=arc.title if arc else "",
            arc_conflict=arc.core_conflict if arc else "",
            surrounding_context=surrounding_text,
            continuity_obligations=obligations_text,
        )

        try:
            raw = await self.llm.chat(prompt, system_prompt="你是内容结构规划专家。只输出JSON。")
            data = self._parse_json(raw)
            if not data:
                return

            for u in data.get("units", []):
                unit_number = u.get("unit_number", start)
                existing = skeleton.get_unit(unit_number)
                if existing:
                    existing.goal = u.get("goal", existing.goal)
                    existing.pacing = u.get("pacing", existing.pacing)
                    existing.critical = u.get("critical", existing.critical)
                    existing.continuity_introduce = u.get("continuity_introduce", [])
                    existing.continuity_reference = u.get("continuity_reference", [])
                    existing.continuity_resolve = u.get("continuity_resolve", [])
                    existing.atmosphere_target = u.get("atmosphere_target", "")
                    existing.type_specific = u.get("type_specific", {})
                else:
                    # 只取 UnitSpec 已知字段，防止 LLM 返回多余 key 导致 ValidationError
                    known_fields = UnitSpec.model_fields.keys()
                    filtered = {k: v for k, v in u.items() if k in known_fields}
                    if "unit_number" in filtered:
                        skeleton.units.append(UnitSpec(**filtered))
        except Exception as e:
            logger.warning(f"Pass3 for units {start}-{end} failed: {e}")

    def _fallback_skeleton(self, intent: str, content_type: str, total_units: int) -> Skeleton:
        """LLM 不可用时的降级骨架"""
        skeleton = Skeleton(
            content_type=content_type,
            total_units=total_units,
            premise=intent,
        )
        skeleton.arcs.append(Arc(
            id="arc_1",
            title="全篇",
            unit_range=[1, total_units],
            core_conflict=intent,
        ))
        for i in range(1, total_units + 1):
            skeleton.units.append(UnitSpec(
                unit_number=i,
                goal=f"第{i}个创作单元",
                pacing="exposition",
            ))
        return skeleton

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        """从 LLM 输出中提取 JSON"""
        text = text.strip()
        # 处理 markdown code block
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    return json.loads(part)
                except (json.JSONDecodeError, ValueError):
                    continue
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
