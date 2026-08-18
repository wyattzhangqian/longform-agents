"""PlanValidator — 计划结构的确定性校验 (Phase 0)

与 PlanCritic（Phase 1，LLM 语义审查）分离：validator 只做**确定性规则**校验，
不调 LLM、不查业务逻辑、不执行 Agent。

校验项（蓝图 §四 + 执行方案 4.4）：
- phase_id 唯一、非空；
- dependencies 引用已存在的 phase_id（不引用 title，不自依赖）；
- 无环；
- execution_layers 与 phases 一致（phase_id 集合相等、无重复）；
- agent 存在（传入 known_agent_ids 时校验）；
- risk_level == "high" → requires_confirmation=True。

失败即返回 errors（不抛异常）；调用方决定是否阻断保存/执行。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from core.run.plan_spec import PlanSpec, PhasePlan


@dataclass
class ValidationResult:
    """确定性校验结果"""
    valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    requires_confirmation: bool = False   # 高风险计划需用户确认（P5）

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def __bool__(self) -> bool:
        return self.valid


def _has_cycle(phases: List[PhasePlan]) -> bool:
    """Kahn 拓扑排序检测环。返回 True 表示有环。"""
    id_set = {p.phase_id for p in phases}
    indegree = {pid: 0 for pid in id_set}
    adj: Dict[str, List[str]] = {pid: [] for pid in id_set}
    for p in phases:
        for dep in p.dependencies:
            if dep in id_set:
                adj[dep].append(p.phase_id)
                indegree[p.phase_id] += 1

    queue = [pid for pid in id_set if indegree[pid] == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in adj[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return visited != len(id_set)


def validate_plan(
    spec: PlanSpec,
    known_agent_ids: Optional[Set[str]] = None,
) -> ValidationResult:
    """确定性校验 PlanSpec 结构。返回 ValidationResult（不抛异常）。

    Args:
        spec: 已标准化的 PlanSpec
        known_agent_ids: 平台已知的 agent id 集合（None=跳过 agent 存在性校验）
    """
    result = ValidationResult()
    ids = [p.phase_id for p in spec.phases]
    id_set = set(ids)

    # 1. phase_id 唯一 + 非空
    seen: Set[str] = set()
    for p in spec.phases:
        if not p.phase_id:
            result.add_error("存在空 phase_id（phase_id 是唯一引用键，不可为空）")
        elif p.phase_id in seen:
            result.add_error(f"重复 phase_id: {p.phase_id}")
        seen.add(p.phase_id)

    # 2. dependencies 引用已存在的 phase_id（不引用 title，不自依赖）
    for p in spec.phases:
        for dep in p.dependencies:
            if not dep:
                result.add_error(f"phase {p.phase_id} 存在空依赖")
            elif dep not in id_set:
                result.add_error(f"phase {p.phase_id} 依赖不存在的 phase: {dep}")
            elif dep == p.phase_id:
                result.add_error(f"phase {p.phase_id} 自依赖（不允许）")

    # 3. 无环（仅当依赖都合法时才测，否则环检测结果无意义）
    if not result.errors and len(ids) > 0:
        if _has_cycle(spec.phases):
            result.add_error("存在循环依赖（DAG 不合法）")

    # 4. execution_layers 与 phases 一致（提供了 execution_layers 时校验）
    if spec.execution_layers:
        flat = [pid for layer in spec.execution_layers for pid in layer]
        if len(flat) != len(set(flat)):
            result.add_error("execution_layers 存在重复 phase_id")
        if set(flat) != id_set:
            missing = id_set - set(flat)
            extra = set(flat) - id_set
            if missing:
                result.add_error(f"execution_layers 缺失 phase: {sorted(missing)}")
            if extra:
                result.add_error(f"execution_layers 含未知 phase: {sorted(extra)}")

    # 5. agent 存在（传 known_agent_ids 时）
    if known_agent_ids is not None:
        for p in spec.phases:
            if p.agent_id and p.agent_id not in known_agent_ids:
                result.add_error(f"phase {p.phase_id} 引用未知 agent: {p.agent_id}")

    # 6. 高风险 → 需用户确认（P5：不可逆/高风险不静默执行）
    if any(p.risk_level == "high" for p in spec.phases):
        result.requires_confirmation = True
        result.add_warning("计划含高风险阶段（risk_level=high），执行前需用户确认")

    return result
