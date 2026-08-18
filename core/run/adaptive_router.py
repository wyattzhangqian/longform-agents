"""AdaptiveRouter — 自适应路由决策器

在 adaptive 模式下，每个 Agent 节点执行后由 AdaptiveRouter 评估产出质量，
决定 CONTINUE / RETRY / ASK_HUMAN / SKIP，支持重试上限和总迭代上限。

设计约束:
- Router 不作为显式图节点存在，仅在 NodeExecutor 层隐式调用
- 重试必须带 feedback 注入，不能裸重试
- max_retries_per_node / max_total_iterations 是硬上限，LLM 不能覆盖
- 每次路由决策发出 router_decision SSE 事件
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel

from core.graph.types import GraphRunConfig

_logger = logging.getLogger(__name__)


class RouterDecision(str, Enum):
    """路由决策枚举"""
    CONTINUE = "continue"       # 进入下一个节点
    RETRY = "retry"             # 重试当前节点（使用反馈）
    ASK_HUMAN = "ask_human"     # 暂停，等待人工输入
    SKIP = "skip"               # 跳过当前节点


class RouterResult(BaseModel):
    """路由评估结果"""
    decision: RouterDecision
    reasoning: str = ""
    feedback: Optional[str] = None   # retry 时传给下一次执行的反馈
    retry_count: int = 0


class QualityCheckResult(BaseModel):
    """质量检查结果（轻量适配，兼容 gateway 输出）"""
    score: float = 0.0
    violations: list[dict[str, Any]] = []
    passed: bool = True

    @property
    def violations_summary(self) -> str:
        if not self.violations:
            return "None"
        lines = []
        for v in self.violations[:5]:
            sev = v.get("severity", "info")
            msg = v.get("message", str(v))
            lines.append(f"[{sev}] {msg}")
        return "\n".join(lines)


ADAPTIVE_ROUTER_PROMPT = """You are a routing decision agent in a multi-phase pipeline.

## Context
- Current node: {node_name} (phase {phase_index}/{total_phases})
- Node output quality score: {quality_score}
- Accumulated retries for this node: {retry_count}/{max_retries}
- Total pipeline iterations so far: {total_iterations}/{max_iterations}
- Quality violations (if any): {violations}

## Node Output (last 2000 chars):
{node_output_tail}

## Decision Rules
1. CONTINUE — output meets quality bar, proceed to next node
2. RETRY — output has fixable issues; provide specific feedback (max {max_retries} retries)
3. ASK_HUMAN — ambiguous situation or critical decision needed; formulate a clear question
4. SKIP — node is optional and output is unrecoverable within retry budget

Respond in JSON: {{"decision": "...", "reasoning": "...", "feedback": "..."}}
"""


class AdaptiveRouter:
    """自适应路由器

    在 adaptive 模式下由 NodeExecutor 调用，评估每个 Agent 节点的产出质量，
    返回路由决策。硬约束（重试上限/迭代上限）优先于 LLM 决策。

    Usage:
        router = AdaptiveRouter(llm_client, config)
        result = await router.evaluate(
            node_id="phase_1",
            node_name="Writer Agent",
            node_output="...",
            phase_index=0,
            total_phases=3,
        )
    """

    def __init__(
        self,
        llm_client: Any,
        config: GraphRunConfig,
    ):
        self.llm = llm_client
        self.max_retries = config.max_retries_per_node
        self.max_iterations = config.max_total_iterations
        self._retry_counts: dict[str, int] = {}
        self._total_iterations: int = 0

    @property
    def total_iterations(self) -> int:
        return self._total_iterations

    async def evaluate(
        self,
        node_id: str,
        node_name: str,
        node_output: str,
        phase_index: int,
        total_phases: int,
        quality_result: Optional[QualityCheckResult] = None,
    ) -> RouterResult:
        """评估节点产出，返回路由决策。

        硬约束优先：
        1. 超过 max_retries → 强制 CONTINUE
        2. 超过 max_total_iterations → 强制 CONTINUE

        Returns:
            RouterResult
        """
        self._total_iterations += 1
        retry_count = self._retry_counts.get(node_id, 0)

        # 硬约束：超过重试上限强制 continue
        if retry_count >= self.max_retries:
            return RouterResult(
                decision=RouterDecision.CONTINUE,
                reasoning=f"Max retries ({self.max_retries}) reached for node '{node_name}'",
                retry_count=retry_count,
            )

        # 硬约束：超过总迭代上限强制 continue
        if self._total_iterations >= self.max_iterations:
            return RouterResult(
                decision=RouterDecision.CONTINUE,
                reasoning=f"Max total iterations ({self.max_iterations}) reached",
                retry_count=retry_count,
            )

        # 构建 prompt 并调用 LLM
        prompt = ADAPTIVE_ROUTER_PROMPT.format(
            node_name=node_name,
            phase_index=phase_index,
            total_phases=total_phases,
            quality_score=quality_result.score if quality_result else "N/A",
            retry_count=retry_count,
            max_retries=self.max_retries,
            total_iterations=self._total_iterations,
            max_iterations=self.max_iterations,
            violations=quality_result.violations_summary if quality_result else "None",
            node_output_tail=node_output[-2000:],
        )

        try:
            response_text = await self._call_llm(prompt)
            result = self._parse_response(response_text)
        except Exception as e:
            _logger.warning(
                "AdaptiveRouter LLM 调用/解析失败 (node=%s): %s — 降级为 CONTINUE",
                node_id, e,
            )
            result = RouterResult(
                decision=RouterDecision.CONTINUE,
                reasoning=f"LLM evaluation failed: {e}",
                retry_count=retry_count,
            )

        # 记录重试计数
        if result.decision == RouterDecision.RETRY:
            self._retry_counts[node_id] = retry_count + 1

        result.retry_count = self._retry_counts.get(node_id, 0)
        return result

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 客户端，返回文本响应。

        兼容两种接口：
        - LLMClient.chat(prompt: str) -> str  （本项目 tools/llm_client.py）
        - 通用 chat(messages=[...]) -> { content: str }
        """
        if hasattr(self.llm, "chat") and not hasattr(self.llm, "chat_messages"):
            # 本项目 LLMClient.chat(prompt) -> str
            return await self.llm.chat(prompt)

        # 通用接口: chat(messages=[{role, content}]) -> { content: str }
        response = await self.llm.chat(messages=[{"role": "user", "content": prompt}])
        if isinstance(response, dict):
            return str(response.get("content", ""))
        if isinstance(response, str):
            return response
        return str(response)

    def _parse_response(self, response_text: str) -> RouterResult:
        """解析 LLM JSON 响应为 RouterResult。

        支持纯 JSON、```json 代码块、以及带前后文字的 JSON。
        """
        json_str = self._extract_json(response_text)
        if json_str:
            try:
                data = json.loads(json_str)
                decision_str = str(data.get("decision", "continue")).lower().strip()
                try:
                    decision = RouterDecision(decision_str)
                except ValueError:
                    _logger.warning("AdaptiveRouter 收到未知 decision: %s — 降级为 CONTINUE", decision_str)
                    decision = RouterDecision.CONTINUE

                return RouterResult(
                    decision=decision,
                    reasoning=str(data.get("reasoning", "")),
                    feedback=str(data.get("feedback")) if data.get("feedback") else None,
                )
            except json.JSONDecodeError as e:
                _logger.warning("AdaptiveRouter JSON 解析失败: %s", e)

        # 降级
        return RouterResult(
            decision=RouterDecision.CONTINUE,
            reasoning="Failed to parse LLM response",
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """从文本中提取 JSON 块"""
        import re
        # ```json ... ``` 代码块
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 纯 JSON（花括号包裹）
        si = text.find("{")
        ei = text.rfind("}")
        if si >= 0 and ei > si:
            return text[si:ei + 1]

        return None

    def reset(self) -> None:
        """重置路由器状态（新 run 时调用）"""
        self._retry_counts.clear()
        self._total_iterations = 0
