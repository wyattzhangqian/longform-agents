"""PhaseSpec — 阶段一等结构 (D3)

模板、SSE、质量、知识均通过 phase_id 寻址。
"""

from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


CollaborationMode = str  # 历史数据兼容：旧项目可能有 competitive/delegated 值，不做枚举校验
DialoguePolicy = Literal["none", "handoff_only", "shared_thread"]
PhaseExitAction = Literal["continue", "retry", "revise_loop", "human_review", "decision"]
ToolPolicy = Literal["auto", "on_demand", "disabled"]


class QualityProfile(BaseModel):
    """阶段质量配置 (D5) — 由 DomainBundle 或模板声明"""
    rule_set_ref: str = ""
    domain_ids: List[str] = Field(default_factory=list)
    pack_rules: List[Dict[str, Any]] = Field(default_factory=list)
    pass_threshold: float = 0.0
    on_fail: PhaseExitAction = "continue"
    max_retries: int = 3


class KnowledgeProfile(BaseModel):
    """阶段知识注入配置 (D4) — 只读附件，不进入 RunContext 可变区"""
    packs: List[str] = Field(default_factory=list)  # domain_id 或 project.attachments
    top_k: int = 5


class PhaseExitPolicy(BaseModel):
    on_pass: Literal["continue", "next_phase"] = "continue"
    on_fail: PhaseExitAction = "continue"
    max_retries: int = 3


class PhaseSpec(BaseModel):
    """单个协作阶段的完整契约"""

    id: str
    agent_id: str
    label: str = ""
    collaboration: CollaborationMode = "pipeline"
    exit: PhaseExitPolicy = Field(default_factory=PhaseExitPolicy)
    quality_profile: QualityProfile = Field(default_factory=QualityProfile)
    knowledge_profile: KnowledgeProfile = Field(default_factory=KnowledgeProfile)
    dialogue_policy: DialoguePolicy = "shared_thread"
    # ── 协作契约（v3.2·运行时框架注入，不来自Agent system_prompt）──
    upstream_phase_id: Optional[str] = None     # 上游阶段ID（null=首个阶段）
    downstream_phase_id: Optional[str] = None   # 下游阶段ID（null=最后阶段）
    expected_inputs: List[str] = Field(default_factory=list)   # 需读取的上游文件名
    expected_outputs: List[str] = Field(default_factory=list)  # 需产出的文件名
    tool_policy: ToolPolicy = "auto"            # 工具触发策略
    tool_bindings: List[Dict[str, Any]] = Field(default_factory=list)  # 节点级确定性工具触发
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_legacy_phase(cls, raw: Dict[str, Any], index: int) -> "PhaseSpec":
        from core.vault.project_artifact_service import expected_artifacts_for_phase

        agent_id = raw.get("agent_id") or raw.get("id") or f"phase_{index}"
        phase_id = raw.get("phase_id") or raw.get("step") or agent_id
        on_complete = str(raw.get("on_complete", "continue"))
        exit_policy = PhaseExitPolicy()
        quality = QualityProfile()
        if on_complete == "review":
            exit_policy.on_fail = "human_review"
        elif on_complete == "retry":
            exit_policy.on_fail = "retry"
            quality.on_fail = "retry"
        elif on_complete == "revise_loop":
            exit_policy.on_fail = "revise_loop"
            quality.on_fail = "revise_loop"
            quality.max_retries = int(raw.get("max_retries") or 3)
        elif on_complete == "decision":
            exit_policy.on_fail = "decision"
        domain_ids = list(raw.get("quality_domain_ids") or [])
        if domain_ids:
            quality.domain_ids = domain_ids
        # Phase 0：无损读取完整阶段契约字段（expected_inputs/outputs/tool/知识 top_k/max_retries）
        expected = expected_artifacts_for_phase(raw, phase_id)
        expected_inputs = list(raw.get("expected_inputs") or [])
        expected_outputs = list(raw.get("expected_outputs") or [])
        max_retries = int(raw.get("max_retries") or 3)
        if expected:
            quality.on_fail = "retry"
            quality.max_retries = max_retries
            quality.pack_rules.append({
                "rule_id": f"{phase_id}_artifact_file",
                "name": f"期望产物 {expected[0]}",
                "check_type": "artifact_file",
                "severity": "error",
                "config": {
                    "expected_files": expected,
                    "phase_id": phase_id,
                },
            })
        # 兼容完整 quality_profile dict（PhasePlan 保存的 rule_set_ref/pack_rules 等）
        raw_qp = raw.get("quality_profile")
        if isinstance(raw_qp, dict):
            if raw_qp.get("rule_set_ref"):
                quality.rule_set_ref = raw_qp["rule_set_ref"]
            if raw_qp.get("pass_threshold") is not None:
                quality.pass_threshold = float(raw_qp["pass_threshold"])
            for rule in raw_qp.get("pack_rules") or []:
                quality.pack_rules.append(rule)
            if raw_qp.get("max_retries") is not None:
                quality.max_retries = int(raw_qp["max_retries"])
            if raw_qp.get("domain_ids"):
                quality.domain_ids = list(raw_qp["domain_ids"])
        knowledge_packs = list(raw.get("knowledge_packs") or [])
        knowledge_top_k = int(raw.get("knowledge_top_k") or 5)
        meta = {k: v for k, v in raw.items() if k not in (
            "agent_id", "id", "label", "on_complete", "phase_id",
        )}
        if expected and "expected_artifacts" not in meta:
            meta["expected_artifacts"] = expected
        return cls(
            id=phase_id,
            agent_id=agent_id,
            label=raw.get("label") or agent_id,
            collaboration=raw.get("collaboration", "pipeline"),
            exit=exit_policy,
            quality_profile=quality,
            knowledge_profile=KnowledgeProfile(packs=knowledge_packs, top_k=knowledge_top_k),
            dialogue_policy=raw.get("dialogue_policy", "shared_thread"),
            expected_inputs=expected_inputs,
            expected_outputs=expected_outputs,
            tool_policy=raw.get("tool_policy", "auto"),
            tool_bindings=list(raw.get("tool_bindings") or []),
            metadata=meta,
        )

    @classmethod
    def from_agent_ids(
        cls,
        agent_ids: List[str],
        default_on_complete: str = "continue",
        labels: Optional[Dict[str, str]] = None,
        dependencies_map: Optional[Dict[str, List[str]]] = None,
    ) -> List["PhaseSpec"]:
        """从 agent ID 列表创建 PhaseSpec 列表

        Args:
            agent_ids: Agent ID 列表
            default_on_complete: 默认完成策略
            labels: phase_id → 显示名称的映射
            dependencies_map: phase_id → 依赖的 phase_id 列表
        """
        labels = labels or {}
        # 未传 / 空 map：默认串成 pipeline，避免被拓扑推断成 parallel fork
        inject_linear = not dependencies_map
        dep_map = dependencies_map or {}
        specs: List[PhaseSpec] = []
        seen: Dict[str, int] = {}
        prev_phase_id: Optional[str] = None
        for i, aid in enumerate(agent_ids):
            # 同一个 agent 出现多次时自动生成唯一 phase_id: agent_0, agent_1, ...
            cnt = seen.get(aid, 0)
            seen[aid] = cnt + 1
            phase_id = f"{aid}_{cnt}" if cnt > 0 else aid
            raw = {
                "agent_id": aid,
                "phase_id": phase_id,
                "on_complete": default_on_complete,
                "label": labels.get(aid, aid),
            }
            spec = cls.from_legacy_phase(raw, i)
            if phase_id in dep_map:
                spec.metadata["dependencies"] = dep_map[phase_id]
            elif inject_linear and prev_phase_id is not None:
                spec.metadata["dependencies"] = [prev_phase_id]
            specs.append(spec)
            prev_phase_id = phase_id
        return specs

    @classmethod
    def from_plan(
        cls,
        plan: Any,
        match_details: Optional[List[Dict[str, Any]]] = None,
    ) -> List["PhaseSpec"]:
        """从 DecompositionPlan 构建富契约 PhaseSpec 列表（保留 dependencies/对话策略/拓扑）

        替代 from_agent_ids——不丢弃 plan 中的 dependencies/dialogue_policy/on_complete。
        消费端 context_builder._build_collaboration_protocol 期望 upstream/downstream/expected_outputs。
        """
        # 兼容 DecompositionPlan 对象和 dict
        if hasattr(plan, "sub_tasks"):
            sub_tasks = plan.sub_tasks
            execution_order = plan.execution_order if hasattr(plan, "execution_order") else []
        else:
            sub_tasks = plan.get("sub_tasks", [])
            execution_order = plan.get("execution_order", [])

        # 建立 task_id/title → sub_task 映射
        task_by_id: Dict[str, Any] = {}
        task_by_title: Dict[str, Any] = {}
        for t in sub_tasks:
            task_by_id[t.id] = t
            if t.title:
                task_by_title[t.title] = t

        # 扁平化 execution_order（拓扑分层）为有序列表
        flat_order: List[str] = []
        for layer in execution_order:
            for tid in layer:
                if tid not in flat_order:
                    flat_order.append(tid)

        # 如果 execution_order 为空，用 sub_tasks 原始顺序
        if not flat_order:
            for t in sub_tasks:
                flat_order.append(t.id)

        # 构建 match_detail 的 title → agent 映射
        match_map: Dict[str, str] = {}
        if match_details:
            for d in match_details:
                title = d.get("subtask_title", "")
                agent_id = d.get("agent_id", "")
                if title and agent_id:
                    match_map[title] = agent_id

        specs: List["PhaseSpec"] = []
        for i, task_id in enumerate(flat_order):
            task = task_by_id.get(task_id) or task_by_title.get(task_id)
            if task is None:
                # 找不到对应 task，用 id 作为 fallback
                agent_id = match_map.get(task_id, task_id)
                raw = {
                    "agent_id": agent_id,
                    "phase_id": task_id,
                    "label": task_id,
                    "on_complete": "continue",
                    "dialogue_policy": "shared_thread",
                }
                spec = cls.from_legacy_phase(raw, i)
                specs.append(spec)
                continue

            agent_id = task.assigned_agent or match_map.get(task.title, task.id)
            raw = {
                "agent_id": agent_id,
                "phase_id": task.id,
                "label": task.title or task.id,
                "on_complete": task.on_complete or "continue",
                "dialogue_policy": task.dialogue_policy or "shared_thread",
            }
            spec = cls.from_legacy_phase(raw, i)

            # 注入 dependencies
            deps = task.dependencies or []
            if deps:
                spec.metadata["dependencies"] = deps
                # 设置上游（首个依赖作为 upstream）
                if deps:
                    spec.upstream_phase_id = deps[0]

            # 设置上下游契约（相邻阶段兜底：仅当未通过 deps 设置时才用线性顺序）
            if i > 0 and not spec.upstream_phase_id:
                spec.upstream_phase_id = flat_order[i - 1]

            # Phase 5 新增：critical 单元 → competition 模式
            task_meta = getattr(task, 'metadata', None)
            if isinstance(task_meta, dict) and task_meta.get("critical"):
                spec.collaboration = "competition"
                spec.metadata["competition_config"] = {
                    "variants": 3,
                    "variant_instructions": [
                        "按时间顺序叙述，以动作和事件推进为主。",
                        "从最紧张的一刻开始然后闪回。深入角色内心。",
                        "以对话推进，像剧本一样展现冲突。",
                    ],
                }

            specs.append(spec)

        # 设置 downstream（从后往前）
        for i in range(len(specs) - 1):
            specs[i].downstream_phase_id = specs[i + 1].id if i + 1 < len(specs) else None

        return specs
