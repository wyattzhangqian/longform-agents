"""已有内容分析 — 从正文提取设定/角色/线索/状态"""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """分析以下已有创作内容，提取关键信息。

内容类型：{content_type}
单元数量：{unit_count}

内容摘要（每个单元的前500字）：
{content_preview}

请提取：
1. global_settings: 世界观/背景设定的核心描述（200字内）
2. characters: 出场角色列表，每个角色含 id/name/core_identity/speech_style
3. continuity_threads: 已存在的线索/伏笔/悬念（id/content/introduce_at/status）
4. current_atmosphere: 最后一个单元结束时的角色情绪和场景氛围
5. summary: 整体故事进展的一句话总结

输出 JSON 格式。
"""


class ContentAnalyzer:
    """分析用户提供的已有内容"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    async def analyze(
        self, existing_units: list, content_type: str = "novel"
    ) -> Dict[str, Any]:
        """
        分析已有内容，返回结构化的提取结果。
        LLM 不可用时返回最小化结果（只有 unit_summaries）。
        """
        # 准备内容预览
        previews = []
        for unit in existing_units:
            content = unit.content if hasattr(unit, 'content') else unit.get("content", "")
            number = unit.unit_number if hasattr(unit, 'unit_number') else unit.get("unit_number", 0)
            previews.append(f"--- 第 {number} 单元 ---\n{content[:500]}")

        # 基本的 unit_summaries（不需要 LLM）
        unit_summaries = []
        for unit in existing_units:
            content = unit.content if hasattr(unit, 'content') else unit.get("content", "")
            number = unit.unit_number if hasattr(unit, 'unit_number') else unit.get("unit_number", 0)
            unit_summaries.append({
                "unit_number": number,
                "summary": content[:300] + ("..." if len(content) > 300 else ""),
            })

        result = {
            "unit_summaries": unit_summaries,
            "global_settings": "",
            "characters": [],
            "continuity_threads": [],
            "character_profiles": [],
            "current_atmosphere": None,
            "summary": "",
        }

        if not self.llm:
            return result

        # LLM 分析（可能失败，有 fallback）
        try:
            prompt = ANALYSIS_PROMPT.format(
                content_type=content_type,
                unit_count=len(existing_units),
                content_preview="\n\n".join(previews[:10]),  # 最多 10 个单元的预览
            )
            raw = await self.llm.chat(prompt, system_prompt="你是内容分析专家。只输出JSON。")
            data = self._parse_json(raw)
            if data:
                result["global_settings"] = data.get("global_settings", "")
                result["summary"] = data.get("summary", "")
                result["current_atmosphere"] = data.get("current_atmosphere")

                # 角色 → CharacterProfile 格式
                for char in data.get("characters", []):
                    result["character_profiles"].append({
                        "character_id": char.get("id", char.get("name", "")),
                        "core_identity": char.get("core_identity", ""),
                        "behavioral_patterns": [],
                        "speech_style": {"characteristics": [], "avoids": [], "emotion_expression": char.get("speech_style", "")},
                        "relationships": [],
                        "growth_stage": {"current": "", "next_milestone": "", "growth_trigger": ""},
                    })

                # 连续性线索
                for ct in data.get("continuity_threads", []):
                    result["continuity_threads"].append({
                        "id": ct.get("id", f"ct_{len(result['continuity_threads'])+1:03d}"),
                        "type": "plot_foreshadow",
                        "content": ct.get("content", ""),
                        "significance": ct.get("significance", ""),
                        "introduce_at": ct.get("introduce_at", 1),
                        "reference_at": ct.get("reference_at", []),
                        "resolve_at": ct.get("resolve_at", 999),
                        "scope": "global",
                        "status": ct.get("status", "introduced"),
                    })
        except Exception as e:
            logger.warning(f"Content analysis LLM failed: {e}")

        return result

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip().lstrip("json").strip()
                try:
                    return json.loads(part)
                except (json.JSONDecodeError, ValueError):
                    continue
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
