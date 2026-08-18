"""RunBudget — 协作成本预算（P1）

每个项目运行维护 token / LLM 调用计数；超出预算后 LLM 调用被熔停，
图执行会以错误内容收尾而不是无限烧钱。

归属方式：CollaborationGraph.execute/resume 在协程上下文设置
budget scope（contextvar），运行内所有 LLM 调用自动归属到该项目。
预算为 0 表示不限制。
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_scope: ContextVar[str] = ContextVar("run_budget_scope", default="")

# project_id → 计数器
_counters: Dict[str, Dict[str, float]] = {}

# #12 单 Agent 分层预算（agent_id → 计数器 + scope contextvar）
_agent_scope_ctx: ContextVar[str] = ContextVar("agent_budget_scope", default="")
_agent_counters: Dict[str, Dict[str, float]] = {}


def set_agent_budget_scope(agent_id: str) -> None:
    """将当前协程上下文归属到指定 agent（每次 Agent.execute 入口调用）"""
    _agent_scope_ctx.set(agent_id or "")
    if agent_id:
        _agent_counters.setdefault(agent_id, {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "started_at": time.time(),
        })


def set_budget_scope(project_id: str) -> None:
    """将当前协程上下文的 LLM 调用归属到 project_id"""
    _scope.set(project_id or "")


def get_budget_scope() -> str:
    return _scope.get()


def reset_budget(project_id: str) -> None:
    """新一次运行开始时清零计数"""
    if project_id:
        _counters[project_id] = {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "started_at": time.time(), "notified": 0,
        }


def record_usage(prompt_tokens: int, completion_tokens: int) -> None:
    """记录一次 LLM 调用（归属到当前 scope；无 scope 则忽略）

    #12：同时归属到当前 agent scope（分层预算）
    """
    pid = _scope.get()
    if not pid:
        return
    c = _counters.setdefault(pid, {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "started_at": time.time(), "notified": 0,
    })
    c["calls"] += 1
    c["prompt_tokens"] += int(prompt_tokens or 0)
    c["completion_tokens"] += int(completion_tokens or 0)

    # #12 归属到 agent 级计数
    aid = _agent_scope_ctx.get()
    if aid:
        ac = _agent_counters.setdefault(aid, {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "started_at": time.time(),
        })
        ac["calls"] += 1
        ac["prompt_tokens"] += int(prompt_tokens or 0)
        ac["completion_tokens"] += int(completion_tokens or 0)


def check_budget() -> Optional[str]:
    """超出预算返回原因字符串；未超出或无 scope 返回 None

    分层预算：项目级（RUN_TOKEN_BUDGET） + 单 Agent 级（AGENT_TOKEN_BUDGET）
    """
    pid = _scope.get()
    if not pid:
        return None
    try:
        from config import RUN_TOKEN_BUDGET, RUN_LLM_CALL_BUDGET
        import os as _os
        _agent_token_budget = int(_os.getenv("AGENT_TOKEN_BUDGET", "0"))
        _agent_call_budget = int(_os.getenv("AGENT_CALL_BUDGET", "0"))
    except (ImportError, ValueError):
        return None
    c = _counters.get(pid)
    if not c:
        return None

    reason = None
    total_tokens = c["prompt_tokens"] + c["completion_tokens"]
    if RUN_LLM_CALL_BUDGET > 0 and c["calls"] >= RUN_LLM_CALL_BUDGET:
        reason = f"LLM 调用次数已达预算上限（{int(c['calls'])}/{RUN_LLM_CALL_BUDGET}）"
    elif RUN_TOKEN_BUDGET > 0 and total_tokens >= RUN_TOKEN_BUDGET:
        reason = f"token 用量已达预算上限（{int(total_tokens)}/{RUN_TOKEN_BUDGET}）"

    # #12 单 Agent 分层预算：当前 agent 的累计 token / 调用数
    _agent_scope = _agent_scope_ctx.get()
    if _agent_scope and not reason:
        ac = _agent_counters.get(_agent_scope)
        if ac:
            a_total = ac["prompt_tokens"] + ac["completion_tokens"]
            if _agent_call_budget > 0 and ac["calls"] >= _agent_call_budget:
                reason = f"Agent {_agent_scope} 调用次数达上限（{int(ac['calls'])}/{_agent_call_budget}）"
            elif _agent_token_budget > 0 and a_total >= _agent_token_budget:
                reason = f"Agent {_agent_scope} token 用量达上限（{int(a_total)}/{_agent_token_budget}）"

    if reason and not c.get("notified"):
        c["notified"] = 1
        logger.warning("项目 %s 成本预算触顶: %s", pid, reason)
        try:
            from core.events import event_bus
            event_bus.broadcast(
                event="log",
                data={"message": f"💸 成本预算触顶，后续 LLM 调用已熔停: {reason}", "level": "warning"},
                project_id=pid,
            )
        except Exception:
            pass
    return reason


def budget_snapshot(project_id: str) -> Dict[str, float]:
    """当前用量快照（API / 测试用）"""
    c = _counters.get(project_id)
    if not c:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "calls": int(c["calls"]),
        "prompt_tokens": int(c["prompt_tokens"]),
        "completion_tokens": int(c["completion_tokens"]),
        "total_tokens": int(c["prompt_tokens"] + c["completion_tokens"]),
    }
