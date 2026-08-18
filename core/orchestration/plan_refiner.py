"""PlanRefiner — 计划修订 (Phase 1)

基于 `plan_critic` 的结构化问题列表，调 LLM 生成**新 revision** 的 PlanSpec。
Refiner 不直接原地修改原计划，而是产出新版本（revision+1），保留 plan_id/task/
domain_id/strategy 等计划级字段。

处理顺序（执行方案 4.4）：
    Intent → DraftPlan → 确定性校验 → PlanCritic → PlanRefiner → FinalPlan

失败返回 None（调用方保留原计划，不静默替换）。
"""

from __future__ import annotations
import json
from typing import Optional

from core.run.plan_spec import PlanSpec, PhasePlan
from core.orchestration.plan_critic import PlanCritique


def build_refine_prompt(spec: PlanSpec, critique: PlanCritique) -> str:
    """构建修订 prompt（纯函数，可单测）。"""
    lines = [f"任务：{spec.task}", "当前阶段："]
    for i, p in enumerate(spec.phases, 1):
        deps = f"（依赖 {', '.join(p.dependencies)}）" if p.dependencies else ""
        lines.append(f"{i}. {p.phase_id}「{p.label}」→ {p.agent_id}{deps}")

    if critique.issues:
        lines.append("需要修复的问题：")
        for it in critique.issues:
            lines.append(f"- [{it.kind}] {it.phase_id or '计划级'}：{it.message}")
    else:
        lines.append("（无结构性问题，仅做语义优化）")

    lines.append("""
请输出修订后的完整阶段列表，返回 JSON：
{"phases": [{"phase_id": "阶段ID", "agent_id": "agent", "label": "名称", "description": "描述",
  "dependencies": ["依赖的phase_id"], "expected_outputs": ["文件"], "acceptance_criteria": ["验收"],
  "on_complete": "continue|retry|review|decision"}]}

规则：phase_id 保持稳定（新增阶段才用新 ID）；dependencies 只引用 phase_id；只返回 JSON。""")
    return "\n".join(lines)


def parse_refine_response(response: str, base_spec: PlanSpec) -> Optional[PlanSpec]:
    """解析 LLM 修订输出，生成 revision+1 的新 PlanSpec（失败返回 None）。"""
    try:
        text = response.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        phases_raw = data.get("phases") if isinstance(data, dict) else data
        if not isinstance(phases_raw, list) or not phases_raw:
            return None

        phases = [PhasePlan.from_legacy(p, i) for i, p in enumerate(phases_raw)]
        # 保序重写 dependencies（LLM 可能引用 title，这里做 title→id 兜底）
        title_to_id = {}
        for p in phases:
            for key in ("label", "phase_id"):
                title_to_id[p.phase_id] = p.phase_id
            if p.label:
                title_to_id[p.label] = p.phase_id
        for p in phases:
            p.dependencies = [title_to_id.get(d, d) for d in p.dependencies]

        return base_spec.model_copy(update={
            "revision": base_spec.revision + 1,
            "phases": phases,
        })
    except Exception:
        return None


async def refine_plan(
    spec: PlanSpec,
    critique: PlanCritique,
    llm_client,
) -> Optional[PlanSpec]:
    """调 LLM 生成修订版（失败返回 None，调用方保留原计划）。"""
    try:
        prompt = build_refine_prompt(spec, critique)
        response = await llm_client.chat(prompt)
        if isinstance(response, str) and response.startswith("[LLM"):
            return None
        return parse_refine_response(response, spec)
    except Exception:
        return None
