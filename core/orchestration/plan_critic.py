"""PlanCritic — 计划语义审查 (Phase 1)

与 `plan_validator`（确定性结构校验）分离：critic 调 LLM 检查**语义完整性与风险**，
输出结构化问题列表，**不直接修改计划**（修改由 plan_refiner 完成）。

检查项（执行方案 4.4）：
- 步骤遗漏（缺交付物必要步骤）；
- 粒度失衡（阶段过粗/过细）；
- 不必要串行（可并行被串行化）；
- 阶段目标不清；
- 验收标准不可执行；
- 风险未覆盖。

Critic 失败（LLM 异常）返回空 critique，不阻断规划（P0 非致命降级记录在案）。
"""

from __future__ import annotations
import json
from typing import Any, List, Optional
from pydantic import BaseModel, Field

from core.run.plan_spec import PlanSpec


_ISSUE_KINDS = (
    "missing_step",
    "granularity",
    "unnecessary_serial",
    "unclear_objective",
    "unexecutable_criteria",
    "uncovered_risk",
)


class CritiqueIssue(BaseModel):
    """一条语义审查问题"""
    phase_id: str = ""                 # 关联的阶段（空=计划级问题）
    kind: str = ""                     # missing_step/granularity/...（见 _ISSUE_KINDS）
    message: str = ""                  # 问题描述（可执行的修改建议）


class PlanCritique(BaseModel):
    """结构化审查结果（不直接修改计划）"""
    issues: List[CritiqueIssue] = Field(default_factory=list)
    overall: str = ""                  # 一句话总评


def build_critique_prompt(spec: PlanSpec) -> str:
    """构建语义审查 prompt（纯函数，可单测）。"""
    lines = [f"任务：{spec.task}"]
    if spec.deliverables:
        lines.append("交付物：" + "；".join(d.type for d in spec.deliverables))
    lines.append("阶段清单：")
    for i, p in enumerate(spec.phases, 1):
        deps = f"（依赖 {', '.join(p.dependencies)}）" if p.dependencies else ""
        criteria = f"验收：{'; '.join(p.acceptance_criteria)}" if p.acceptance_criteria else "验收：无"
        lines.append(f"{i}. {p.phase_id}「{p.label}」→ {p.agent_id}{deps}；{criteria}")

    lines.append("""
请审查这个计划的语义完整性与风险，返回 JSON：
{"issues": [{"phase_id": "阶段ID或空", "kind": "问题类型", "message": "可执行的修改建议"}], "overall": "一句话总评"}

问题类型（kind）只能是：missing_step（缺必要步骤）/ granularity（粒度失衡）/
unnecessary_serial（可并行却串行）/ unclear_objective（目标不清）/
unexecutable_criteria（验收不可执行）/ uncovered_risk（风险未覆盖）。

没有问题时 issues 返回空数组。只返回 JSON。""")
    return "\n".join(lines)


def parse_critique_response(response: str) -> PlanCritique:
    """解析 LLM 审查输出（纯函数，可单测；解析失败返回空 critique）。"""
    try:
        text = response.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        issues = []
        for it in data.get("issues") or []:
            kind = it.get("kind", "")
            if kind not in _ISSUE_KINDS:
                kind = "uncovered_risk"  # 未知类型归入风险类，不静默丢弃
            issues.append(CritiqueIssue(
                phase_id=it.get("phase_id", ""),
                kind=kind,
                message=it.get("message", ""),
            ))
        return PlanCritique(issues=issues, overall=data.get("overall", ""))
    except Exception:
        return PlanCritique()


async def critique_plan(spec: PlanSpec, llm_client) -> PlanCritique:
    """调 LLM 审查计划（失败返回空 critique，不阻断规划）。"""
    try:
        prompt = build_critique_prompt(spec)
        response = await llm_client.chat(prompt)
        if isinstance(response, str) and response.startswith("[LLM"):
            return PlanCritique()
        return parse_critique_response(response)
    except Exception:
        return PlanCritique()
