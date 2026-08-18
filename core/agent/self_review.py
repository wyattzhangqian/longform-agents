"""Agent 自评循环 — 产出完成后的质检"""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SELF_REVIEW_PROMPT = """请对以下创作产出进行自我审查。

任务要求：
{unit_brief}

你的产出摘要（前1500字）：
{output_preview}

审查维度：
1. 是否完成了目标中描述的关键事件？
2. 氛围是否匹配目标？
3. 连续性线索（引入/呼应/解决）是否处理了？
4. 整体质量如何？

输出严格 JSON 格式：
{{"pass": true/false, "score": 0.0-1.0, "issues": ["问题1", "问题2"]}}
如果质量达标且无明显问题，pass=true。
"""


async def self_review(
    llm_client,
    output_text: str,
    unit_brief: str = "",
    threshold: float = 0.7,
) -> Tuple[bool, float, List[str]]:
    """
    Agent 自评。返回 (passed, score, issues)。
    LLM 不可用时默认 pass。
    """
    if not llm_client or not unit_brief:
        return True, 1.0, []

    try:
        prompt = SELF_REVIEW_PROMPT.format(
            unit_brief=unit_brief[:1000],
            output_preview=output_text[:1500],
        )
        raw = await llm_client.chat(prompt, system_prompt="你是质量审查者。只输出JSON。")

        # 解析
        raw = raw.strip()
        data = {}
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip().lstrip("json").strip()
                try:
                    data = json.loads(part)
                    break
                except (json.JSONDecodeError, ValueError):
                    continue
        else:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                data = {}

        passed = data.get("pass", True)
        score = float(data.get("score", 0.8))
        issues = data.get("issues", [])

        # 用阈值兜底
        if score < threshold:
            passed = False

        return passed, score, issues

    except Exception as e:
        logger.warning(f"Self-review failed: {e}")
        return True, 0.8, []  # 失败时默认 pass
