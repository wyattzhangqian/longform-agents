"""轻量协作协议 — 将 workflow_config 编译为 OrchestrationEngine 运行参数

支持字段：
  pattern: sequential | parallel | roundtable
  review_mode: bool
  default_on_complete: continue | review | retry
  phases: [{ agent_id, on_complete, ... }]
  roles: { moderator, participants[], proposer, ... }
  constraints: { max_rounds, consensus_threshold, max_duration_minutes }
  roundtable_config / parallel_config: 显式覆盖
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CompiledProtocol:
    """编译后的执行计划"""
    mode: str = "sequential"
    review_mode: Optional[bool] = None
    agent_order: List[str] = field(default_factory=list)
    roundtable_config: Dict[str, Any] = field(default_factory=dict)
    parallel_config: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    roles: Dict[str, Any] = field(default_factory=dict)
    phases: List[Dict[str, Any]] = field(default_factory=list)


def compile_protocol(
    workflow_config: Optional[Dict[str, Any]],
    default_mode: str = "sequential",
    default_review: bool = False,
) -> CompiledProtocol:
    """将模板 workflow_config 编译为运行参数"""
    cfg = workflow_config or {}
    constraints = dict(cfg.get("constraints") or {})

    mode = str(cfg.get("pattern") or cfg.get("mode") or default_mode)
    review_mode = cfg.get("review_mode") if "review_mode" in cfg else None

    roles = dict(cfg.get("roles") or {})
    agent_order: List[str] = []

    participants = roles.get("participants")
    if isinstance(participants, list) and participants:
        agent_order = [str(a) for a in participants]
    elif cfg.get("agent_ids"):
        agent_order = [str(a) for a in cfg["agent_ids"]]

    # 角色映射：moderator / proposer 等置于队首（圆桌）
    for key in ("moderator", "proposer", "coordinator"):
        aid = roles.get(key)
        if aid and isinstance(aid, str) and aid not in agent_order:
            if mode == "roundtable":
                agent_order.insert(0, aid)

    rt_cfg = dict(cfg.get("roundtable_config") or {})
    if constraints.get("max_rounds") is not None:
        rt_cfg.setdefault("max_rounds", int(constraints["max_rounds"]))
    if constraints.get("consensus_threshold") is not None:
        rt_cfg.setdefault("threshold", float(constraints["consensus_threshold"]))
    if roles.get("moderator"):
        rt_cfg.setdefault("moderator", roles["moderator"])
    topic = constraints.get("topic") or cfg.get("topic")
    if topic:
        rt_cfg.setdefault("topic", topic)

    parallel_cfg = dict(cfg.get("parallel_config") or {})
    if constraints.get("judge_fn"):
        parallel_cfg.setdefault("judge_fn", constraints["judge_fn"])

    return CompiledProtocol(
        mode=mode,
        review_mode=review_mode if review_mode is not None else default_review,
        agent_order=agent_order,
        roundtable_config=rt_cfg,
        parallel_config=parallel_cfg,
        constraints=constraints,
        roles=roles,
        phases=list(cfg.get("phases") or []),
    )


def merge_run_params(
    compiled: CompiledProtocol,
    request_mode: str,
    request_review: bool,
    roundtable_config: Optional[Dict] = None,
    parallel_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """合并模板协议与 HTTP 请求参数（请求显式字段优先）"""
    mode = request_mode or compiled.mode
    review = request_review
    if compiled.review_mode is not None and not request_review:
        review = bool(compiled.review_mode)

    rt = {**compiled.roundtable_config, **(roundtable_config or {})}
    par = {**compiled.parallel_config, **(parallel_config or {})}

    return {
        "mode": mode,
        "review_mode": review,
        "roundtable_config": rt,
        "parallel_config": par,
        "agent_order": compiled.agent_order,
    }
