"""自然语言→DAG 规划器：将用户描述转化为结构化阶段定义

核心流程：
1. LLM 理解用户意图 → 生成 PhaseSpec[]（阶段定义+依赖关系）
2. _validate_and_fix_plan 校验修复 LLM 输出
3. 下游 Agent Matcher + TemplateCompiler 消费
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DAG_PLANNER_PROMPT = """你是一个工作流规划专家。用户会描述一个任务，你需要将其拆解为多个协作阶段。

## 输出格式（严格 JSON）

```json
{
  "phases": [
    {
      "phase_id": "phase_1_research",
      "title": "市场调研",
      "description": "收集竞品信息和市场数据",
      "required_capabilities": ["research", "data_collection"],
      "dependencies": [],
      "estimated_output": "调研报告（含数据和来源）"
    },
    {
      "phase_id": "phase_2_writing",
      "title": "报告撰写",
      "description": "基于调研结果撰写分析报告",
      "required_capabilities": ["writing", "analysis"],
      "dependencies": ["phase_1_research"],
      "estimated_output": "完整分析报告"
    }
  ],
  "collaboration_config": {
    "enable_shared_thread": true,
    "enable_negotiation": true,
    "quality_gate_after": ["phase_2_writing"]
  }
}
```

## 规则

1. phase_id 必须唯一，格式：phase_N_关键词
2. dependencies 引用已定义的 phase_id
3. required_capabilities 必须严格从以下列表中选择，不要自创或翻译标签：{capability_list}
4. 阶段数量 2-6 个，不要过度拆分
5. 最后一个阶段如果是审核/检查类，capabilities 用 ["review", "quality_check"]
6. collaboration_config.quality_gate_after 放在关键产出阶段之后

## 用户输入
{user_input}

请只输出 JSON，不要其他文字。"""


async def plan_dag_from_natural_language(
    user_input: str,
    capabilities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """将自然语言描述转化为 DAG 阶段定义。

    capabilities: 可用能力池（2026-08-10 修复）——传领域能力池时，LLM 可输出
    领域专用能力（如 storyboard/character_design），natural 路径就能匹配到
    domain 专用 agent。不传则用默认 11 个通用能力（向后兼容）。
    """
    from tools.llm_client import LLMClient

    client = LLMClient()

    if capabilities:
        capability_list = ", ".join(dict.fromkeys(capabilities))
    else:
        capability_list = (
            "research, analysis, writing, review, planning, data_collection, "
            "content_creation, quality_check, summarization, execution, general"
        )
    prompt = DAG_PLANNER_PROMPT.replace("{user_input}", user_input, 1) \
                               .replace("{capability_list}", capability_list, 1)

    response = await client.chat(prompt, system_prompt="你是一个工作流规划专家", temperature=0.3)

    # 解析 JSON
    raw = response.strip()
    # 处理可能的 markdown 包裹
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("{"):
                raw = part
                break
            if part.startswith("json"):
                raw = part[4:].strip()
                break

    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("DAG plan JSON parse failed: %s\nRaw: %s", e, raw[:200])
        plan = _fallback_plan(user_input)

    plan = _validate_and_fix_plan(plan)
    return plan


def _validate_and_fix_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """校验并修复 DAG 计划"""
    phases: List[Dict[str, Any]] = plan.get("phases", [])

    # 确保 phase_id 唯一
    seen_ids: set = set()
    for i, phase in enumerate(phases):
        if not phase.get("phase_id"):
            phase["phase_id"] = f"phase_{i + 1}"
        if phase["phase_id"] in seen_ids:
            phase["phase_id"] = f"{phase['phase_id']}_{i}"
        seen_ids.add(phase["phase_id"])

    # 过滤无效依赖
    valid_ids = {p["phase_id"] for p in phases}
    for phase in phases:
        phase["dependencies"] = [
            d for d in phase.get("dependencies", [])
            if d in valid_ids and d != phase["phase_id"]
        ]

    # 确保至少有一个阶段
    if not phases:
        phases = [{
            "phase_id": "phase_1_execute",
            "title": "执行",
            "description": "执行用户任务",
            "required_capabilities": ["general"],
            "dependencies": [],
        }]
        plan["phases"] = phases

    if "collaboration_config" not in plan:
        plan["collaboration_config"] = {
            "enable_shared_thread": True,
            "enable_negotiation": True,
            "quality_gate_after": [],
        }

    return plan


def _fallback_plan(user_input: str) -> Dict[str, Any]:
    """LLM 解析失败的兜底：单阶段通用执行"""
    return {
        "phases": [{
            "phase_id": "phase_1_execute",
            "title": "执行任务",
            "description": user_input,
            "required_capabilities": ["general"],
            "dependencies": [],
        }],
        "collaboration_config": {
            "enable_shared_thread": False,
            "enable_negotiation": False,
            "quality_gate_after": [],
        },
    }
