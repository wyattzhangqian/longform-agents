"""PlanRevision — 用户修改意图驱动的计划修订 (Phase 2)

与 `plan_refiner`（基于 critic 结构化问题修订）的区别：这里基于用户**自由文本指令**
（"把研究和竞品分析并行"、"换成人工审核"、"输出改 PPT"）。

流程（执行方案 5.2）：
    读 base revision → LLM 解析修改意图 → 生成 candidate revision → 确定性校验 → 返回。

revision 是独立生命周期（与 Run checkpoint 分开），每次修订 revision+1，
不覆盖历史版本；`base_revision` 不匹配时拒绝（避免并发/过期修订）。
"""

from __future__ import annotations
from typing import List, Optional

from pydantic import BaseModel, Field

from core.run.plan_spec import PlanSpec, PhasePlan
from core.orchestration.plan_refiner import parse_refine_response


class PlanRevisionDiff(BaseModel):
    """局部重规划的 revision diff（执行方案 6.2）—— 不静默替换整张图。"""
    base_revision: int
    new_revision: int
    added_phases: List[PhasePlan] = Field(default_factory=list)
    removed_phase_ids: List[str] = Field(default_factory=list)
    updated_phases: List[PhasePlan] = Field(default_factory=list)
    unchanged_phase_ids: List[str] = Field(default_factory=list)
    reason: str = ""
    requires_user_confirmation: bool = True


def diff_plan_revisions(
    base: PlanSpec,
    new: PlanSpec,
    reason: str = "",
) -> PlanRevisionDiff:
    """对比两个 revision，输出结构化 diff（纯函数，可单测）。

    高风险变更（删除阶段 / 新增或更新 high-risk 阶段）→ requires_user_confirmation=True
    （P5：不可逆/高风险操作必须暂停等待用户确认）。
    """
    base_map = {p.phase_id: p for p in base.phases}
    new_map = {p.phase_id: p for p in new.phases}
    base_ids = set(base_map)
    new_ids = set(new_map)

    added = [new_map[i] for i in new_ids - base_ids]
    removed = sorted(base_ids - new_ids)
    unchanged: List[str] = []
    updated: List[PhasePlan] = []
    for i in base_ids & new_ids:
        if base_map[i].model_dump() == new_map[i].model_dump():
            unchanged.append(i)
        else:
            updated.append(new_map[i])

    requires = bool(removed) or any(p.risk_level == "high" for p in added + updated)

    return PlanRevisionDiff(
        base_revision=base.revision,
        new_revision=new.revision,
        added_phases=added,
        removed_phase_ids=removed,
        updated_phases=updated,
        unchanged_phase_ids=unchanged,
        reason=reason,
        requires_user_confirmation=requires,
    )


def build_revise_prompt(spec: PlanSpec, instruction: str) -> str:
    """构建用户指令修订 prompt（纯函数，可单测）。"""
    lines = [f"任务：{spec.task}", "当前阶段："]
    for i, p in enumerate(spec.phases, 1):
        deps = f"（依赖 {', '.join(p.dependencies)}）" if p.dependencies else ""
        lines.append(f"{i}. {p.phase_id}「{p.label}」→ {p.agent_id}{deps}")

    lines.append(f"""
用户要求修改计划：{instruction}

请输出修订后的完整阶段列表，返回 JSON：
{{"phases": [{{"phase_id": "阶段ID", "agent_id": "agent", "label": "名称", "description": "描述",
  "dependencies": ["依赖的phase_id"], "expected_outputs": ["文件"], "acceptance_criteria": ["验收"],
  "on_complete": "continue|retry|review|decision"}}]}}

规则：只改用户要求的部分，其余阶段保持不变；phase_id 保持稳定；dependencies 只引用 phase_id；只返回 JSON。""")
    return "\n".join(lines)


async def revise_plan(
    spec: PlanSpec,
    instruction: str,
    llm_client,
) -> Optional[PlanSpec]:
    """调 LLM 基于用户指令生成新 revision（失败返回 None，调用方保留原计划）。"""
    try:
        prompt = build_revise_prompt(spec, instruction)
        response = await llm_client.chat(prompt)
        if isinstance(response, str) and response.startswith("[LLM"):
            return None
        return parse_refine_response(response, spec)
    except Exception:
        return None
