"""Intent — 意图理解器 (Phase 1 4.1 + Phase 2 5.1)

在分解之前理解用户目标：识别目标、交付物、任务类型、约束、假设、需要澄清的问题。

只在「答案会改变计划结构、权限、成本或风险」时才输出 open_questions 触发澄清，
不为所有输入强制追问（执行方案 4.1）。

open_questions 驱动 Phase 2 的澄清交互（clarification_required SSE + clarify API）。
"""

from __future__ import annotations
import json
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from core.run.plan_spec import DeliverableSpec


TaskType = Literal["code", "research", "analysis", "creative", "long_content", "ops", "general"]


class ClarifyQuestion(BaseModel):
    """一个需要用户澄清的问题（2-4 个选项）"""
    id: str
    question: str
    options: List[str] = Field(default_factory=list)


class IntentResult(BaseModel):
    """意图理解结果"""
    goal: str = ""
    task_type: TaskType = "general"
    domain_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    hard_constraints: List[str] = Field(default_factory=list)
    soft_preferences: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    open_questions: List[ClarifyQuestion] = Field(default_factory=list)
    deliverables: List[DeliverableSpec] = Field(default_factory=list)
    confidence: float = 0.0

    @property
    def needs_clarification(self) -> bool:
        """是否需要澄清（有 open_questions）。"""
        return bool(self.open_questions)


def build_intent_prompt(task: str) -> str:
    """构建意图理解 prompt（纯函数，可单测）。"""
    return f"""分析这个用户任务，理解目标与不确定性。返回 JSON：
{{"goal": "结构化目标", "task_type": "code|research|analysis|creative|long_content|ops|general",
  "hard_constraints": ["硬约束"], "soft_preferences": ["软偏好"], "assumptions": ["假设"],
  "deliverables": [{{"id": "d1", "type": "report|code|content", "format": "markdown"}}],
  "open_questions": [{{"id": "q1", "question": "需要澄清的问题", "options": ["选项1", "选项2"]}}],
  "confidence": 0.0-1.0}}

规则：
1. open_questions 只在「答案会改变计划结构/权限/成本/风险」时才输出（如：输出格式、目标受众、
   是否需要审核、预算上限）；信息充分时返回空数组，不要为追问而追问。
2. 每个 open_question 给 2-4 个具体选项。
3. task_type 从给定的枚举里选，拿不准用 general。

任务：{task}

只返回 JSON。"""


def parse_intent_response(response: str) -> IntentResult:
    """解析意图理解输出（纯函数，可单测；解析失败返回空 IntentResult）。"""
    try:
        text = response.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        task_type = data.get("task_type", "general")
        if task_type not in ("code", "research", "analysis", "creative", "long_content", "ops", "general"):
            task_type = "general"
        return IntentResult(
            goal=data.get("goal", ""),
            task_type=task_type,
            hard_constraints=list(data.get("hard_constraints") or []),
            soft_preferences=list(data.get("soft_preferences") or []),
            assumptions=list(data.get("assumptions") or []),
            open_questions=[ClarifyQuestion(**q) for q in (data.get("open_questions") or [])],
            deliverables=[DeliverableSpec(**d) for d in (data.get("deliverables") or [])],
            confidence=min(1.0, max(0.0, float(data.get("confidence") or 0.0))),
        )
    except Exception:
        return IntentResult()


async def analyze_intent(task: str, llm_client) -> IntentResult:
    """调 LLM 分析意图（失败返回空 IntentResult，不阻断规划）。"""
    try:
        prompt = build_intent_prompt(task)
        response = await llm_client.chat(prompt)
        if isinstance(response, str) and response.startswith("[LLM"):
            return IntentResult()
        return parse_intent_response(response)
    except Exception:
        return IntentResult()
