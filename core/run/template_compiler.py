"""TemplateCompiler — PhaseSpec[] → WorkflowGraph (D1/D3)

支持 LRU 编译缓存，相同参数重复编译走缓存 < 1ms。
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from collections import OrderedDict
import copy
import hashlib
import json
import logging

_logger = logging.getLogger(__name__)

from core.graph.builder import GraphBuilder
from core.graph.types import ConditionType, GraphCondition, MergeStrategy, WorkflowGraph
from core.run.phase_spec import PhaseSpec

# ============================================================
# 模块级编译缓存（因为 compile 是 @staticmethod）
# ============================================================

_COMPILE_CACHE: OrderedDict = OrderedDict()
_CACHE_MAX = 50


def _specs_cache_digest(specs) -> List[Dict[str, Any]]:
    """缓存用：纳入完整 PhaseSpec 字段，避免不同契约撞同一 MD5。

    Phase 0 修复：之前只纳入 id/agent_id/dependencies，导致 on_complete/expected_outputs/
    knowledge_packs/tool_policy/dialogue_policy 不同但拓扑相同的 spec 撞缓存，
    返回旧图丢失字段。现在纳入完整 model_dump，字段一变缓存即失效。
    """
    if not specs:
        return []
    digest: List[Dict[str, Any]] = []
    for s in specs:
        try:
            digest.append(s.model_dump())
        except Exception:
            digest.append({
                "id": getattr(s, "id", "") or "",
                "agent_id": getattr(s, "agent_id", "") or "",
            })
    return digest


def _make_cache_key(
    template,
    agent_ids,
    mode: str,
    review_mode: bool,
    roundtable_config,
    parallel_config,
    domain_id: str,
    specs=None,
    strategy: str = "",
) -> str:
    """基于编译参数生成缓存 key（含 specs 完整契约 + strategy）"""
    if template is not None:
        # 只对影响编译结果的字段 hash
        wf = template.workflow_config if hasattr(template, "workflow_config") else {}
        relevant = {
            "phases": wf.get("phases", []),
            "agent_ids": list(template.agent_ids if hasattr(template, "agent_ids") else []),
            "mode": mode,
            "review_mode": review_mode,
            "domain_id": domain_id,
            "strategy": strategy,
            "specs": _specs_cache_digest(specs),
        }
    else:
        relevant = {
            "agent_ids": agent_ids or [],
            "mode": mode,
            "review_mode": review_mode,
            "roundtable_config": roundtable_config or {},
            "parallel_config": parallel_config or {},
            "domain_id": domain_id,
            "strategy": strategy,
            "specs": _specs_cache_digest(specs),
        }
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()


def invalidate_compile_cache() -> None:
    """清空编译缓存（模板修改时调用）"""
    _COMPILE_CACHE.clear()


def _store_cache(key: str, result: Tuple) -> None:
    """存入编译缓存（LRU 驱逐）"""
    if len(_COMPILE_CACHE) >= _CACHE_MAX:
        _COMPILE_CACHE.popitem(last=False)
    _COMPILE_CACHE[key] = copy.deepcopy(result)

_QUALITY_RETRY_EXPR = "not values.get('quality_passed', True)"
# 质量未通过时不应静默跳过——default False 确保 quality_gate 未执行时不放行
_QUALITY_PASS_EXPR = "values.get('quality_passed', False)"


def _infer_topology_from_specs(specs: List[PhaseSpec]) -> str:
    """从 PhaseSpec 的 dependencies 自动推断拓扑类型"""
    if len(specs) <= 1:
        return "sequential"
    has_deps = False
    has_no_deps = False
    is_purely_linear = True
    for i, spec in enumerate(specs):
        deps = spec.metadata.get("dependencies", [])
        if i == 0:
            continue
        if not deps:
            has_no_deps = True
            is_purely_linear = False
        else:
            has_deps = True
            if len(deps) != 1 or deps[0] != specs[i-1].id:
                is_purely_linear = False
    if is_purely_linear:
        return "sequential"
    # 全部无依赖：默认 pipeline（真并行须 compile(mode="parallel")）
    if has_no_deps and not has_deps:
        return "sequential"
    return "dag"


class TemplateCompiler:
    """将模板 / agent 列表编译为 CollaborationGraph 可执行的 DAG"""

    @staticmethod
    def parse_phases_from_template(
        template, domain_id: str = ""
    ) -> List[PhaseSpec]:
        """从模板解析 PhaseSpec 列表。domain_id 由调用方显式传入，不自动从模板 tags 推断。"""
        wf = template.workflow_config if hasattr(template, "workflow_config") else {}
        wf = wf or {}
        raw_phases = wf.get("phases") or []
        agent_ids = list(template.agent_ids if hasattr(template, "agent_ids") else [])

        if raw_phases:
            specs = [PhaseSpec.from_legacy_phase(p, i) for i, p in enumerate(raw_phases)]
        elif agent_ids:
            default_oc = str(wf.get("default_on_complete", "continue"))
            specs = PhaseSpec.from_agent_ids(agent_ids, default_on_complete=default_oc)
        else:
            raise ValueError("模板缺少 phases 与 agent_ids")

        # DomainBundle 合并机制已随插件体系废弃（质量规则由 DbQualityGateway 承担）
        return specs

    @staticmethod
    def compile(
        template=None,
        *,
        agent_ids: Optional[List[str]] = None,
        specs: Optional[List[PhaseSpec]] = None,
        mode: str = "auto",
        review_mode: bool = False,
        roundtable_config: Optional[Dict[str, Any]] = None,
        parallel_config: Optional[Dict[str, Any]] = None,
        graph_id: str = "compiled",
        domain_id: str = "",
        strategy: str = "",
    ) -> Tuple[WorkflowGraph, List[PhaseSpec]]:
        # ─── 向后兼容：旧 mode 值映射为 strategy ───
        if mode == "roundtable":
            strategy = "roundtable"
        elif mode == "adaptive":
            strategy = "adaptive"
        # mode="sequential"/"parallel"/"dag"/"auto" 由 dependencies 自动决定

        # ─── 缓存检查（specs 拓扑必须进 key，否则不同 deps 会串图）───
        cache_key = _make_cache_key(
            template, agent_ids, "auto", review_mode,
            roundtable_config, parallel_config, domain_id,
            specs=specs, strategy=strategy,
        )
        if cache_key in _COMPILE_CACHE:
            _COMPILE_CACHE.move_to_end(cache_key)
            return copy.deepcopy(_COMPILE_CACHE[cache_key])

        resolved_specs: List[PhaseSpec] = specs or []

        if specs is not None and len(specs) > 0:
            pass  # 直接使用调用方传入的 specs（保留 dependencies 等元数据）
        elif template is not None:
            wf = template.workflow_config if hasattr(template, "workflow_config") else {}
            wf_strategy = str(wf.get("strategy") or "")
            if wf_strategy and not strategy:
                strategy = wf_strategy
            resolved_specs = TemplateCompiler.parse_phases_from_template(template, domain_id=domain_id)
            graph_id = f"tpl_{template.id if hasattr(template, 'id') else graph_id}"
        elif agent_ids:
            resolved_specs = PhaseSpec.from_agent_ids(agent_ids)
        else:
            raise ValueError("compile 需要 template、agent_ids 或 specs")

        # ── 策略优先：roundtable 覆盖拓扑 ──
        if strategy == "roundtable":
            rt = roundtable_config or {}
            graph = TemplateCompiler._compile_roundtable(
                graph_id, resolved_specs,
                max_rounds=int(rt.get("max_rounds", 3)),
                threshold=float(rt.get("threshold", 0.7)),
                roundtable_config=rt,
            )
            if domain_id:
                TemplateCompiler._inject_domain_id(graph, domain_id)
            _store_cache(cache_key, (graph, resolved_specs))
            return graph, resolved_specs

        # ── 拓扑：显式 parallel 优先，否则由 dependencies 推断 ──
        if mode == "parallel":
            graph = TemplateCompiler._compile_parallel(graph_id, resolved_specs)
        else:
            topo = _infer_topology_from_specs(resolved_specs)
            if topo == "dag":
                graph = TemplateCompiler._compile_dag(graph_id, resolved_specs, review_mode)
            else:
                graph = TemplateCompiler._compile_pipeline(graph_id, resolved_specs, review_mode)

        # ── adaptive 标记追加（不改变拓扑，只标记元数据）──
        if strategy == "adaptive":
            graph.metadata["mode"] = "adaptive"

        if domain_id:
            TemplateCompiler._inject_domain_id(graph, domain_id)
        _store_cache(cache_key, (graph, resolved_specs))
        return graph, resolved_specs

    @staticmethod
    def _compile_pipeline(
        graph_id: str,
        specs: List[PhaseSpec],
        review_mode: bool,
    ) -> WorkflowGraph:
        builder = GraphBuilder(graph_id)
        builder._label = "Collaboration Pipeline"

        # 填充协作契约：从相邻 specs 推导上下游关系和预期产物文件
        for i, spec in enumerate(specs):
            if i > 0:
                spec.upstream_phase_id = specs[i - 1].id
                upstream_outputs = specs[i - 1].expected_outputs
                if upstream_outputs:
                    spec.expected_inputs = list(upstream_outputs)
            if i < len(specs) - 1:
                spec.downstream_phase_id = specs[i + 1].id

        exit_nodes: List[Tuple[str, PhaseSpec, bool]] = []

        for spec in specs:
            exit_id, needs_cond = TemplateCompiler._build_phase_subgraph(builder, spec, review_mode)
            exit_nodes.append((exit_id, spec, needs_cond))

        for i in range(len(specs) - 1):
            from_id, _from_spec, needs_cond = exit_nodes[i]
            to_id = specs[i + 1].id
            if needs_cond:
                builder.conditional_edge(from_id, to_id, _QUALITY_PASS_EXPR, label="质量通过，下一阶段")
            else:
                builder.edge(from_id, to_id)

        if review_mode and exit_nodes:
            final_review = "review_final"
            last_exit = exit_nodes[-1][0]
            if final_review not in [n.id for n in builder._nodes]:
                builder.human(final_review, question="请审核工作流最终产出")
                for n in builder._nodes:
                    if n.id == final_review:
                        n.metadata["type"] = "review"
                builder.edge(last_exit, final_review)

        return builder.build()

    @staticmethod
    def _compile_adaptive(
        graph_id: str,
        specs: List[PhaseSpec],
        review_mode: bool,
    ) -> WorkflowGraph:
        """编译自适应模式图：基于 sequential pipeline，Router 在 Runtime 层隐式触发。

        Router 不作为显式节点插入图中，而是在 NodeExecutor 中处理。
        这保持了图结构的简洁性，adaptive 逻辑在 _adaptive_evaluate() 中运行。
        """
        graph = TemplateCompiler._compile_pipeline(graph_id, specs, review_mode)
        # 标记图元数据为 adaptive 模式（Runtime 读取此字段激活 AdaptiveRouter）
        graph.metadata["mode"] = "adaptive"
        return graph

    @staticmethod
    def _compile_parallel(graph_id: str, specs: List[PhaseSpec]) -> WorkflowGraph:
        builder = GraphBuilder(graph_id)
        builder._label = "Parallel Competition"

        agent_ids = [s.id for s in specs]
        builder.fork("fork_start", targets=agent_ids)

        exit_ids: List[str] = []
        for spec in specs:
            exit_id, _ = TemplateCompiler._build_phase_subgraph(builder, spec, review_mode=False)
            exit_ids.append(exit_id)

        builder.merge(
            "judge_merge",
            sources=exit_ids,
            strategy=MergeStrategy.WINNER_TAKES_ALL,
        )
        return builder.build()

    @staticmethod
    def _compile_roundtable(
        graph_id: str,
        specs: List[PhaseSpec],
        max_rounds: int = 3,
        threshold: float = 0.7,
        roundtable_config: Optional[Dict[str, Any]] = None,
    ) -> WorkflowGraph:
        rt = roundtable_config or {}
        builder = GraphBuilder(graph_id)
        builder._label = "Round Table Discussion"

        agent_ids = [s.id for s in specs]
        builder.loop("discussion_loop", body_node_ids=agent_ids, max_iterations=max_rounds)

        prev = "discussion_loop"
        for spec in specs:
            nid = spec.id
            builder.agent(nid, agent_id=spec.agent_id, label=spec.label or spec.agent_id)
            TemplateCompiler._stamp_phase_metadata(builder, nid, spec)
            builder.edge(prev, nid)
            qg_id = f"qg_{nid}"
            builder.quality_gate(qg_id, label=f"质量检查 {spec.label}", check_targets=[nid])
            TemplateCompiler._stamp_gate_metadata(builder, qg_id, spec)
            builder.edge(nid, qg_id)
            prev = qg_id
            spec.collaboration = "deliberative"

        decision_id = "decision_roundtable"
        default_options = [
            {"id": "continue", "label": "继续按当前结论推进", "agent_view": "continue"},
            {"id": "revise", "label": "重新讨论", "agent_view": "revise"},
        ]
        builder.human(
            decision_id,
            question="讨论是否需要您的决策后继续？",
        )
        for n in builder._nodes:
            if n.id == decision_id:
                n.metadata["type"] = "decision"
                n.metadata["phase_id"] = "roundtable_decision"
                n.metadata["options"] = rt.get("decision_options") or default_options
        builder.edge(prev, decision_id)
        builder.metadata(roundtable_threshold=threshold, loop_body_node_ids=agent_ids)
        # 标记所有 spec 为 deliberative（运行时 _build_agent_input 读取此值决定是否注入同伴产出）
        for spec in specs:
            spec.collaboration = "deliberative"
            spec.metadata["roundtable_peers"] = [s.id for s in specs if s.id != spec.id]
        return builder.build()

    @staticmethod
    def _compile_dag(
        graph_id: str,
        specs: List[PhaseSpec],
        review_mode: bool = False,
    ) -> WorkflowGraph:
        """根据 PhaseSpec 的 upstream/downstream 关系编译真正的 DAG 图

        与 _compile_pipeline 的区别：
        - pipeline 是线性链 A→B→C
        - dag 根据 dependencies 生成 fork/merge 结构

        策略：
        1. 从 specs 的 metadata["dependencies"] 提取依赖关系
        2. 无依赖的节点从 START 直接 fork 出去
        3. 汇聚点使用 MERGE(strategy=ALL) 等待所有前驱完成
        4. 每个 Agent 节点后跟 quality_gate（如果 review_mode）
        """
        builder = GraphBuilder(graph_id)
        builder._label = "DAG Workflow"

        # 1. 构建依赖图
        spec_map = {s.id: s for s in specs}
        deps_map: Dict[str, List[str]] = {}
        for spec in specs:
            deps = spec.metadata.get("dependencies", [])
            # dependencies 可能是 phase_id 或 title，统一处理
            resolved_deps = []
            for dep in deps:
                if dep in spec_map:
                    resolved_deps.append(dep)
                else:
                    # 按 title 查找
                    match = next((s.id for s in specs if s.label == dep or s.id == dep), None)
                    if match:
                        resolved_deps.append(match)
            deps_map[spec.id] = resolved_deps

        # 2. 找出入度为 0 的节点（起始节点）
        root_ids = [s.id for s in specs if not deps_map.get(s.id)]

        # 3. 找出出度为 0 的节点（终止节点）
        depended_by: Dict[str, List[str]] = {s.id: [] for s in specs}
        for sid, deps in deps_map.items():
            for d in deps:
                depended_by[d].append(sid)
        leaf_ids = [sid for sid, successors in depended_by.items() if not successors]

        # 4. 构建节点（每个 phase 使用完整安全子图：agent→checkpoint→quality_gate）
        phase_exits: Dict[str, Tuple[str, bool]] = {}  # spec.id → (exit_node_id, needs_cond)
        for spec in specs:
            exit_id, needs_cond = TemplateCompiler._build_phase_subgraph(builder, spec, review_mode)
            phase_exits[spec.id] = (exit_id, needs_cond)

        # 5. 构建边
        # 如果有多个 root，从 START fork 出去
        if len(root_ids) > 1:
            builder.fork("dag_fork_start", targets=root_ids)

        # 为每个节点连接其后继（从安全子图的 exit 节点出发）
        for spec in specs:
            source_id = phase_exits[spec.id][0]
            successors = depended_by.get(spec.id, [])
            for succ in successors:
                builder.edge(source_id, succ)

        # 如果有多个 leaf 需要汇聚（从安全子图的 exit 节点汇聚）
        if len(leaf_ids) > 1:
            merge_sources = [phase_exits[lid][0] for lid in leaf_ids]
            builder.merge("dag_merge_end", sources=merge_sources, strategy=MergeStrategy.ALL)

        return builder.build()

    @staticmethod
    def _build_phase_subgraph(
        builder: GraphBuilder,
        spec: PhaseSpec,
        review_mode: bool = False,
    ) -> Tuple[str, bool]:
        """为单个 phase 构建 agent→checkpoint→quality_gate 安全子图

        返回 (exit_node_id, needs_conditional_edge)。
        _compile_pipeline / _compile_dag / _compile_parallel 共用此方法。
        """
        node_id = spec.id
        builder.agent(node_id, agent_id=spec.agent_id, label=spec.label or spec.agent_id)
        TemplateCompiler._stamp_phase_metadata(builder, node_id, spec)

        # Checkpoint：Agent 后自动保存状态快照
        ckpt_id = f"ckpt_{node_id}"
        builder.checkpoint(ckpt_id, label=f"Checkpoint {spec.label}")
        builder.edge(node_id, ckpt_id)

        exit_fail = spec.exit.on_fail
        qprof = spec.quality_profile
        is_retry = exit_fail in ("retry", "revise_loop") or qprof.on_fail in ("retry", "revise_loop")

        if exit_fail == "human_review":
            review_id = f"review_{node_id}"
            builder.human(review_id, question=f"请审核 {spec.label} 的产出")
            for n in builder._nodes:
                if n.id == review_id:
                    n.metadata["type"] = "review"
                    n.metadata["phase_id"] = spec.id
            builder.edge(ckpt_id, review_id)
            return review_id, False
        elif exit_fail == "decision":
            decision_id = f"decision_{node_id}"
            builder.human(
                decision_id,
                question=spec.metadata.get("decision_question") or f"请选择 {spec.label} 的后续方向",
            )
            for n in builder._nodes:
                if n.id == decision_id:
                    n.metadata["type"] = "decision"
                    n.metadata["phase_id"] = spec.id
                    n.metadata["options"] = spec.metadata.get("decision_options") or []
            builder.edge(ckpt_id, decision_id)
            return decision_id, False
        elif is_retry:
            qg_id = f"qg_{node_id}"
            builder.quality_gate(qg_id, label=f"质量门控 {spec.label}", check_targets=[node_id])
            TemplateCompiler._stamp_gate_metadata(builder, qg_id, spec)
            builder.edge(ckpt_id, qg_id)
            builder.loop_back(
                qg_id, node_id,
                condition=GraphCondition(
                    condition_type=ConditionType.EXPRESSION,
                    expression=_QUALITY_RETRY_EXPR,
                    label="质量未通过，重试",
                ),
            )
            return qg_id, True
        else:
            qg_id = f"qg_{node_id}_check"
            builder.quality_gate(qg_id, label=f"质量检查 {spec.label}", check_targets=[node_id])
            TemplateCompiler._stamp_gate_metadata(builder, qg_id, spec)
            builder.edge(ckpt_id, qg_id)
            return qg_id, False

    @staticmethod
    def _stamp_phase_metadata(builder: GraphBuilder, node_id: str, spec: PhaseSpec) -> None:
        for n in builder._nodes:
            if n.id == node_id:
                n.metadata["phase_id"] = spec.id
                n.metadata["phase_spec"] = spec.model_dump()
                n.metadata["quality_profile"] = spec.quality_profile.model_dump()
                n.metadata["knowledge_profile"] = spec.knowledge_profile.model_dump()

    @staticmethod
    def _inject_domain_id(graph: WorkflowGraph, domain_id: str) -> None:
        """为所有 QUALITY_GATE 节点注入 domain_id 元数据"""
        from core.graph.types import NodeType
        for node in graph.nodes:
            if node.type == NodeType.QUALITY_GATE:
                node.metadata["domain_id"] = domain_id

    @classmethod
    def compile_from_plan(cls, plan_data: dict, review_mode: bool = False) -> Tuple[WorkflowGraph, List[PhaseSpec]]:
        """从 plans 表记录编译 WorkflowGraph（替代 compile(template=...) 路径）

        [结合点] 被 execute_saved_plan() 调用
        """
        import json
        phases_raw = json.loads(plan_data["phases"]) if isinstance(plan_data["phases"], str) else plan_data["phases"]
        mode = plan_data.get("mode", "sequential")
        domain_id = plan_data.get("domain_id", "")
        strategy = plan_data.get("strategy", "")
        graph_id = f"plan_{plan_data['id']}"

        # 构建 PhaseSpec 列表
        specs = []
        for i, p in enumerate(phases_raw):
            if not isinstance(p, dict):
                _logger.warning("Phase %d 非法格式（非 dict），跳过: %s", i, type(p).__name__)
                continue
            raw_agent_id = p.get("agent_id", "")
            if not raw_agent_id:
                # 规划阶段未匹配到 Agent 的 phase，兜底为通用助手
                raw_agent_id = "builtin_general"
                _logger.warning(
                    "Phase %d (%s) 无 agent_id，兜底为 builtin_general",
                    i, p.get("label", ""),
                )
            # Phase 0：无损传递完整 phase dict 到 PhaseSpec（不再只挑 5 个字段）
            # 完整字段由 PhaseSpec.from_legacy_phase 读取（expected_inputs/outputs/
            # tool_policy/knowledge_top_k/quality_profile/max_retries 等）
            raw = {
                **p,
                "agent_id": raw_agent_id,
                "phase_id": p.get("phase_id") or p.get("id") or f"phase_{i}",
                "label": p.get("label") or p.get("title") or "",
            }
            spec = PhaseSpec.from_legacy_phase(raw, i)
            deps = p.get("dependencies", [])
            if deps:
                spec.metadata["dependencies"] = deps
            specs.append(spec)

        return cls.compile(
            specs=specs,
            mode=mode,
            review_mode=review_mode,
            domain_id=domain_id,
            graph_id=graph_id,
            strategy=strategy,
        )

    @staticmethod
    def _stamp_gate_metadata(builder: GraphBuilder, gate_id: str, spec: PhaseSpec) -> None:
        for n in builder._nodes:
            if n.id == gate_id:
                n.metadata["phase_id"] = spec.id
                qp = spec.quality_profile.model_dump()
                qp["phase_id"] = spec.id
                n.metadata["quality_profile"] = qp
