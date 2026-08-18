"""参考作品风格提取 — 从参考作品中提取风格/结构规则"""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

EXTRACT_STYLE_PROMPT = """分析以下参考作品，提取可复用的风格规则。

作品标题：{title}
参考方面：{aspects}

作品内容（前3000字）：
{content_preview}

请提取 3-5 条风格规则，每条含：
- category: style | structure | pacing | character
- title: 规则名称
- content: 规则描述（如何在新作品中应用，100字内）

输出 JSON 数组格式。
"""


class ReferenceExtractor:
    """从参考作品中提取风格/结构规则"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    async def extract(self, work: Any) -> List[Dict[str, str]]:
        """提取参考作品的风格规则"""
        title = work.title if hasattr(work, 'title') else work.get("title", "")
        content = work.content if hasattr(work, 'content') else work.get("content", "")
        aspects = work.aspects if hasattr(work, 'aspects') else work.get("aspects", ["style"])

        if not self.llm:
            return self._fallback_rules(title, aspects)

        try:
            prompt = EXTRACT_STYLE_PROMPT.format(
                title=title,
                aspects="、".join(aspects),
                content_preview=content[:3000],
            )
            raw = await self.llm.chat(prompt, system_prompt="你是文学分析专家。只输出JSON数组。")
            rules = self._parse_json_array(raw)
            return rules if rules else self._fallback_rules(title, aspects)
        except Exception as e:
            logger.warning(f"Reference extraction failed: {e}")
            return self._fallback_rules(title, aspects)

    def _fallback_rules(self, title: str, aspects: List[str]) -> List[Dict[str, str]]:
        """LLM 不可用时的降级规则"""
        rules = []
        if "style" in aspects:
            rules.append({
                "category": "style",
                "title": f"参考《{title}》风格",
                "content": f"写作风格参考《{title}》，注意模仿其叙事语言和表达特色。",
            })
        if "structure" in aspects:
            rules.append({
                "category": "structure",
                "title": f"参考《{title}》结构",
                "content": f"章节结构参考《{title}》的起承转合模式。",
            })
        return rules

    @staticmethod
    def _parse_json_array(text: str) -> List[Dict]:
        text = text.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip().lstrip("json").strip()
                try:
                    result = json.loads(part)
                    return result if isinstance(result, list) else []
                except (json.JSONDecodeError, ValueError):
                    continue
        try:
            result = json.loads(text)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
