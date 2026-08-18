"""PlanSpec — 统一计划契约 (Phase 0)

Planner 输出 / 前端保存 / plans.phases / PhaseSpec / WorkflowGraph 五处必须使用同一份
计划契约。本模块只放 Pydantic 契约 + 旧计划兼容读取（normalizer），
**不放 LLM 调用，不放执行逻辑**。

与 `core/run/phase_spec.py` 的分工：
- `PhasePlan`（本模块）= 计划阶段的**存储契约**（Planner 生成、前端展示、持久化）。
- `PhaseSpec`（phase_spec.py）= 运行时的**执行契约**（TemplateCompiler 编译产物）。
- 两者字段对齐，但 `PhasePlan` 不引用运行时组件（如 project_artifact_service），
  避免循环依赖。`PhasePlan → PhaseSpec` 的转换在 compile_from_plan（PR-2）完成。

关键契约原则（蓝图 §四）：
- `phase_id` 是唯一引用键，dependencies 不能引用 title；
- title/label 仅用于展示，不参与拓扑计算；
- normalizer 兼容旧计划（title 依赖、缺字段），但新计划不再走多套默认值。
"""

from __future__ import annotations
import json
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ── 值域（与 phase_spec.py 对齐，作为"目标契约"文档化合法值） ──
Strategy = Literal["pipeline", "dag", "roundtable", "adaptive"]
DialoguePolicy = Literal["none", "handoff_only", "shared_thread"]
# 计划层语义用 "review"（编译期映射到 phase_spec 的 "human_review"）
OnComplete = Literal["continue", "retry", "review", "decision", "revise_loop"]
ToolPolicy = Literal["auto", "on_demand", "disabled"]


class PlanConstraints(BaseModel):
    """约束：硬约束 / 软偏好 / 隐含假设 / 待确认问题"""
    hard: List[str] = Field(default_factory=list)
    soft: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)


class DeliverableSpec(BaseModel):
    """最终交付物定义"""
    id: str
    type: str = "artifact"
    format: str = "markdown"
    description: str = ""
    acceptance_criteria: List[str] = Field(default_factory=list)


class PhasePlan(BaseModel):
    """计划中单个阶段的完整契约（覆盖 PhaseSpec 字段，不设计平行字段）"""

    phase_id: str
    agent_id: str
    label: str = ""
    objective: str = ""
    description: str = ""
    dependencies: List[str] = Field(default_factory=list)
    expected_inputs: List[str] = Field(default_factory=list)
    expected_outputs: List[str] = Field(default_factory=list)
    expected_artifacts: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)
    dialogue_policy: DialoguePolicy = "shared_thread"
    on_complete: OnComplete = "continue"
    collaboration: str = "pipeline"
    quality_domain_ids: List[str] = Field(default_factory=list)
    quality_profile: Dict[str, Any] = Field(default_factory=dict)
    knowledge_packs: List[str] = Field(default_factory=list)
    knowledge_top_k: int = 5
    tool_policy: ToolPolicy = "auto"
    tool_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    max_retries: int = 3
    budget: Dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "medium"
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_legacy(cls, raw: Dict[str, Any], index: int) -> "PhasePlan":
        """从旧 phase dict 标准化（单阶段，缺字段补默认值，值归一不崩）。

        phase_id 兜底顺序：phase_id → id → step → f"phase_{index}"。
        不在这里做 title→id 依赖重写（需要全量 phase 映射，见 PlanSpec.normalize）。
        """
        if not isinstance(raw, dict):
            raw = {}
        phase_id = raw.get("phase_id") or raw.get("id") or raw.get("step") or f"phase_{index}"
        agent_id = raw.get("agent_id") or raw.get("id") or "builtin_general"
        return cls(
            phase_id=phase_id,
            agent_id=agent_id,
            label=raw.get("label") or raw.get("title") or raw.get("name") or phase_id,
            objective=raw.get("objective", ""),
            description=raw.get("description", ""),
            dependencies=_as_str_list(raw.get("dependencies", [])),
            expected_inputs=_as_str_list(raw.get("expected_inputs", [])),
            expected_outputs=_as_str_list(raw.get("expected_outputs", [])),
            expected_artifacts=_as_str_list(
                raw.get("expected_artifacts") or raw.get("expected_outputs") or []
            ),
            acceptance_criteria=_as_str_list(raw.get("acceptance_criteria", [])),
            dialogue_policy=_coerce(raw.get("dialogue_policy"), ("none", "handoff_only", "shared_thread"), "shared_thread"),
            on_complete=_coerce_on_complete(raw.get("on_complete")),
            collaboration=raw.get("collaboration", "pipeline"),
            quality_domain_ids=_as_str_list(raw.get("quality_domain_ids", [])),
            quality_profile=raw.get("quality_profile") or {},
            knowledge_packs=_as_str_list(raw.get("knowledge_packs", [])),
            knowledge_top_k=int(raw.get("knowledge_top_k") or 5),
            tool_policy=_coerce(raw.get("tool_policy"), ("auto", "on_demand", "disabled"), "auto"),
            tool_bindings=raw.get("tool_bindings") or [],
            max_retries=int(raw.get("max_retries") or 3),
            budget=raw.get("budget") or {},
            risk_level=raw.get("risk_level", "medium"),
            metadata=raw.get("metadata") or {},
        )


class PlanSpec(BaseModel):
    """一份完整的、可验证、可编辑、可恢复的计划契约"""

    plan_id: str = ""
    schema_version: int = 1
    revision: int = 1
    task: str = ""
    domain_id: str = ""
    task_type: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    strategy: Strategy = "pipeline"
    mode: str = "sequential"
    phases: List[PhasePlan] = Field(default_factory=list)
    execution_layers: List[List[str]] = Field(default_factory=list)
    deliverables: List[DeliverableSpec] = Field(default_factory=list)
    constraints: PlanConstraints = Field(default_factory=PlanConstraints)
    reasoning: str = ""
    match_details: List[Dict[str, Any]] = Field(default_factory=list)
    status: str = "draft"
    long_content_config: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_legacy(cls, data: Dict[str, Any]) -> "PlanSpec":
        """从旧 plans 记录 / Planner SSE 输出标准化为 PlanSpec（兼容读取，不崩）。

        输入来源：
        - 旧 plans 记录：{"phases": [...], "execution_layers": [...], ...}（phases 可能是 JSON str）
        - Planner SSE 输出：{"pipeline": [...], "strategy": ..., ...}

        dependencies 的 title→phase_id 重写在这里完成（能看到全量 phase 映射）。
        """
        if not isinstance(data, dict):
            data = {}
        phases_raw = data.get("phases") if data.get("phases") is not None else data.get("pipeline")
        phases_raw = _as_phase_list(phases_raw)

        # 第一遍：为每个 phase 确定稳定 phase_id，建立 title/id → phase_id 映射
        title_to_id: Dict[str, str] = {}
        id_to_id: Dict[str, str] = {}
        normalized: List[PhasePlan] = []
        for i, raw in enumerate(phases_raw):
            if isinstance(raw, PhasePlan):
                p = raw  # 已是标准契约，直接沿用（幂等）
            else:
                p = PhasePlan.from_legacy(raw, i)
                if isinstance(raw, dict):
                    for key in ("title", "name", "label"):
                        v = raw.get(key)
                        if v:
                            title_to_id.setdefault(v, p.phase_id)
                    if raw.get("id") and raw.get("id") != p.phase_id:
                        id_to_id[raw["id"]] = p.phase_id
            normalized.append(p)

        # 第二遍：重写 dependencies（title/旧 id → phase_id）
        for p in normalized:
            p.dependencies = [_resolve_dep(d, title_to_id, id_to_id) for d in p.dependencies]

        # execution_layers 同理：title/id → phase_id
        layers = data.get("execution_layers") or []
        if isinstance(layers, str):
            try:
                layers = json.loads(layers)
            except Exception:
                layers = []
        execution_layers = [
            [_resolve_dep(sid, title_to_id, id_to_id) for sid in layer]
            for layer in layers
            if isinstance(layer, (list, tuple))
        ]

        return cls(
            plan_id=data.get("plan_id") or data.get("id") or "",
            schema_version=int(data.get("schema_version") or 1),
            revision=int(data.get("revision") or 1),
            task=data.get("task") or data.get("original_task") or "",
            domain_id=data.get("domain_id", ""),
            task_type=data.get("task_type", ""),
            confidence=_coerce_float(data.get("confidence"), 0.0, 1.0),
            strategy=_coerce(data.get("strategy"), ("pipeline", "dag", "roundtable", "adaptive"), "pipeline"),
            mode=data.get("mode", "sequential"),
            phases=normalized,
            execution_layers=execution_layers,
            deliverables=[DeliverableSpec.model_validate(d) for d in (data.get("deliverables") or [])],
            constraints=PlanConstraints.model_validate(data.get("constraints") or {}),
            reasoning=data.get("reasoning") or "",
            match_details=data.get("match_details") or [],
            status=data.get("status", "draft"),
            long_content_config=data.get("long_content_config") or {},
            metadata=data.get("metadata") or {},
        )


def normalize_plan(data: Any) -> PlanSpec:
    """统一入口：任意来源（dict / PlanSpec）→ 标准 PlanSpec。幂等。"""
    if isinstance(data, PlanSpec):
        return data
    if not isinstance(data, dict):
        return PlanSpec()
    return PlanSpec.from_legacy(data)


# ═══════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════

_ON_COMPLETE_ALIASES = {
    "human_review": "review",   # phase_spec 运行时值 → 计划层语义
    "review_gate": "review",
    "auto": "continue",
}
_ON_COMPLETE_VALUES = ("continue", "retry", "review", "decision", "revise_loop")


def _as_str_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x]
    return []


def _as_phase_list(v: Any) -> List[Any]:
    """phases/pipeline 可能是 JSON str / list of dict / list of PhasePlan。"""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    if isinstance(v, list):
        return v
    return []


def _coerce(v: Any, allowed, default: str) -> Any:
    return v if v in allowed else default


def _coerce_on_complete(v: Any) -> str:
    if not isinstance(v, str):
        return "continue"
    v = _ON_COMPLETE_ALIASES.get(v, v)
    return v if v in _ON_COMPLETE_VALUES else "continue"


def _coerce_float(v: Any, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, f))


def _resolve_dep(dep: Any, title_to_id: Dict[str, str], id_to_id: Dict[str, str]) -> str:
    """把依赖值解析为 phase_id：title → id，旧 id → id，已是 id 则原样。找不到则原样保留（PR-3 validator 会拒绝）。"""
    s = str(dep) if dep is not None else ""
    if not s:
        return s
    if s in title_to_id:
        return title_to_id[s]
    if s in id_to_id:
        return id_to_id[s]
    return s
