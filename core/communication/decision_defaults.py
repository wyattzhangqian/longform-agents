"""默认决策选项 — 圆桌未产出观点 / options 为空时使用"""

from __future__ import annotations

from typing import Any, Dict, List

from core.agent.types import DecisionOption


def default_decision_option_dicts() -> List[Dict[str, Any]]:
    return [
        {
            "id": "retry_roundtable",
            "label": "继续讨论一轮",
            "agent_id": "",
            "agent_view": "在现有讨论基础上再跑一轮圆桌，尝试重新达成共识。",
            "confidence": 0.5,
        },
        {
            "id": "accept_partial",
            "label": "采纳当前最佳观点",
            "agent_id": "",
            "agent_view": "不再等待完全一致，由您选定方向后继续后续流程。",
            "confidence": 0.5,
        },
        {
            "id": "abort_run",
            "label": "终止本次运行",
            "agent_id": "",
            "agent_view": "结束工作流，保留当前讨论记录供复盘。",
            "confidence": 0.5,
        },
    ]


def default_decision_options() -> List[DecisionOption]:
    return [DecisionOption(**d) for d in default_decision_option_dicts()]


def ensure_decision_options(options: List[Any]) -> List[DecisionOption]:
    """将任意 options 列表规范为 DecisionOption；空则返回默认三项。"""
    if not options:
        return default_decision_options()
    out: List[DecisionOption] = []
    for item in options:
        if isinstance(item, DecisionOption):
            out.append(item)
        elif isinstance(item, dict) and item.get("id"):
            out.append(DecisionOption(**item))
    return out if out else default_decision_options()


DEFAULT_DECISION_QUESTION = "经多轮讨论未达成共识，请选择后续方向"
