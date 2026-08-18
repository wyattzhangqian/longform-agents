"""MemoryCurator — 每个创作单元完成后的记忆维护"""
from __future__ import annotations
import logging
from typing import Any, Dict, Optional

from core.memory.layered_memory import LayeredMemory
from core.memory.continuity_tracker import ContinuityTracker

logger = logging.getLogger(__name__)


class MemoryCurator:
    """
    在每个创作单元完成后执行维护：
    1. 解析 Agent 产出的 metadata
    2. 更新连续性追踪
    3. 生成单元摘要 → 推入滑动窗口
    4. 检查逾期线索
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client

    async def process_unit_completion(
        self,
        run_context_dict: dict,
        unit_number: int,
        agent_output: Dict[str, Any],
    ) -> dict:
        """
        处理单元完成事件，更新 run_context_dict 中的记忆状态并返回。

        Parameters:
            run_context_dict: RunContext 的可变引用（将被就地修改）
            unit_number: 完成的单元编号
            agent_output: Agent 的产出结果 dict

        Returns:
            更新后的 run_context_dict
        """
        # 1. 解析 metadata
        metadata = self._extract_metadata(agent_output)

        # 2. 更新连续性追踪
        continuity_state = run_context_dict.get("continuity_state")
        if continuity_state is not None:
            tracker = ContinuityTracker.from_state(continuity_state)
            declaration = metadata.get("continuity_declaration", {})
            if declaration:
                tracker.update_from_declaration(unit_number, declaration)
            tracker.check_overdue(unit_number)
            run_context_dict["continuity_state"] = tracker.to_state()

        # 3. 生成摘要 → 推入滑动窗口
        layered_data = run_context_dict.get("layered_memory")
        if layered_data is not None:
            memory = LayeredMemory.from_run_context(layered_data)
            summary = await self._generate_summary(agent_output, unit_number)
            memory.push_to_L2(unit_number, summary)
            run_context_dict["layered_memory"] = memory.to_dict()

        # 4. 推进 current_unit
        run_context_dict["current_unit"] = unit_number + 1

        # --- Phase 3 新增：氛围更新 ---
        atmosphere_data = run_context_dict.get("atmosphere_state")
        if atmosphere_data is not None:
            from core.memory.atmosphere_tracker import AtmosphereTracker
            atm_tracker = AtmosphereTracker(state=atmosphere_data)
            atm_tracker.update_from_declaration(unit_number, metadata)
            run_context_dict["atmosphere_state"] = atm_tracker.to_state()

        # --- 修正10：角色 Profile examples 积累 ---
        profiles_data = run_context_dict.get("character_profiles")
        if profiles_data:
            try:
                from core.agent.character_profile import CharacterProfileStore
                store = CharacterProfileStore(profiles=profiles_data)
                for char_id, change in (metadata.get("atmosphere_changes") or {}).items():
                    trigger = change.get("trigger", "") if isinstance(change, dict) else ""
                    if trigger:
                        profile = store.get(char_id)
                        if profile and profile.behavioral_patterns:
                            store.append_example(char_id, profile.behavioral_patterns[0].situation, f"第{unit_number}单元：{trigger}")
                run_context_dict["character_profiles"] = store.to_state()
            except Exception as e:
                logger.warning("append_example failed: %s", e)

        # --- 修正4：骨架偏离检测 ---
        await self._check_skeleton_deviation(run_context_dict, unit_number, agent_output)

        return run_context_dict

    async def _generate_summary(self, agent_output: Dict[str, Any], unit_number: int) -> str:
        """为完成的单元生成精炼摘要"""
        content = self._extract_content(agent_output)
        if not content:
            return f"第 {unit_number} 单元已完成（无内容摘要）"

        if self.llm:
            try:
                prompt = (
                    f"请为以下创作单元生成精炼摘要（200字以内）。\n"
                    f"重点提取：关键事件、角色状态变化、氛围结束状态。\n\n"
                    f"内容（截取前2000字）：\n{content[:2000]}"
                )
                summary = await self.llm.chat(prompt, system_prompt="你是内容摘要专家。只输出摘要文本。")
                if summary and len(summary) > 10:
                    return summary.strip()
            except Exception as e:
                logger.warning(f"Summary generation failed for unit {unit_number}: {e}")

        # Fallback: 截取前 300 字
        return content[:300] + ("..." if len(content) > 300 else "")

    def _extract_metadata(self, agent_output: Dict[str, Any]) -> dict:
        """从 Agent 产出中提取 metadata 块"""
        # Agent 在正文末尾用 ```metadata 块输出元数据
        raw_text = agent_output.get("raw_text", "") or agent_output.get("content", "")
        result = agent_output.get("result", {})

        # 优先从 result 中取（如果 Agent 以结构化方式返回）
        if isinstance(result, dict) and "continuity_declaration" in result:
            return result

        # 否则尝试从 raw_text 中解析 ```metadata 块
        if "```metadata" in raw_text:
            try:
                import json
                start = raw_text.index("```metadata") + len("```metadata")
                end = raw_text.index("```", start)
                json_str = raw_text[start:end].strip()
                return json.loads(json_str)
            except (ValueError, json.JSONDecodeError):
                pass

        return {}

    def _extract_content(self, agent_output: Dict[str, Any]) -> str:
        """提取产出的正文内容（去除 metadata 块）"""
        raw_text = agent_output.get("raw_text", "") or ""
        result = agent_output.get("result", {})

        # 从 result 中取
        if isinstance(result, dict):
            content = result.get("content", "") or result.get("text", "")
            if content:
                return content

        # 从 raw_text 中取（去掉 metadata 块）
        if "```metadata" in raw_text:
            idx = raw_text.index("```metadata")
            return raw_text[:idx].strip()

        return raw_text

    async def _check_skeleton_deviation(self, run_context_dict: dict, unit_number: int, agent_output: Dict[str, Any]):
        """修正4：检测实际产出是否偏离骨架规划，连续3单元偏离触发调整"""
        skeleton_data = run_context_dict.get("skeleton")
        if not skeleton_data or not self.llm:
            return
        try:
            from core.skeleton.models import Skeleton
            skeleton = Skeleton.model_validate(skeleton_data)
            unit_spec = skeleton.get_unit(unit_number)
            if not unit_spec:
                return

            metadata = self._extract_metadata(agent_output)
            declaration = metadata.get("continuity_declaration", {})
            missed_resolves = [ct_id for ct_id in (unit_spec.continuity_resolve or []) if ct_id not in declaration.get("resolved", [])]

            deviation_key = "_consecutive_deviations"
            prev_count = run_context_dict.get(deviation_key, 0)
            new_count = prev_count + 1 if missed_resolves else 0
            run_context_dict[deviation_key] = new_count

            if new_count >= 3:
                from core.skeleton.planner import SkeletonPlanner
                planner = SkeletonPlanner(llm_client=self.llm)
                deviation_report = {
                    "missed_resolves": missed_resolves,
                    "consecutive_deviations": new_count,
                    "current_unit": unit_number,
                }
                adjusted = await planner.adjust(skeleton, deviation_report, unit_number)
                run_context_dict["skeleton"] = adjusted.model_dump()
                run_context_dict[deviation_key] = 0
                run_context_dict["_skeleton_just_adjusted"] = {
                    "reason": f"连续 {new_count} 个单元偏离规划（伏笔未按期回收）",
                    "affected_units": list(range(unit_number + 1, min(unit_number + 6, skeleton.total_units + 1))),
                    "changes_summary": f"已调整第 {unit_number+1}-{unit_number+5} 单元的 Brief",
                    "requires_confirmation": new_count >= 5,
                }
        except Exception as e:
            logger.warning(f"Skeleton deviation check failed: {e}")
