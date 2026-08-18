"""Legacy 路径适配 — auto_plan 编译为 PhaseSpec (D1，无 Engine stub)"""

from __future__ import annotations
from typing import List, Tuple

from core.logging import get_logger
from core.orchestration.planner import plan_agent_order
from core.run.phase_spec import PhaseSpec

_logger = get_logger("run.legacy_adapter")


async def auto_plan_phase_specs(
    task: str,
    *,
    use_llm: bool = True,
    domain_id: str = "",
) -> List[PhaseSpec]:
    """TaskDecomposer + TeamBuilder → PhaseSpec 列表（富契约：保留 dependencies/对话策略/拓扑）"""
    plan, _, agent_order, match_details, _, _ = await plan_agent_order(task, use_llm=use_llm, domain_id=domain_id)
    if not agent_order:
        raise ValueError("自动规划未产生可用 Agent 顺序")
    _logger.info("auto_plan → %d phases: %s", len(agent_order), agent_order)
    # 使用 from_plan 保留 dependencies/dialogue_policy/on_complete/execution_order
    specs = PhaseSpec.from_plan(plan, match_details=match_details)
    if not specs:
        # 兜底：from_plan 无产出时回退到 from_agent_ids
        specs = PhaseSpec.from_agent_ids(agent_order)
    # 把匹配度元信息附加到每个 phase 的 metadata 里
    detail_map = {d["subtask_title"]: d for d in match_details}
    for spec in specs:
        detail = detail_map.get(spec.label, {})
        if detail:
            spec.metadata["match"] = detail
    return specs


async def resolve_run_plan(
    task: str,
    *,
    template=None,
    agent_ids: List[str] | None = None,
    auto_plan: bool = False,
    use_llm_decompose: bool = True,
    domain_id: str = "",
) -> Tuple[List[PhaseSpec], str]:
    from core.run.template_compiler import TemplateCompiler

    if template is not None:
        specs = TemplateCompiler.parse_phases_from_template(template, domain_id=domain_id)
        wf = template.workflow_config if hasattr(template, "workflow_config") else {}
        mode = str((wf or {}).get("pattern") or (wf or {}).get("mode") or "sequential")
        return specs, mode

    if agent_ids:
        return PhaseSpec.from_agent_ids(agent_ids), "sequential"

    if auto_plan or not agent_ids:
        specs = await auto_plan_phase_specs(task, use_llm=use_llm_decompose, domain_id=domain_id)
        return specs, "sequential"

    raise ValueError("无法解析 Run 计划：需要 template、agent_ids 或 auto_plan")
