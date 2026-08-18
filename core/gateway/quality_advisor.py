"""QualityAdvisor — LLM 驱动的质量违规修复建议生成器

为每条 WARNING/ERROR 级别 violation 生成 suggestion 和 auto_fix_available。
INFO 级别跳过以节省 LLM 调用。
LLM 调用失败时 graceful 降级（suggestion=None），不阻断流程。
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from core.models.quality import (
    QualityCheckReport,
    QualityCheckResult,
    Severity,
    Violation,
)

_logger = logging.getLogger(__name__)

SUGGESTION_PROMPT = """Given the following quality violation in a content generation pipeline:

Rule: {rule_name}
Severity: {severity}
Message: {message}
Content excerpt (around violation): {excerpt}

Generate a concise, actionable fix suggestion (1-2 sentences) that an AI agent can follow.
Also determine if this can be auto-fixed (true/false) — auto-fixable means the fix is mechanical
and does not require creative judgment.

Respond in JSON: {{"suggestion": "...", "auto_fix_available": true/false}}
"""


class QualityAdvisor:
    """LLM 驱动的质量建议生成器"""

    def __init__(self, llm_client: Any):
        self.llm = llm_client

    async def enhance_result(
        self,
        result: QualityCheckResult,
        content: str,
        node_id: str = "",
        node_name: str = "",
    ) -> QualityCheckReport:
        """为每条 violation 生成 suggestion，返回增强后的 QualityCheckReport。

        Args:
            result: RuleEngine 输出的 QualityCheckResult
            content: Agent 产出的原始文本
            node_id: 节点 ID（用于报告填充）
            node_name: 节点名称

        Returns:
            QualityCheckReport（含 LLM 生成的建议）
        """
        enhanced_violations: List[Violation] = []

        for violation in result.violations:
            if violation.severity == Severity.INFO:
                # INFO 级别不生成建议（节省 LLM 调用）
                enhanced_violations.append(violation)
                continue

            excerpt = self._extract_excerpt(content, violation.location)
            prompt = SUGGESTION_PROMPT.format(
                rule_name=violation.rule_name,
                severity=violation.severity.value,
                message=violation.message,
                excerpt=excerpt,
            )

            try:
                suggestion, auto_fix = await self._call_llm_for_suggestion(prompt)
                violation.suggestion = suggestion
                violation.auto_fix_available = auto_fix
            except Exception as e:
                _logger.warning(
                    "QualityAdvisor LLM 调用失败 (rule=%s): %s — 降级为无建议",
                    violation.rule_id, e,
                )
                violation.suggestion = None
                violation.auto_fix_available = False

            enhanced_violations.append(violation)

        result.violations = enhanced_violations

        return QualityCheckReport(
            node_id=node_id,
            node_name=node_name,
            result=result,
            suggestions_generated=True,
            overall_suggestion=self._summarize_suggestions(enhanced_violations),
        )

    async def _call_llm_for_suggestion(self, prompt: str) -> tuple:
        """调用 LLM 生成建议，返回 (suggestion, auto_fix_available)。

        兼容本项目 LLMClient.chat(prompt) -> str 接口。
        """
        # 优先使用 chat_json（如果可用）
        if hasattr(self.llm, 'chat_json'):
            parsed = await self.llm.chat_json(prompt)
            if parsed and isinstance(parsed, dict):
                return (
                    str(parsed.get("suggestion", "")),
                    bool(parsed.get("auto_fix_available", False)),
                )

        # 回退到 chat + 手动解析
        response = await self.llm.chat(prompt, temperature=0.2)
        parsed = self._parse_json_response(response)
        if parsed:
            return (
                str(parsed.get("suggestion", "")),
                bool(parsed.get("auto_fix_available", False)),
            )

        return (None, False)

    def _extract_excerpt(
        self,
        content: str,
        location: Optional[str] = None,
        window: int = 200,
    ) -> str:
        """从内容中提取违规位置附近的片段"""
        if not location:
            return content[:500]
        # 尝试根据 location 定位
        idx = content.find(location)
        if idx >= 0:
            start = max(0, idx - window // 2)
            end = min(len(content), idx + len(location) + window // 2)
            return content[start:end]
        return content[:500]

    def _parse_json_response(self, text: str) -> Optional[dict]:
        """从 LLM 响应中提取 JSON"""
        import re
        # ```json ... ``` 代码块
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        # 纯 JSON
        si = text.find("{")
        ei = text.rfind("}")
        if si >= 0 and ei > si:
            try:
                return json.loads(text[si:ei + 1])
            except json.JSONDecodeError:
                pass
        return None

    def _summarize_suggestions(self, violations: List[Violation]) -> str:
        """汇总建议为全局修复方向"""
        fixable = [v for v in violations if v.auto_fix_available]
        if not fixable:
            return "No auto-fixable violations."
        first = fixable[0].suggestion or "No specific suggestion."
        return f"{len(fixable)} violation(s) can be auto-fixed. Focus on: {first}"


def build_fix_prompt(
    violations: List[Violation],
    custom: Optional[str] = None,
) -> str:
    """构建修复指令 prompt（供 API /fix 使用）"""
    lines = ["Please fix the following quality violations in your output:"]
    for v in violations:
        lines.append(f"- [{v.severity.value}] {v.rule_name}: {v.message}")
        if v.suggestion:
            lines.append(f"  Suggested fix: {v.suggestion}")
        elif v.fix_hint:
            lines.append(f"  Hint: {v.fix_hint}")
    if custom:
        lines.append(f"\nAdditional instructions: {custom}")
    return "\n".join(lines)
