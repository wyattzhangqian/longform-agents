"""Workflow Graph Engine — Graph Builder (声明式 DSL)

提供链式 API 构建 WorkflowGraph，支持：
- 链式节点/边定义
- 声明式条件路由
- 子图嵌套
- Hook 注册
- 旧模式 Adapter（sequential / parallel / roundtable / review）

使用方式:
    graph = (
        GraphBuilder("my_graph")
        .agent("researcher", agent_id="researcher")
        .agent("writer", agent_id="writer")
        .router("quality_gate", routes={"pass": "output", "fail": "reviser"})
        .edge("researcher", "writer")
        .edge("writer", "quality_gate")
        .route_from("quality_gate")
            .when("state.score >= 0.8").to("output").label("通过")
            .default().to("reviser").label("修改")
        .build()
    )
"""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Union
from copy import deepcopy

from .types import (
    WorkflowGraph,
    GraphNode,
    GraphEdge,
    GraphCondition,
    StateSchema,
    StateFieldDef,
    NodeType,
    EdgeType,
    ConditionType,
    NodeConfig,
    MergeStrategy,
    START_NODE_ID,
    END_NODE_ID,
)


class RouteBuilder:
    """路由构建辅助类 — 用于 route_from() 之后的链式调用"""

    def __init__(
        self,
        builder: 'GraphBuilder',
        source_node_id: str,
        default_condition_type: ConditionType = ConditionType.EXPRESSION,
    ):
        self._builder = builder
        self._source = source_node_id
        self._cond_type = default_condition_type
        self._default_target: Optional[str] = None

    def when(
        self,
        condition: str,
        condition_type: ConditionType = ConditionType.EXPRESSION,
        label: str = "",
        priority: int = 0,
    ) -> 'RouteBuilderTarget':
        """声明一个条件分支
        
        Args:
            condition: 条件表达式字符串
            condition_type: 条件类型
            label: 可读标签
            priority: 优先级
        """
        return RouteBuilderTarget(
            builder=self._builder,
            source=self._source,
            condition_str=condition,
            condition_type=condition_type,
            label=label,
            priority=priority,
        )

    def default(self) -> 'RouteBuilderDefault':
        """声明默认分支"""
        return RouteBuilderDefault(builder=self._builder, source=self._source)

    def to(self, target_node_id: str) -> 'GraphBuilder':
        """无条件转移到目标（快捷方式）"""
        self._builder.edge(self._source, target_node_id)
        return self._builder


class RouteBuilderTarget:
    """条件分支的目标指定器"""

    def __init__(
        self,
        builder: 'GraphBuilder',
        source: str,
        condition_str: str,
        condition_type: ConditionType,
        label: str,
        priority: int,
    ):
        self._builder = builder
        self._source = source
        self._condition_str = condition_str
        self._condition_type = condition_type
        self._label = label
        self._priority = priority

    def to(self, target_node_id: str, label: Optional[str] = None) -> 'RouteBuilder':
        """指定条件分支的目标节点，返回 RouteBuilder 以继续定义更多路由"""
        cond = GraphCondition(
            condition_type=self._condition_type,
            expression=self._condition_str,
            label=label or self._label or f"{self._source} → {target_node_id}",
            priority=self._priority,
        )
        edge = GraphEdge(
            from_node=self._source,
            to_node=target_node_id,
            edge_type=EdgeType.CONDITIONAL,
            condition=cond,
        )
        self._builder.add_edge(edge)
        # 返回 RouteBuilder 以支持继续链式调用 .when() / .default()
        return RouteBuilder(self._builder, self._source)


class RouteBuilderDefault:
    """默认分支的指定器"""

    def __init__(self, builder: 'GraphBuilder', source: str):
        self._builder = builder
        self._source = source

    def to(self, target_node_id: str, label: str = "") -> 'GraphBuilder':
        edge = GraphEdge(
            from_node=self._source,
            to_node=target_node_id,
            edge_type=EdgeType.DEFAULT,
            label=label or f"{self._source} → {target_node_id} (默认)",
        )
        self._builder.add_edge(edge)
        return self._builder


# ============================================================
# GraphBuilder — 主构建器
# ============================================================

class GraphBuilder:
    """Workflow Graph 声明式构建器
    
    支持链式 API 构建完整的工作流图定义。
    """

    def __init__(self, graph_id: str):
        self._graph_id = graph_id
        self._nodes: List[GraphNode] = []
        self._edges: List[GraphEdge] = []
        self._schema: Optional[StateSchema] = None
        self._config: Dict[str, Any] = {}
        self._metadata: Dict[str, Any] = {}
        self._tags: List[str] = []
        self._label = ""
        self._description = ""
        self._last_node_id: Optional[str] = None  # 用于自动连接

    # ==================== 图级配置 ====================

    def label(self, label: str) -> 'GraphBuilder':
        self._label = label
        return self

    def description(self, desc: str) -> 'GraphBuilder':
        self._description = desc
        return self

    def metadata(self, **kwargs) -> 'GraphBuilder':
        self._metadata.update(kwargs)
        return self

    def tags(self, *tags: str) -> 'GraphBuilder':
        self._tags.extend(tags)
        return self

    def config(self, **kwargs) -> 'GraphBuilder':
        self._config.update(kwargs)
        return self

    # ==================== 状态 Schema ====================

    def state(
        self,
        schema_def: Optional[Dict[str, type]] = None,
        description: str = "",
    ) -> 'GraphBuilder':
        """定义状态 Schema
        
        Args:
            schema_def: 字段名 → 类型 的映射，如 {"task": str, "score": float}
            description: Schema 描述
        """
        if schema_def:
            self._schema = StateSchema(description=description)
            for name, type_hint in schema_def.items():
                type_str = getattr(type_hint, '__name__', str(type_hint))
                self._schema.add_field(name, type_hint=type_str)
        else:
            self._schema = StateSchema(description=description)
        return self

    def add_state_field(
        self,
        name: str,
        type_hint: Any = "Any",
        default: Any = None,
        required: bool = True,
        description: str = "",
    ) -> 'GraphBuilder':
        """添加单个状态字段"""
        if not self._schema:
            self._schema = StateSchema()
        type_str = getattr(type_hint, '__name__', str(type_hint)) if type_hint != "Any" else "Any"
        self._schema.add_field(name, type_str, default, required, description)
        return self

    # ==================== 节点定义 ====================

    def agent(
        self,
        node_id: str,
        agent_id: str,
        label: str = "",
        description: str = "",
        **node_config,
    ) -> 'GraphBuilder':
        """添加 Agent 节点"""
        node = GraphNode(
            id=node_id,
            type=NodeType.AGENT,
            label=label or node_id,
            description=description,
            agent_id=agent_id,
            config=NodeConfig(**node_config) if node_config else NodeConfig(),
        )
        self._add_node(node)
        return self

    def function(
        self,
        node_id: str,
        func_ref: str,
        label: str = "",
        description: str = "",
        **node_config,
    ) -> 'GraphBuilder':
        """添加 Function 节点"""
        node = GraphNode(
            id=node_id,
            type=NodeType.FUNCTION,
            label=label or node_id,
            description=description,
            func_ref=func_ref,
            config=NodeConfig(**node_config) if node_config else NodeConfig(),
        )
        self._add_node(node)
        return self

    def subgraph(
        self,
        node_id: str,
        subgraph: 'WorkflowGraph',
        input_mapping: Optional[Dict[str, str]] = None,
        output_mapping: Optional[Dict[str, str]] = None,
        label: str = "",
    ) -> 'GraphBuilder':
        """添加 Subgraph 节点"""
        node = GraphNode(
            id=node_id,
            type=NodeType.SUBGRAPH,
            label=label or node_id or subgraph.label or subgraph.graph_id,
            description=f"Subgraph: {subgraph.graph_id}",
            subgraph_id=subgraph.graph_id,
            input_mapping=input_mapping or {},
            output_mapping=output_mapping or {},
        )
        self._add_node(node)
        # 存储子图引用（用于序列化）
        node.metadata['_subgraph_def'] = subgraph.to_dict()
        return self

    def router(
        self,
        node_id: str,
        routes: Dict[str, str],
        label: str = "",
        router_type: ConditionType = ConditionType.HANDLER_FN,
        description: str = "",
    ) -> 'GraphBuilder':
        """添加 Router 节点
        
        Args:
            node_id: 节点 ID
            routes: 路由映射 {route_key: target_node_id}
            label: 显示标签
            router_type: 路由决策类型
        """
        node = GraphNode(
            id=node_id,
            type=NodeType.ROUTER,
            label=label or node_id,
            description=description or f"Router: {len(routes)} routes",
            routes=routes,
            router_type=router_type,
        )
        self._add_node(node)
        return self

    def fork(
        self,
        node_id: str,
        targets: List[str],
        label: str = "",
        merge_strategy: MergeStrategy = MergeStrategy.ALL,
    ) -> 'GraphBuilder':
        """添加 Fork 分叉节点"""
        node = GraphNode(
            id=node_id,
            type=NodeType.FORK,
            label=label or node_id,
            fork_targets=targets,
            merge_strategy=merge_strategy,
        )
        self._add_node(node)

        # 自动创建 Fork → targets 的边
        for target in targets:
            self._edges.append(GraphEdge(
                from_node=node_id,
                to_node=target,
                edge_type=EdgeType.ALWAYS,
                label=f"fork → {target}",
            ))
        return self

    def merge(
        self,
        node_id: str,
        sources: List[str],
        strategy: MergeStrategy = MergeStrategy.ALL,
        label: str = "",
    ) -> 'GraphBuilder':
        """添加 Merge 汇聚节点"""
        node = GraphNode(
            id=node_id,
            type=NodeType.MERGE,
            label=label or node_id,
            merge_sources=sources,
            merge_strategy=strategy,
        )
        self._add_node(node)

        # 自动创建 sources → Merge 的边
        for src in sources:
            self._edges.append(GraphEdge(
                from_node=src,
                to_node=node_id,
                edge_type=EdgeType.PARALLEL_JOIN,
                label=f"{src} → merge",
            ))
        return self

    def loop(
        self,
        node_id: str,
        body_node_ids: List[str],
        max_iterations: int = 10,
        terminate_when: Optional[GraphCondition] = None,
        label: str = "",
    ) -> 'GraphBuilder':
        """添加 Loop 循环节点"""
        node = GraphNode(
            id=node_id,
            type=NodeType.LOOP,
            label=label or node_id,
            loop_body=body_node_ids,
            max_iterations=max_iterations,
            terminate_condition=terminate_when,
        )
        self._add_node(node)
        return self

    def human(
        self,
        node_id: str,
        question: str,
        options: Optional[List[Dict[str, str]]] = None,
        timeout_seconds: int = 300,
        label: str = "",
    ) -> 'GraphBuilder':
        """添加 Human-in-the-loop 人机协作节点"""
        node = GraphNode(
            id=node_id,
            type=NodeType.HUMAN_IN_THE_LOOP,
            label=label or node_id or "等待用户输入",
            description=question,
            config=NodeConfig(timeout_seconds=timeout_seconds),
            metadata={
                "question": question,
                "options": options or [],
            },
        )
        self._add_node(node)
        return self

    def checkpoint(
        self,
        node_id: str = "checkpoint",
        label: str = "检查点",
        quality_target: Optional[str] = None,
    ) -> 'GraphBuilder':
        """添加显式 Checkpoint 节点
        
        Args:
            node_id: 节点 ID
            label: 显示标签
            quality_target: 可选，在此检查点前对指定 Agent 节点执行质量检查
        """
        metadata: Dict[str, Any] = {}
        if quality_target:
            metadata["quality_check_target"] = quality_target
        node = GraphNode(
            id=node_id,
            type=NodeType.CHECKPOINT,
            label=label,
            metadata=metadata,
        )
        self._add_node(node)
        return self

    def quality_gate(
        self,
        node_id: str = "quality_gate",
        label: str = "质量门禁",
        check_targets: Optional[List[str]] = None,
    ) -> 'GraphBuilder':
        """添加质量门禁节点：对指定（或所有已完成）Agent 产出执行 QualityGateway 检查
        
        检查结果写入 state.values["quality_violations"] 和 
        state.values["quality_passed"]，后续 Router 节点可通过 
        "state.quality_passed" 表达式进行条件路由。

        用法:
            builder
                .agent("writer", agent_id="writer")
                .quality_gate("check", check_targets=["writer"])
                .router("gate", routes={"pass": "done", "fail": "retry"})
                .route_from("gate")
                    .when("state.quality_passed").to("done").label("通过")
                    .default().to("retry").label("重试")
        
        Args:
            node_id: 节点 ID
            label: 显示标签
            check_targets: 要检查的节点 ID 列表（默认检查所有已完成 Agent 节点）
        """
        metadata: Dict[str, Any] = {}
        if check_targets:
            metadata["quality_check_targets"] = check_targets
        node = GraphNode(
            id=node_id,
            type=NodeType.QUALITY_GATE,
            label=label,
            metadata=metadata,
        )
        self._add_node(node)
        return self

    def with_quality_check(
        self,
        agent_node_id: str,
        gate_node_id: str = "quality_gate",
        retry_node_id: Optional[str] = None,
    ) -> 'GraphBuilder':
        """便捷方法：在 Agent 执行后自动添加 QualityGateway 检查 + 条件路由
        
        生成结构：
            agent_node → quality_gate → (passed → 继续 | failed → 重试/结束)
        
        Args:
            agent_node_id: 要检查的 Agent 节点 ID
            gate_node_id: 质量门禁节点 ID（默认 "quality_gate"）
            retry_node_id: 失败时跳转的节点 ID（默认直接结束）
        """
        gate_id = gate_node_id
        # 避免重复添加同名 gate
        existing = [n.id for n in self._nodes]
        if gate_id not in existing:
            self.quality_gate(node_id=gate_id, check_targets=[agent_node_id])

        if retry_node_id:
            # 通过 → 继续到 END，失败 → 重试
            router_id = f"_{gate_id}_router"
            self.router(router_id, routes={"passed": "__end__", "retry": retry_node_id})
            self.edge(agent_node_id, gate_id)
            self.edge(gate_id, router_id)
            self.route_from(router_id) \
                .when("state.quality_passed", label="质量通过").to("__end__") \
                .default().to(retry_node_id).label("质量未通过")
        else:
            # 通过 → 继续到 END，失败 → 也继续（只记录不阻断）
            self.edge(agent_node_id, gate_id)
            self.edge(gate_id, "__end__")

        return self

    # ==================== 边定义 ====================

    def edge(
        self,
        from_node: str,
        to_node: str,
        edge_type: EdgeType = EdgeType.ALWAYS,
        label: str = "",
        condition: Optional[GraphCondition] = None,
    ) -> 'GraphBuilder':
        """添加一条边"""
        edge = GraphEdge(
            from_node=from_node,
            to_node=to_node,
            edge_type=edge_type,
            label=label or f"{from_node} → {to_node}",
            condition=condition,
        )
        self._edges.append(edge)
        return self

    def conditional_edge(
        self,
        from_node: str,
        to_node: str,
        condition: Union[str, GraphCondition],
        label: str = "",
        priority: int = 0,
    ) -> 'GraphBuilder':
        """添加条件边（便捷方法）"""
        if isinstance(condition, str):
            cond = GraphCondition(
                condition_type=ConditionType.EXPRESSION,
                expression=condition,
                label=label,
                priority=priority,
            )
        elif isinstance(condition, GraphCondition):
            cond = condition
        else:
            raise ValueError(f"condition 必须是 str 或 GraphCondition, got {type(condition)}")

        return self.edge(from_node, to_node, EdgeType.CONDITIONAL, label or cond.label, cond)

    def loop_back(
        self,
        from_node: str,
        to_node: str,
        condition: Optional[GraphCondition] = None,
        label: str = "",
    ) -> 'GraphBuilder':
        """添加回环边"""
        edge = GraphEdge(
            from_node=from_node,
            to_node=to_node,
            edge_type=EdgeType.LOOP_BACK,
            label=label or f"{from_node} ↺ {to_node}",
            condition=condition,
        )
        self._edges.append(edge)
        return self

    def error_edge(
        self,
        from_node: str,
        to_node: str,
        label: str = "",
    ) -> 'GraphBuilder':
        """添加异常处理边"""
        edge = GraphEdge(
            from_node=from_node,
            to_node=to_node,
            edge_type=EdgeType.ERROR_EDGE,
            label=label or f"{from_node} !→ {to_node}",
        )
        self._edges.append(edge)
        return self

    # ==================== 路由 DSL ====================

    def route_from(self, source_node_id: str) -> RouteBuilder:
        """开始路由定义（返回 RouteBuilder 用于链式调用）"""
        return RouteBuilder(self, source_node_id)

    def auto_route(
        self,
        router_node_id: str,
        route_map: Dict[str, str],
        default_target: Optional[str] = None,
    ) -> 'GraphBuilder':
        """快速路由映射（一次性完成所有路由边）
        
        Args:
            router_node_id: Router 节点 ID
            route_map: {route_expression_or_key: target_node_id}
            default_target: 默认目标
        """
        for key, target in route_map.items():
            # 判断 key 是表达式还是简单的路由键
            if any(op in key for op in ['>', '<', '=', ' ', 'and', 'or', 'not']):
                cond = GraphCondition(
                    condition_type=ConditionType.EXPRESSION,
                    expression=key,
                    label=f"{key} → {target}",
                )
                edge = GraphEdge(
                    from_node=router_node_id,
                    to_node=target,
                    edge_type=EdgeType.CONDITIONAL,
                    condition=cond,
                )
            else:
                cond = GraphCondition(
                    condition_type=ConditionType.EXPRESSION,
                    expression=f'state.last_router_output == "{key}"',
                    label=f"[{key}] → {target}",
                )
                edge = GraphEdge(
                    from_node=router_node_id,
                    to_node=target,
                    edge_type=EdgeType.CONDITIONAL,
                    condition=cond,
                )
            self._edges.append(edge)

        if default_target:
            self._edges.append(GraphEdge(
                from_node=router_node_id,
                to_node=default_target,
                edge_type=EdgeType.DEFAULT,
                label="默认路由",
            ))
        return self

    # ==================== Hooks ====================

    def on_node_start(self, handler_ref: str, node_filter: Optional[str] = None) -> 'GraphBuilder':
        """注册节点启动钩子"""
        for node in self._nodes:
            if node_filter is None or node.id == node_filter:
                node.config.pre_hooks.append(handler_ref)
        return self

    def on_node_complete(self, handler_ref: str, node_filter: Optional[str] = None) -> 'GraphBuilder':
        """注册节点完成钩子"""
        for node in self._nodes:
            if node_filter is None or node.id == node_filter:
                node.config.post_hooks.append(handler_ref)
        return self

    def on_error(self, handler_ref: str) -> 'GraphBuilder':
        """注册全局错误处理器"""
        self._config['error_handler'] = handler_ref
        return self

    # ==================== 内部方法 ====================

    def _add_node(self, node: GraphNode) -> None:
        """内部：添加节点并更新 last_node_id"""
        # 检查重复 ID
        existing_ids = [n.id for n in self._nodes]
        if node.id in existing_ids:
            raise ValueError(f"重复的节点 ID: '{node.id}'")
        
        self._nodes.append(node)
        self._last_node_id = node.id

    def add_edge(self, edge: GraphEdge) -> None:
        """内部：添加边（供 RouteBuilder 使用）"""
        self._edges.append(edge)

    # ==================== 构建 ====================

    def build(self) -> WorkflowGraph:
        """构建最终的 WorkflowGraph
        
        执行验证并返回不可变的图定义。
        """
        # 自动连接隐式的 START 边
        start_nodes = self._find_start_nodes()
        for node_id in start_nodes:
            has_start_edge = any(e.from_node == START_NODE_ID and e.to_node == node_id for e in self._edges)
            if not has_start_edge and not any(e.to_node == node_id and e.from_node != START_NODE_ID for e in self._edges):
                # 只有真正无入边的才加 START
                in_edges = [e for e in self._edges if e.to_node == node_id]
                if not in_edges:
                    self._edges.insert(0, GraphEdge(
                        from_node=START_NODE_ID,
                        to_node=node_id,
                        edge_type=EdgeType.ALWAYS,
                        label=f"START → {node_id}",
                    ))

        # 自动连接到 END
        end_nodes = self._find_end_nodes()
        for node_id in end_nodes:
            has_end_edge = any(e.from_node == node_id and e.to_node == END_NODE_ID for e in self._edges)
            out_edges = [e for e in self._edges if e.from_node == node_id]
            if not out_edges:
                self._edges.append(GraphEdge(
                    from_node=node_id,
                    to_node=END_NODE_ID,
                    edge_type=EdgeType.ALWAYS,
                    label=f"{node_id} → END",
                ))

        graph = WorkflowGraph(
            graph_id=self._graph_id,
            label=self._label or self._graph_id,
            description=self._description,
            nodes=list(self._nodes),
            edges=list(self._edges),
            state_schema=self._schema,
            metadata=dict(self._metadata),
            tags=list(self._tags),
            config=dict(self._config) if self._config else {},
        )

        # 基础结构验证（深度验证在 validators.py 中进行）
        self._validate_basic(graph)

        return graph

    def _find_start_nodes(self) -> List[str]:
        """找出入口节点（被任何其他节点引用为目标的节点不算入口）"""
        targets = {e.to_node for e in self._edges}
        return [n.id for n in self._nodes if n.id not in targets]

    def _find_end_nodes(self) -> List[str]:
        """找出出口节点（没有任何出边的节点）"""
        sources = {e.from_node for e in self._edges}
        return [n.id for n in self._nodes if n.id not in sources]

    @staticmethod
    def _validate_basic(graph: WorkflowGraph) -> None:
        """基础结构验证"""
        node_ids = set(graph.node_ids)

        # 检查边的端点是否都指向存在的节点
        for edge in graph.edges:
            if edge.from_node not in node_ids and edge.from_node not in (START_NODE_ID, END_NODE_ID):
                raise ValueError(f"边引用了不存在的源节点: '{edge.from_node}'")
            if edge.to_node not in node_ids and edge.to_node not in (START_NODE_ID, END_NODE_ID):
                raise ValueError(f"边引用了不存在的目标节点: '{edge.to_node}'")

        # 检查是否有孤立节点
        referenced = set()
        for edge in graph.edges:
            referenced.add(edge.from_node)
            referenced.add(edge.to_node)
        isolated = node_ids - referenced
        if isolated:
            import warnings
            warnings.warn(f"存在孤立节点（无任何边连接）: {isolated}")

    # ==================== 旧版适配器工厂方法 ====================

    @classmethod
    def sequential(cls, graph_id: str, agent_ids: List[str]) -> 'GraphBuilder':
        """从旧的 sequential 模式创建 Graph
        
        Args:
            graph_id: 图 ID
            agent_ids: 按顺序执行的 Agent ID 列表
        """
        builder = cls(graph_id).label("Sequential Pipeline")
        prev = None
        for aid in agent_ids:
            builder.agent(aid, agent_id=aid)
            if prev:
                builder.edge(prev, aid)
            prev = aid
        return builder

    @classmethod
    def parallel_competition(
        cls,
        graph_id: str,
        agent_ids: List[str],
        judge_fn_ref: str = "",
    ) -> 'GraphBuilder':
        """从旧的 parallel 模式创建 Graph
        
        Args:
            graph_id: 图 ID
            agent_ids: 并行竞争的 Agent ID 列表
            judge_fn_ref: 评判函数引用
        """
        builder = cls(graph_id).label("Parallel Competition")

        # Fork 节点
        builder.fork("fork_start", targets=agent_ids)

        # 每个 agent 作为独立节点
        for aid in agent_ids:
            builder.agent(aid, agent_id=aid)

        # Merge 节点（竞争模式）
        builder.merge(
            "judge_merge",
            sources=agent_ids,
            strategy=MergeStrategy.WINNER_TAKES_ALL,
        )

        return builder

    @classmethod
    def roundtable(
        cls,
        graph_id: str,
        agent_ids: List[str],
        max_rounds: int = 3,
        threshold: float = 0.7,
    ) -> 'GraphBuilder':
        """从旧的 roundtable 模式创建 Graph"""
        builder = cls(graph_id).label("Round Table Discussion")

        # Loop 包裹所有讨论者
        builder.loop(
            "discussion_loop",
            body_node_ids=agent_ids,
            max_iterations=max_rounds,
        )

        for aid in agent_ids:
            builder.agent(aid, agent_id=aid)

        # 串行连接（每轮内按顺序发言）
        prev = "discussion_loop"
        for aid in agent_ids:
            builder.edge(prev, aid)
            prev = aid

        return builder

    @classmethod
    def review_pipeline(
        cls,
        graph_id: str,
        agent_ids: List[str],
        review_points: Optional[List[int]] = None,
    ) -> 'GraphBuilder':
        """从旧的 review 模式创建 Graph
        
        在指定的阶段后插入 human checkpoint。
        """
        builder = cls(graph_id).label("Review Pipeline")
        review_set = set(review_points or [])

        prev = None
        for i, aid in enumerate(agent_ids):
            builder.agent(aid, agent_id=aid)
            if prev:
                builder.edge(prev, aid)

            # 在指定位置插入 checkpoint
            if i in review_set:
                cp_id = f"review_after_{aid}"
                builder.checkpoint(cp_id, label=f"审核点 #{i+1}")
                builder.edge(aid, cp_id)
                prev = cp_id
            else:
                prev = aid

        return builder


# ============================================================
# 便捷导出函数
# ============================================================

def create_graph(graph_id: str) -> GraphBuilder:
    """创建新 GraphBuilder 的便捷函数"""
    return GraphBuilder(graph_id)


def sequential_graph(agent_ids: List[str], graph_id: str = "sequential") -> WorkflowGraph:
    """一键创建顺序流水线图"""
    return GraphBuilder.sequential(graph_id, agent_ids).build()


def parallel_graph(agent_ids: List[str], graph_id: str = "parallel") -> WorkflowGraph:
    """一键创建并行竞争图"""
    return GraphBuilder.parallel_competition(graph_id, agent_ids).build()


def roundtable_graph(
    agent_ids: List[str],
    max_rounds: int = 3,
    threshold: float = 0.7,
    graph_id: str = "roundtable",
) -> WorkflowGraph:
    """一键创建圆桌讨论图"""
    return GraphBuilder.roundtable(graph_id, agent_ids, max_rounds, threshold).build()
