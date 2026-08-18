"""Workflow Graph Engine — 图验证器

在 build() 阶段对图定义进行深度结构验证，确保：
1. 连通性：所有节点可达，无孤立节点
2. 无死循环（可选允许）：检测无法终止的环
3. 完整性：Router 节点的 routes 都有对应边
4. 类型安全：节点配置与类型匹配
5. 安全性：max_iterations 限制等安全约束

使用方式:
    from core.graph.validators import GraphValidator
    
    errors = GraphValidator.validate(graph)
    if errors:
        for err in errors:
            print(f"ERROR: {err}")
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field


@dataclass
class ValidationError:
    """单个验证错误"""
    level: str                              # "error" | "warning" | "info"
    code: str                               # 错误码（如 "ISOLATED_NODE"）
    message: str                            # 可读错误消息
    node_id: Optional[str] = None           # 关联的节点 ID
    edge_from: Optional[str] = None         # 关联的边的起点
    edge_to: Optional[str] = None           # 关联的边的终点
    suggestion: str = ""                    # 修复建议


class ValidationResult:
    """验证结果"""
    def __init__(self):
        self.errors: List[ValidationError] = []

    @property
    def is_valid(self) -> bool:
        return not any(e.level == "error" for e in self.errors)

    @property
    def has_warnings(self) -> bool:
        return any(e.level == "warning" for e in self.errors)

    def add_error(
        self,
        code: str,
        message: str,
        node_id: Optional[str] = None,
        suggestion: str = "",
        **kwargs,
    ) -> None:
        self.errors.append(ValidationError(
            level="error", code=code, message=message,
            node_id=node_id, suggestion=suggestion, **kwargs,
        ))

    def add_warning(
        self,
        code: str,
        message: str,
        node_id: Optional[str] = None,
        suggestion: str = "",
        **kwargs,
    ) -> None:
        self.errors.append(ValidationError(
            level="warning", code=code, message=message,
            node_id=node_id, suggestion=suggestion, **kwargs,
        ))

    def add_info(self, code: str, message: str, **kwargs) -> None:
        self.errors.append(ValidationError(level="info", code=code, message=message, **kwargs))

    def __len__(self):
        return len(self.errors)

    def __iter__(self):
        return iter(self.errors)

    def format_report(self) -> str:
        """格式化完整的验证报告"""
        lines = [f"Graph Validation Report ({len(self.errors)} issues)"]
        
        error_count = sum(1 for e in self.errors if e.level == "error")
        warning_count = sum(1 for e in self.errors if e.level == "warning")
        info_count = sum(1 for e in self.errors if e.level == "info")
        
        lines.append(f"  Errors: {error_count}, Warnings: {warning_count}, Info: {info_count}")
        lines.append("")

        for err in self.errors:
            prefix = {"error": "✗", "warning": "⚠", "info": "ℹ"}[err.level]
            line = f"  [{prefix}] [{err.code}] {err.message}"
            if err.node_id:
                line += f" (node: {err.node_id})"
            if err.suggestion:
                line += f"\n      → {err.suggestion}"
            lines.append(line)

        return "\n".join(lines)


class GraphValidator:
    """图结构验证器
    
    提供静态方法进行各种维度的验证。
    """

    @staticmethod
    def validate(graph, raise_on_error: bool = False) -> ValidationResult:
        """
        执行完整验证
        
        Args:
            graph: WorkflowGraph 实例
            raise_on_error: 是否在发现 error 时抛出异常
            
        Returns:
            ValidationResult 包含所有问题
        """
        result = ValidationResult()

        # 1. 基础连通性
        GraphValidator._check_connectivity(graph, result)

        # 2. 孤立节点检测
        GraphValidator._check_isolated_nodes(graph, result)

        # 3. 节点完整性
        GraphValidator._check_node_completeness(graph, result)

        # 4. 边完整性
        GraphValidator._check_edge_completeness(graph, result)

        # 5. Router 一致性
        GraphValidator._check_router_consistency(graph, result)

        # 6. Fork/Merge 匹配
        GraphValidator._check_fork_merge_consistency(graph, result)

        # 7. 循环安全性
        GraphValidator._check_loop_safety(graph, result)

        # 8. 终止性检查
        GraphValidator._check_termination(graph, result)

        # 9. 状态 Schema 一致性
        GraphValidator._check_state_schema_consistency(graph, result)

        if raise_on_error and not result.is_valid:
            errors = [e.message for e in result.errors if e.level == "error"]
            raise ValueError(f"图验证失败:\n" + "\n".join(f"  - {e}" for e in errors))

        return result

    # ==================== 各维度验证逻辑 ====================

    @staticmethod
    def _check_connectivity(graph, result: ValidationResult) -> None:
        """检查图的连通性"""
        from .types import START_NODE_ID, END_NODE_ID

        node_ids = set(graph.node_ids)

        # 检查是否有节点
        if not node_ids:
            result.add_error("EMPTY_GRAPH", "图中没有任何节点")
            return

        # 找出从 START 可达的所有节点
        reachable = GraphValidator._bfs_reachable(graph, START_NODE_ID, direction="forward")
        unreachable_from_start = node_ids - reachable

        for nid in sorted(unreachable_from_start):
            result.add_warning(
                "UNREACHABLE_FROM_START",
                f"节点 '{nid}' 从 START 不可达",
                node_id=nid,
                suggestion="添加从 START 或其他节点的边连接到该节点",
            )

        # 检查是否能到达 END
        can_reach_end = set()
        for nid in node_ids:
            end_reachable = GraphValidator._bfs_reachable(graph, nid, direction="forward")
            if END_NODE_ID in end_reachable or nid == END_NODE_ID:
                can_reach_end.add(nid)

        cannot_reach_end = node_ids - can_reach_end - {END_NODE_ID}
        for nid in sorted(cannot_reach_end):
            result.add_warning(
                "CANNOT_REACH_END",
                f"从节点 '{nid}' 无法到达 END（可能导致悬挂状态）",
                node_id=nid,
                suggestion=f"添加从 '{nid}' 到 END 或后续节点的边",
            )

    @staticmethod
    def _check_isolated_nodes(graph, result: ValidationResult) -> None:
        """检测孤立节点（无任何边连接）"""
        referenced_nodes = set()
        for edge in graph.edges:
            referenced_nodes.add(edge.from_node)
            referenced_nodes.add(edge.to_node)

        for node in graph.nodes:
            if node.id not in referenced_nodes and node.id not in (
                getattr(graph.__class__, 'START_NODE_ID', '__start__'),
                getattr(graph.__class__, 'END_NODE_ID', '__end__'),
            ):
                result.add_warning(
                    "ISOLATED_NODE",
                    f"节点 '{node.id}' 没有任何边与之相连",
                    node_id=node.id,
                    suggestion="删除此节点或添加连接它的边",
                )

    @staticmethod
    def _check_node_completeness(graph, result: ValidationResult) -> None:
        """检查节点定义的完整性"""
        from .types import NodeType

        for node in graph.nodes:
            # AGENT 节点需要 agent_id
            if node.type == NodeType.AGENT and not node.agent_id:
                result.add_error(
                    "MISSING_AGENT_ID",
                    f"AGENT 节点 '{node.id}' 缺少 agent_id",
                    node_id=node.id,
                    suggestion=".agent('node_id', agent_id='some_agent')",
                )

            # FUNCTION 节点需要 func_ref
            if node.type == NodeType.FUNCTION and not node.func_ref:
                result.add_error(
                    "MISSING_FUNC_REF",
                    f"FUNCTION 节点 '{node.id}' 缺少 func_ref",
                    node_id=node.id,
                    suggestion=".function('node_id', func_ref='my_module.my_func')",
                )

            # ROUTER 节点需要 routes
            if node.type == NodeType.ROUTER and not node.routes:
                result.add_error(
                    "MISSING_ROUTES",
                    f"ROUTER 节点 '{node.id}' 缺少路由定义",
                    node_id=node.id,
                    suggestion='.router("id", routes={"pass": "next", "fail": "retry"})',
                )

            # FORK 节点需要 fork_targets
            if node.type == NodeType.FORK and not node.fork_targets:
                result.add_error(
                    "MISSING_FORK_TARGETS",
                    f"FORK 节点 '{node.id}' 缺少分叉目标列表",
                    node_id=node.id,
                    suggestion='.fork("id", targets=["a", "b", "c"])',
                )

            # Loop 节点的 body 内节点必须存在
            if node.type == NodeType.LOOP:
                node_ids_set = set(graph.node_ids)
                for body_node_id in node.loop_body:
                    if body_node_id not in node_ids_set:
                        result.add_error(
                            "LOOP_BODY_NODE_MISSING",
                            f"LOOP 节点 '{node.id}' 引用的体节点 '{body_node_id}' 不存在",
                            node_id=node.id,
                            suggestion=f"确保 '{body_node_id}' 已被定义为节点",
                        )

            # Subgraph 的 subgraph_id 应有引用
            if node.type == NodeType.SUBGRAPH and not node.subgraph_id:
                result.add_warning(
                    "MISSING_SUBGRAPH_ID",
                    f"SUBGRAPH 节点 '{node.id}' 缺少子图引用",
                    node_id=node.id,
                    suggestion='.subgraph("id", some_graph)',
                )

    @staticmethod
    def _check_edge_completeness(graph, result: ValidationResult) -> None:
        """检查边的完整性"""
        from .types import EdgeType, START_NODE_ID, END_NODE_ID

        node_ids = set(graph.node_ids)
        special_ids = {START_NODE_ID, END_NODE_ID}
        valid_ids = node_ids | special_ids

        for edge in graph.edges:
            # 检查端点存在性
            if edge.from_node not in valid_ids:
                result.add_error(
                    "INVALID_EDGE_SOURCE",
                    f"边的源节点 '{edge.from_node}' 不存在",
                    edge_from=edge.from_node,
                    edge_to=edge.to_node,
                )
            if edge.to_node not in valid_ids:
                result.add_error(
                    "INVALID_EDGE_TARGET",
                    f"边的目标节点 '{edge.to_node}' 不存在",
                    edge_from=edge.from_node,
                    edge_to=edge.to_node,
                )

            # CONDITIONAL 边必须有 condition
            if edge.edge_type == EdgeType.CONDITIONAL and not edge.condition:
                result.add_error(
                    "CONDITIONAL_EDGE_WITHOUT_CONDITION",
                    f"条件边 ('{edge.from_node}' → '{edge.to_node}') 缺少条件定义",
                    edge_from=edge.from_node,
                    edge_to=edge.to_node,
                    suggestion=".conditional_edge('from', 'to', 'state.xxx > 0.5')",
                )

    @staticmethod
    def _check_router_consistency(graph, result: ValidationResult) -> None:
        """检查 Router 节点的路由一致性"""
        from .types import NodeType, EdgeType

        routers = [n for n in graph.nodes if n.type == NodeType.ROUTER]

        for router in routers:
            route_keys = set(router.routes.keys())
            route_targets = set(router.routes.values())

            # 检查每个路由目标是否有对应的出边
            out_edges = graph.get_edges_from(router.id)
            conditional_targets = {
                e.to_node for e in out_edges
                if e.edge_type in (EdgeType.CONDITIONAL, EdgeType.DEFAULT)
            }

            missing_edges = route_targets - conditional_targets - {"__end__"}
            for target in missing_edges:
                result.add_warning(
                    "ROUTER_TARGET_MISSING_EDGE",
                    f"Router '{router.id}' 的路由目标 '{target}' 没有对应的条件边",
                    node_id=router.id,
                    suggestion=f'添加 .route_from("{router.id}").when(...).to("{target}")',
                )

            # 检查是否有孤立的出边（不匹配任何路由）
            orphan_edges = conditional_targets - route_targets - {"__end__"}
            for target in orphan_edges:
                result.add_info(
                    "ORPHAN_CONDITIONAL_EDGE",
                    f"Router '{router.id}' 有指向 '{target}' 的条件边但不在 routes 中",
                    node_id=router.id,
                )

    @staticmethod
    def _check_fork_merge_consistency(graph, result: ValidationResult) -> None:
        """检查 Fork/Merge 节点的一致性"""
        from .types import NodeType

        forks = [n for n in graph.nodes if n.type == NodeType.FORK]
        merges = [n for n in graph.nodes if n.type == NodeType.MERGE]

        for fork in forks:
            # 每个 fork 目标应该是已定义的节点
            node_ids = set(graph.node_ids)
            for target in fork.fork_targets:
                if target not in node_ids:
                    result.add_error(
                        "FORK_TARGET_MISSING",
                        f"Fork 节点 '{fork.id}' 的目标 '{target}' 未定义",
                        node_id=fork.id,
                        suggestion=f"先定义节点 .agent('{target}', ...)",
                    )

        for merge in merges:
            # 每个 merge 来源应该有对应的 fork 或独立节点
            for source in merge.merge_sources:
                source_node = graph.get_node(source)
                if not source_node:
                    result.add_warning(
                        "MERGE_SOURCE_MISSING",
                        f"Merge 节点 '{merge.id}' 的来源 '{source}' 未定义",
                        node_id=merge.id,
                    )

    @staticmethod
    def _check_loop_safety(graph, result: ValidationResult) -> None:
        """检查循环安全性"""
        from .types import EdgeType, NodeType, START_NODE_ID

        loop_edges = [e for e in graph.edges if e.edge_type == EdgeType.LOOP_BACK]

        if loop_edges:
            # 检查是否有 max_iterations 保护
            loop_nodes = [n for n in graph.nodes if n.type == NodeType.LOOP]

            for edge in loop_edges:
                to_node = graph.get_node(edge.to_node)
                has_protection = False

                # 检查目标是否是 Loop 节点或有 max_iterations 配置
                if to_node:
                    if to_node.type == NodeType.LOOP:
                        has_protection = True
                    elif to_node.config and to_node.config.retry_count > 0:
                        has_protection = True

                # 检查条件是否有终止可能
                if edge.condition and edge.condition.expression:
                    # 简单启发式：如果条件包含迭代次数相关变量
                    expr = edge.condition.expression.lower()
                    if any(kw in expr for kw in ['iteration', 'count', 'max', 'limit']):
                        has_protection = True

                if not has_protection:
                    result.add_warning(
                        "UNPROTECTED_LOOP",
                        f"回环边 ('{edge.from_node}' → '{edge.to_node}') 可能缺少终止保护",
                        edge_from=edge.from_node,
                        edge_to=edge.to_node,
                        suggestion="使用 Loop 节点包裹或设置 max_iterations / terminate_condition",
                    )
        else:
            # 检查是否存在隐式环（非 LOOP_BACK 类型）
            try:
                cycle = GraphValidator._detect_cycle(graph)
                if cycle:
                    result.add_warning(
                        "IMPLICIT_CYCLE_DETECTED",
                        f"图中检测到隐式环路: {' → '.join(cycle)}",
                        suggestion="如需循环请使用 LOOP_BACK 类型的边或 Loop 节点",
                    )
            except Exception:
                pass  # 循环检测失败不影响主流程

    @staticmethod
    def _check_termination(graph, result: ValidationResult) -> None:
        """检查图的终止性（确保所有路径都能到达 END）"""
        from .types import START_NODE_ID, END_NODE_ID

        # 使用 BFS 检测从 START 出发能否到达 END
        can_end = GraphValidator._bfs_reachable(graph, START_NODE_ID, direction="forward")
        if END_NODE_ID not in can_end:
            result.add_error(
                "NO_PATH_TO_END",
                "图中不存在从 START 到 END 的路径",
                suggestion="确保至少有一条路径能到达 END（可添加到终节点的边）",
            )

    @staticmethod
    def _check_state_schema_consistency(graph, result: ValidationResult) -> None:
        """检查状态 Schema 的一致性（轻量级）"""
        if not graph.state_schema:
            return  # Schema 是可选的

        schema_fields = set(graph.state_schema.get_field_names())

        # 条件表达式中引用的字段应该在 Schema 中声明
        for edge in graph.edges:
            if edge.condition and edge.condition.expression:
                expr = edge.condition.expression
                # 简单解析 state.xxx 引用
                import re
                refs = re.findall(r'state\.(\w+)', expr)
                for ref in refs:
                    if ref not in schema_fields and ref not in (
                        'values', 'routing_context', 'last_router_output'
                    ):
                        result.add_info(
                            "SCHEMA_FIELD_UNDECLARED",
                            f"条件表达式引用了未声明的状态字段 '{ref}'",
                            edge_from=edge.from_node,
                            edge_to=edge.to_node,
                            suggestion=f"在 StateSchema 中添加字段: .add_state_field('{ref}', ...)",
                        )

    # ==================== 图算法辅助 ====================

    @staticmethod
    def _bfs_reachable(graph, start_node: str, direction: str = "forward") -> Set[str]:
        """BFS 找出从指定节点可达的所有节点
        
        Args:
            graph: WorkflowGraph
            start_node: 起点
            direction: "forward" (沿边方向) 或 "backward" (逆边方向)
            
        Returns:
            可达节点集合（包含起点）
        """
        visited = set()
        queue = [start_node]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if direction == "forward":
                neighbors = graph.get_successors(current)
            else:
                neighbors = graph.get_predecessors(current)

            for neighbor in neighbors:
                if neighbor not in visited:
                    queue.append(neighbor)

        return visited

    @staticmethod
    def _detect_cycle(graph) -> Optional[List[str]]:
        """检测图中的环（DFS 方法）"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in graph.node_ids}
        parent = {}

        def dfs(node: str) -> Optional[List[str]]:
            color[node] = GRAY
            for successor in graph.get_successors(node):
                if successor == "__end__":
                    continue
                if successor not in color:
                    continue
                if color[successor] == GRAY:
                    # 找到环！回溯构建环路径
                    cycle = [successor, node]
                    current = node
                    while current in parent and parent[current] != successor:
                        current = parent[current]
                        cycle.append(current)
                    cycle.append(successor)
                    return list(reversed(cycle))
                if color[successor] == WHITE:
                    parent[successor] = node
                    result = dfs(successor)
                    if result:
                        return result
            color[node] = BLACK
            return None

        for nid in graph.node_ids:
            if color[nid] == WHITE:
                result = dfs(nid)
                if result:
                    return result

        return None
