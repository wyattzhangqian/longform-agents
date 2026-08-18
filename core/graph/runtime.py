"""Workflow Graph Engine — 执行引擎 (Runtime)

Platform-Core v4: 图的运行时执行器，负责：
- 调度（Scheduler）：决定下一个/批要执行的节点
- 路由（Router）：解析条件边，决定执行路径
- 节点执行（NodeExecutor）：实际调用 Agent/Function/Subgraph
- 并行执行（ParallelExecutor）：Fork/Merge 的并行协调
- 状态管理（StateManager）：共享状态的读写与 checkpoint
- 生命周期管理：初始化 → 运行 → 暂停 → 恢复 → 终止

架构:
    GraphRuntime
    ├── Scheduler        (调度器：就绪队列 + 并行组检测)
    ├── Router           (路由器：条件边求值)
    ├── NodeExecutor     (节点执行器：分发到不同类型)
    ├── ParallelExecutor (并行执行器：asyncio 协调)
    └── StateManager     (状态管理器：快照 + checkpoint)
"""

from __future__ import annotations
import asyncio
import logging
import os as _os
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union
from datetime import datetime
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

from .types import (
    WorkflowGraph,
    GraphNode,
    GraphEdge,
    GraphCondition,
    GraphState,
    ExecutionResult,
    GraphRunConfig,
    NodeType,
    EdgeType,
    ConditionType,
    NodeStatus,
    GraphStatus,
    MergeStrategy,
    START_NODE_ID,
    END_NODE_ID,
)


# ============================================================
# 函数注册表 (Handler Registry)
# ============================================================

class HandlerRegistry:
    """全局函数注册表
    
    将字符串引用名映射到实际的 Python callable。
    用于序列化后的函数恢复。
    
    使用方式:
        registry = HandlerRegistry()
        registry.register("my_func", my_function)
        fn = registry.get("my_func")
        
    质量检查请使用 Graph QUALITY_GATE 节点（D5），不在此注册 post-hook。
    """

    _instance: Optional['HandlerRegistry'] = None

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._condition_handlers: Dict[str, Callable] = {}

    @classmethod
    def get_global(cls) -> 'HandlerRegistry':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, ref_name: str, handler: Callable) -> None:
        """注册一个函数"""
        self._handlers[ref_name] = handler

    def register_condition(self, ref_name: str, handler: Callable) -> None:
        """注册条件判断函数"""
        self._condition_handlers[ref_name] = handler

    def get(self, ref_name: str) -> Optional[Callable]:
        """获取已注册的函数"""
        return self._handlers.get(ref_name)

    def get_condition(self, ref_name: str) -> Optional[Callable]:
        """获取已注册的条件函数"""
        return self._condition_handlers.get(ref_name)

    def has(self, ref_name: str) -> bool:
        return ref_name in self._handlers


# ============================================================
# Scheduler — 调度器
# ============================================================

@dataclass
class ScheduleResult:
    """调度结果"""
    nodes: List[str]                          # 可执行的节点 ID 列表
    is_parallel_group: bool = False          # 是否为并行组
    reason: str = ""                          # 调度原因说明


class Scheduler:
    """图调度器
    
    基于拓扑排序的就绪队列调度算法。
    支持：
    - 入度追踪（DAG 正确性保证）
    - 并行组检测（可同时执行的独立节点）
    - 循环检测（安全上限）
    
    状态机：
        INITIALIZED → SCHEDULING → COMPLETED / DEADLOCKED
    """

    def __init__(self, graph: WorkflowGraph):
        self.graph = graph
        self.ready: Set[str] = set()         # 就绪节点集合
        self.running: Set[str] = set()       # 正在执行中
        self.completed: Set[str] = set()     # 已完成
        self.failed: Set[str] = set()        # 已失败
        self.pending: Dict[str, int] = {}    # node_id → 剩余未完成的前驱数
        self._initialized = False
        # Phase 5 新增
        self._precondition_cache: Dict[str, bool] = {}
        self._run_context: Optional[Any] = None  # 由 GraphRuntime 注入

    def set_run_context(self, ctx):
        """Phase 5: 由 GraphRuntime 在执行开始时调用，注入 RunContext 引用"""
        self._run_context = ctx

    def initialize(self, initial_nodes: Optional[List[str]] = None) -> None:
        """
        初始化调度器
        
        计算每个节点的入度，将入度为 0 的节点标记为 ready。
        
        Args:
            initial_nodes: 强制指定的起始节点列表（覆盖自动计算）
        """
        self.pending.clear()
        self.ready.clear()
        self.running.clear()
        self.completed.clear()
        self.failed.clear()

        all_node_ids = set(self.graph.node_ids)

        # 初始化入度计数
        for node_id in all_node_ids:
            in_edges = self.graph.get_edges_to(node_id)
            # 排除 LOOP_BACK 边的初始入度（循环边不阻塞首次执行）
            effective_in = [
                e for e in in_edges if e.edge_type != EdgeType.LOOP_BACK
            ]
            self.pending[node_id] = len(effective_in)

        # 标记入度为 0 的节点为 ready
        if initial_nodes:
            for nid in initial_nodes:
                if nid in all_node_ids:
                    self.ready.add(nid)
                    self.pending[nid] = 0
        else:
            for node_id in all_node_ids:
                # 入度为 0 或仅从 START 来
                if self.pending[node_id] == 0:
                    self.ready.add(node_id)
                elif self._is_start_target(node_id):
                    self.ready.add(node_id)
                    self.pending[node_id] = 0

        self._initialized = True

    def next(self) -> Optional[ScheduleResult]:
        """
        调度决策：返回下一批应执行的节点
        
        Returns:
            ScheduleResult 包含可执行节点列表。
            如果返回 None，表示无可执行节点（完成或死锁）。
        """
        if not self.ready and not self.running:
            return None  # 全部完成或死锁

        if not self.ready:
            return ScheduleResult(nodes=[], reason="等待正在运行的节点完成")

        # === Phase 5 新增：过滤不满足约束的节点 ===
        ready_and_eligible = []
        waiting_on_preconditions = []
        for node_id in list(self.ready):
            if self._check_preconditions(node_id):
                ready_and_eligible.append(node_id)
            else:
                waiting_on_preconditions.append(node_id)

        if not ready_and_eligible:
            if waiting_on_preconditions:
                return ScheduleResult(
                    nodes=[],
                    reason=f"waiting on preconditions: {waiting_on_preconditions[:3]}"
                )
            return None

        # 检测并行组：用过滤后的候选列表
        parallel_group = self._find_parallel_group_from(ready_and_eligible)

        if len(parallel_group) > 1:
            # 将整组从 ready 移到 running
            for nid in parallel_group:
                self.ready.discard(nid)
                self.running.add(nid)
            return ScheduleResult(
                nodes=parallel_group,
                is_parallel_group=True,
                reason=f"并行组: {parallel_group}",
            )

        # 单节点调度（从符合条件的节点中取第一个）
        node_id = ready_and_eligible[0]
        self.ready.discard(node_id)
        self.running.add(node_id)

        return ScheduleResult(
            nodes=[node_id],
            is_parallel_group=False,
            reason=f"单节点: {node_id}",
        )

    def snapshot(self) -> Dict[str, Any]:
        """导出调度器状态（checkpoint 恢复用）"""
        return {
            "completed": sorted(self.completed),
            "ready": sorted(self.ready),
            "failed": sorted(self.failed),
            "pending": dict(self.pending),
        }

    def restore_from_snapshot(self, data: Dict[str, Any]) -> None:
        """从 checkpoint 恢复调度器"""
        self.completed = set(data.get("completed") or [])
        self.ready = set(data.get("ready") or [])
        self.failed = set(data.get("failed") or [])
        self.pending = dict(data.get("pending") or {})
        self.running = set()
        self._initialized = True

    def complete(self, node_id: str, success: bool = True) -> List[str]:
        """
        标记节点执行完成，释放后继节点
        
        Args:
            node_id: 完成的节点 ID
            success: 是否成功
            
        Returns:
            新变为 ready 的后继节点列表
        """
        self.running.discard(node_id)

        if success:
            self.completed.add(node_id)
        else:
            self.failed.add(node_id)
            # 失败节点的 ERROR_EDGE 特殊处理
            error_targets = self._get_error_targets(node_id)
            if error_targets:
                for target in error_targets:
                    self.pending[target] = max(0, self.pending.get(target, 1) - 1)
                    if self.pending[target] <= 0 and target not in self.completed:
                        self.ready.add(target)
            # P0-6 修复：失败节点不放行常规后继（无论是否有 error_edge）
            # 避免失败被吞、后继照常跑、run 以 COMPLETED 收尾
            return []

        # 释放常规后继
        newly_ready = []
        out_edges = self.graph.get_edges_from(node_id)

        for edge in out_edges:
            target = edge.to_node
            if target == END_NODE_ID:
                continue

            # 条件边不在调度阶段处理（留给 Router）
            if edge.edge_type == EdgeType.CONDITIONAL:
                continue

            # LOOP_BACK 边特殊处理：只增加目标入度（不减少）
            if edge.edge_type == EdgeType.LOOP_BACK:
                # Loop back 不在这里处理，由 Runtime 的循环逻辑控制
                continue

            # 常规边：减少目标入度
            self.pending[target] = self.pending.get(target, 1) - 1
            if self.pending[target] <= 0 and target not in self.completed:
                if target not in self.ready and target not in self.running:
                    self.ready.add(target)
                    newly_ready.append(target)

        return newly_ready

    @property
    def is_complete(self) -> bool:
        """是否所有节点都已完成（或失败）"""
        total = len(self.graph.node_ids)
        done = len(self.completed) + len(self.failed)
        return done >= total and len(self.running) == 0 and len(self.ready) == 0

    @property
    def is_deadlocked(self) -> bool:
        """是否处于死锁状态"""
        has_pending_work = len(self.running) > 0 or len(self.ready) > 0
        return not has_pending_work and not self.is_complete

    @property
    def progress(self) -> Dict[str, int]:
        """当前进度统计"""
        return {
            "total": len(self.graph.node_ids),
            "completed": len(self.completed),
            "failed": len(self.failed),
            "running": len(self.running),
            "ready": len(self.ready),
            "remaining": len(self.ready) + len(self.running),
        }

    # ==================== 内部方法 ====================

    def _find_parallel_group(self) -> List[str]:
        """找出可并行的独立节点组
        
        算法：在当前 ready 集合中，找出彼此之间没有依赖关系的节点子集。
        这里使用简化版本：如果 ready 中有多个节点且它们彼此不是前驱关系，
        则可以并行。
        """
        ready_list = list(self.ready)
        if len(ready_list) <= 1:
            return ready_list or []

        parallel = []
        for node_id in ready_list:
            # 检查这个节点是否被同组其他节点依赖
            is_independent = True
            for other_id in ready_list:
                if other_id == node_id:
                    continue
                # 如果 other 是 node_id 的前驱，则不能并行
                if node_id in self.graph.get_successors(other_id):
                    is_independent = False
                    break
                # 如果 node_id 是 other 的前驱，则不能并行
                if other_id in self.graph.get_successors(node_id):
                    is_independent = False
                    break

            if is_independent:
                parallel.append(node_id)

        return parallel if len(parallel) > 1 else (ready_list[:1] if ready_list else [])

    def _find_parallel_group_from(self, candidates: List[str]) -> List[str]:
        """Phase 5: 从候选列表中找出可并行的组（参数化版本）"""
        if len(candidates) <= 1:
            return candidates
        parallel = []
        for node_id in candidates:
            is_independent = True
            for other_id in candidates:
                if other_id == node_id:
                    continue
                if node_id in self.graph.get_successors(other_id):
                    is_independent = False
                    break
                if other_id in self.graph.get_successors(node_id):
                    is_independent = False
                    break
            if is_independent:
                parallel.append(node_id)
        return parallel if len(parallel) > 1 else (candidates[:1] if candidates else [])

    def _check_preconditions(self, node_id: str) -> bool:
        """Phase 5: 检查节点的前置约束条件（纯内存，不做 IO/LLM）"""
        node = None
        for n in self.graph.nodes:
            if n.id == node_id:
                node = n
                break
        if not node:
            return True
        preconditions = (node.metadata or {}).get("preconditions", [])
        if not preconditions:
            return True
        for cond in preconditions:
            cond_type = cond.get("type", "")
            if cond_type == "artifact_approved":
                art_result = self._check_artifact_condition(cond)
                if art_result is False:
                    return False
                if art_result is None:
                    # 查询失败放行（2026-08-09 P1：不静默，记录降级）
                    self._record_degradation(
                        "warning", "precondition",
                        "artifact_approved 状态查询失败，已放行（约束可能被跳过）",
                        "约束检查被跳过",
                    )
            elif cond_type == "unit_completed":
                required_unit = cond.get("unit_number", 0)
                if self._run_context:
                    current = getattr(self._run_context, 'current_unit', None) or 1
                    if current <= required_unit:
                        return False
            elif cond_type == "quality_passed":
                target_node = cond.get("node_id", "")
                if self._run_context:
                    records = getattr(self._run_context, 'quality_records', None) or []
                    passed = any(
                        r.phase_id == target_node and r.passed
                        for r in records
                    )
                    if not passed:
                        return False
        return True

    def _check_artifact_condition(self, cond: dict) -> Optional[bool]:
        """Gap2.3 修复：检查 Artifact 是否已 approved（同步查 DB）

        返回 None 表示查询失败（调用方应放行但记录降级）。
        """
        artifact_id = cond.get("artifact_id") or cond.get("id")
        if not artifact_id:
            return True
        try:
            import sqlite3
            from pathlib import Path as _Path
            db_path = _Path(__file__).resolve().parents[2] / "data" / "agent_platform.db"
            if not db_path.exists():
                return True
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute("SELECT status FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
                return bool(row and row[0] == "approved")
            finally:
                conn.close()
        except Exception:
            return None  # 查询失败：调用方放行但记录降级（不私下静默）

    def _record_degradation(
        self, severity, component: str, message: str, recovery_action: str = "",
    ) -> None:
        """Scheduler 内降级记录 — 委托给 RunContext.record_degradation（若可用）。"""
        if self._run_context and hasattr(self._run_context, "record_degradation"):
            try:
                self._run_context.record_degradation(severity, component, message, recovery_action)
            except Exception:
                pass

    def _is_start_target(self, node_id: str) -> bool:
        """检查节点是否是 START 的直接目标"""
        for edge in self.graph.edges:
            if edge.from_node == START_NODE_ID and edge.to_node == node_id:
                return True
        return False

    def _get_error_targets(self, node_id: str) -> List[str]:
        """获取错误处理边的目标"""
        targets = []
        for edge in self.graph.get_edges_from(node_id):
            if edge.edge_type == EdgeType.ERROR_EDGE:
                targets.append(edge.to_node)
        return targets


# ============================================================
# Router — 路由解析器
# ============================================================

class Router:
    """条件路由解析器
    
    在节点执行完成后，根据执行结果和当前状态，
    解析该节点出发的所有条件边，决定下一步应该走哪条路。
    
    支持的条件评估策略：
    1. EXPRESSION: 基于 state 的表达式求值
    2. HANDLER_FN: 调用 Python async callable
    3. LLM_ROUTER: LLM 动态决策
    4. CONSENSUS: 多 Agent 共识投票
    """

    def __init__(
        self,
        graph: WorkflowGraph,
        handler_registry: Optional[HandlerRegistry] = None,
    ):
        self.graph = graph
        self.registry = handler_registry or HandlerRegistry.get_global()
        # LLM 路由缓存：一次 resolve() 内只调一次 LLM，多个边缘共享结果
        self._llm_route_cache: Optional[str] = None

    async def resolve(
        self,
        source_node_id: str,
        execution_result: ExecutionResult,
        state: GraphState,
    ) -> List[str]:
        """
        解析从指定节点出发的路由决策

        Args:
            source_node_id: 刚执行完的节点 ID
            execution_result: 该节点的执行结果
            state: 当前图状态

        Returns:
            下一步应该执行的目标节点 ID 列表。
            空列表表示无后续动作（可能终止）。
        """
        # 每次 resolve() 调用重置 LLM 路由缓存，避免跨节点污染
        self._llm_route_cache = None

        out_edges = self.graph.get_edges_from(source_node_id)
        targets = []
        default_target: Optional[str] = None

        for edge in out_edges:
            # END 节点始终可达
            if edge.to_node == END_NODE_ID:
                continue

            if edge.edge_type == EdgeType.DEFAULT:
                default_target = edge.to_node
                continue

            should_traverse = await self._evaluate_edge(
                edge, execution_result, state
            )
            if should_traverse:
                targets.append(edge.to_node)

        # 如果没有条件匹配，走默认分支
        if not targets and default_target:
            targets.append(default_target)

        return targets

    async def evaluate_condition(
        self,
        condition: GraphCondition,
        result: ExecutionResult,
        state: GraphState,
    ) -> bool:
        """评估单个条件（公共方法，供外部使用）"""
        return await self._evaluate_condition_impl(condition, result, state)

    async def _evaluate_edge(
        self,
        edge: GraphEdge,
        result: ExecutionResult,
        state: GraphState,
    ) -> bool:
        """评估边的遍历条件"""
        if edge.edge_type == EdgeType.ALWAYS:
            return True

        if edge.edge_type == EdgeType.PARALLEL_JOIN:
            # 并行汇合边由 Scheduler 控制
            return True

        if edge.edge_type == EdgeType.LOOP_BACK:
            # 回环边需要检查终止条件
            if edge.condition:
                return await self._evaluate_condition_impl(
                    edge.condition, result, state
                )
            # 无条件的 loop_back 默认可以走（但受限于 max_iterations）
            return True

        if edge.condition:
            # LLM_ROUTER: 触发 LLM 决策（写入 self._llm_route_cache），
            # 然后根据缓存判断当前边缘是否匹配 LLM 选中的目标。
            if edge.condition.condition_type == ConditionType.LLM_ROUTER:
                await self._evaluate_condition_impl(edge.condition, result, state)
                # 缓存由 _eval_llm_router 设置；匹配则遍历此边
                if self._llm_route_cache and self._llm_route_cache == edge.to_node:
                    return True
                return False

            return await self._evaluate_condition_impl(
                edge.condition, result, state
            )

        return True

    async def _evaluate_condition_impl(
        self,
        condition: GraphCondition,
        result: ExecutionResult,
        state: GraphState,
    ) -> bool:
        """条件评估的分发实现"""

        if condition.condition_type == ConditionType.EXPRESSION:
            return self._eval_expression(condition.expression, state)

        elif condition.condition_type == ConditionType.HANDLER_FN:
            return await self._eval_handler_fn(
                condition.handler_ref, result, state
            )

        elif condition.condition_type == ConditionType.LLM_ROUTER:
            return await self._eval_llm_router(condition, result, state)

        elif condition.condition_type == ConditionType.CONSENSUS:
            return await self._eval_consensus(condition, result, state)

        elif condition.condition_type == ConditionType.CUSTOM:
            return await self._eval_handler_fn(
                condition.handler_ref, result, state
            )

        return False

    @staticmethod
    def _eval_expression(expression: str, state: "GraphState") -> bool:
        """安全表达式求值 — AST 白名单解析，不再使用 eval()"""
        import ast
        try:
            tree = ast.parse(expression, mode="eval")
            return Router._eval_ast_node(tree.body, state)
        except (SyntaxError, ValueError, KeyError, TypeError) as e:
            import logging
            logging.getLogger("graph.router").warning(
                "表达式求值失败 [%s]: %s", expression, e,
            )
            return False

    @staticmethod
    def _eval_ast_node(node, state: "GraphState"):
        """递归解析 AST 节点 — 仅支持白名单操作"""
        import ast
        import operator

        if isinstance(node, ast.Compare):
            left = Router._eval_ast_node(node.left, state)
            comparators = [Router._eval_ast_node(c, state) for c in node.comparators]
            for op, right in zip(node.ops, comparators):
                op_fn = {
                    ast.Eq: operator.eq, ast.NotEq: operator.ne,
                    ast.Lt: operator.lt, ast.LtE: operator.le,
                    ast.Gt: operator.gt, ast.GtE: operator.ge,
                    ast.In: lambda a, b: a in b,
                    ast.NotIn: lambda a, b: a not in b,
                    ast.Is: operator.is_, ast.IsNot: operator.is_not,
                }.get(type(op))
                if op_fn is None:
                    raise ValueError(f"不支持的比较运算符: {type(op).__name__}")
                if not op_fn(left, right):
                    return False
                left = right
            return True

        if isinstance(node, ast.BoolOp):
            values = [Router._eval_ast_node(v, state) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
            raise ValueError(f"不支持的逻辑运算符: {type(node.op).__name__}")

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not Router._eval_ast_node(node.operand, state)

        if isinstance(node, ast.BinOp):
            left = Router._eval_ast_node(node.left, state)
            right = Router._eval_ast_node(node.right, state)
            op_fn = {
                ast.Add: operator.add, ast.Sub: operator.sub,
                ast.Mult: operator.mul, ast.Div: operator.truediv,
                ast.Mod: operator.mod,
            }.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"不支持的二元运算符: {type(node.op).__name__}")
            return op_fn(left, right)

        if isinstance(node, ast.Name):
            name = node.id
            if name == "True":
                return True
            if name == "False":
                return False
            if name == "None":
                return None
            if hasattr(state, name):
                return getattr(state, name)
            if name in state.values:
                return state.values[name]
            if name == "routing_context" and hasattr(state, "routing_context"):
                return state.routing_context
            name_fn = {
                "len": len, "str": str, "int": int, "float": float,
                "bool": bool, "abs": abs, "min": min, "max": max,
            }.get(name)
            if name_fn is not None:
                return name_fn
            raise ValueError(f"未知变量: {name}")

        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.Attribute):
            obj = Router._eval_ast_node(node.value, state)
            if hasattr(obj, node.attr):
                return getattr(obj, node.attr)
            if isinstance(obj, dict):
                return obj.get(node.attr)
            raise ValueError(f"无法访问 .{node.attr}")

        if isinstance(node, ast.Subscript):
            obj = Router._eval_ast_node(node.value, state)
            key = Router._eval_ast_node(node.slice, state)
            return obj[key]

        if isinstance(node, ast.Call):
            func = Router._eval_ast_node(node.func, state)
            args = [Router._eval_ast_node(a, state) for a in node.args]
            kwargs = {kw.arg: Router._eval_ast_node(kw.value, state) for kw in node.keywords}
            return func(*args, **kwargs)

        if isinstance(node, ast.List):
            return [Router._eval_ast_node(e, state) for e in node.elts]
        if isinstance(node, ast.Dict):
            return {
                Router._eval_ast_node(k, state): Router._eval_ast_node(v, state)
                for k, v in zip(node.keys, node.values)
            }

        raise ValueError(f"不支持的 AST 节点: {type(node).__name__}")

    async def _eval_handler_fn(
        self,
        handler_ref: str,
        result: ExecutionResult,
        state: GraphState,
    ) -> bool:
        """调用注册的处理器函数"""
        handler = self.registry.get_condition(handler_ref)
        if not handler:
            handler = self.registry.get(handler_ref)
        if not handler:
            raise RuntimeError(f"未注册的处理函数: '{handler_ref}'")

        if asyncio.iscoroutinefunction(handler):
            return await handler(result, state)
        else:
            return handler(result, state)

    async def _eval_llm_router(
        self,
        condition: GraphCondition,
        result: ExecutionResult,
        state: GraphState,
    ) -> bool:
        """LLM 动态路由 — 调用 LLM 在多个目标节点间做出路由决策。

        同一组出边共享一次 LLM 调用（_llm_route_cache），避免重复请求。
        """
        import json as _json
        import logging
        logger = logging.getLogger("graph.router.llm")

        options = condition.llm_options
        if not options:
            logger.warning("LLM Router 缺少 llm_options，回退默认")
            return False

        # 此边缘不在 LLM 选中的目标列表中 → False
        # 注意：调用方是 _evaluate_edge，需要知道当前 edge.to_node 是否是 LLM 选中的目标。
        # 但 _eval_condition 没有 edge 上下文。我们需要在 Router 层面缓存 LLM 决策，
        # 然后在 _evaluate_edge 中根据缓存判断。
        #
        # 简化方案：所有 LLM_ROUTER 边缘共享同一个 condition 实例，
        # 第一次调用时请求 LLM 并缓存结果到 self._llm_route_cache，
        # 后续调用直接读缓存。
        if self._llm_route_cache is not None:
            return False  # 决策已由第一次边缘做出，后续边缘不再重复匹配

        # 构建路由 prompt
        template = (condition.llm_prompt_template or "").strip()
        output_text = ""
        if result and result.output:
            output_text = str(result.output)[:2000]
        elif result and result.error:
            output_text = f"[执行失败] {result.error[:500]}"

        prompt = template
        if "{output}" in prompt:
            prompt = prompt.replace("{output}", output_text)
        elif "{result}" in prompt:
            prompt = prompt.replace("{result}", output_text)
        elif output_text:
            prompt = f"{template}\n\n节点输出:\n{output_text}" if template else output_text

        if not prompt:
            prompt = "根据节点执行结果，选择最合适的下一步。只返回目标标识符。"

        options_text = "\n".join(f"- {o}" for o in options)
        full_prompt = (
            f"{prompt}\n\n"
            f"可选目标:\n{options_text}\n\n"
            f"只返回一个目标标识符（从上述列表中选），不要返回其他内容。"
        )

        try:
            from tools.llm_client import LLMClient
            from config import DEFAULT_LLM_MODEL
            client = LLMClient({"model": DEFAULT_LLM_MODEL, "temperature": 0.2, "max_tokens": 64})
            raw = await client.chat(full_prompt)
            chosen = (raw or "").strip().strip("`\"'").split("\n")[0].strip()
        except Exception as e:
            logger.warning("LLM Router 调用失败，回退到第一个选项: %s", e)
            chosen = options[0] if options else ""

        # 缓存决策（同一组边缘后续不再调 LLM）
        self._llm_route_cache = chosen
        logger.info("LLM Router 决策: options=%s → %s", options, chosen)
        return False  # 本轮不直接返回 True，让 _evaluate_edge 根据缓存判断

    async def _eval_consensus(
        self,
        condition: GraphCondition,
        result: ExecutionResult,
        state: GraphState,
    ) -> bool:
        """共识检测（多 Agent 加权投票 + LLM 仲裁）

        策略：
        1. 收集所有参与者的成功输出
        2. 达到阈值 → 直接通过
        3. 接近阈值（差距 ≤30%）→ LLM 仲裁判断是否存在实质共识
        4. 低于阈值 → 不通过
        """
        participants = condition.consensus_participants
        threshold = condition.consensus_threshold

        if not participants:
            return True  # 无参与者默认通过

        # 收集参与者输出，按质量分加权
        votes = []
        total_weight = 0.0
        for pid in participants:
            output = state.get_node_output(pid)
            if output and output.is_success:
                weight = float(getattr(output, 'quality_score', None) or 0.5)
                votes.append((pid, str(output.output)[:1000], weight))
                total_weight += weight

        if not votes:
            return False

        consensus_ratio = len(votes) / len(participants)
        if consensus_ratio >= threshold:
            return True

        # 接近阈值时用 LLM 判断是否存在实质共识
        gap = threshold - consensus_ratio
        if gap <= 0.3 and len(votes) >= 2:
            try:
                from tools.llm_client import LLMClient
                from config import DEFAULT_LLM_MODEL
                vote_texts = "\n---\n".join(
                    f"参与者 {pid}:\n{text[:600]}" for pid, text, _ in votes
                )
                prompt = (
                    f"以下是 {len(votes)}/{len(participants)} 个参与者的输出（阈值 {threshold:.0%}）：\n\n"
                    f"{vote_texts}\n\n"
                    f"判断：这些输出是否存在实质共识（即核心结论一致、可以合并为一个结果）？"
                    f"只返回 YES 或 NO。"
                )
                client = LLMClient({"model": DEFAULT_LLM_MODEL, "temperature": 0.1, "max_tokens": 8})
                answer = (await client.chat(prompt)).strip().upper()
                if answer.startswith("YES"):
                    return True
            except Exception:
                pass  # LLM 不可用时回退到简单投票

        return False


# ============================================================
# NodeExecutor — 节点执行器
# ============================================================


def resolve_agent_timeout(agent_config: dict, node) -> float:
    """预算化 Agent 节点超时：轮数 × 单轮预算 + 终稿 + 保底，硬上限封顶。

    背景（2026-08-08）：旧实现是固定 AGENT_TIMEOUT_SECONDS（300/600），与
    工具循环预算（max_tool_iterations 轮 × 每轮 LLM+工具）不对齐——8 轮正常
    执行的上界（含偶发 50s+ LLM 调用，实测单次最长 52.7s）就可能超过固定值，
    保护机制误杀正常执行。预算化让"正常+偶发慢"永不超时，
    只有真卡死（持续超预算）才被 wait_for 掐。

    优先级：
      1. node.metadata.timeout_seconds 显式配置（最优先，按节点覆盖）
      2. 预算化推导（默认）

    可配参数（环境变量）：
      LLM_TIMEOUT_SECONDS=120             每轮 LLM 单次调用预算（复用全局）
      PER_ROUND_TOOL_BUDGET_SECONDS=60    每轮工具执行预算（file/web/生成等）
      FINAL_GEN_BUDGET_SECONDS=180        终稿长文生成预算
      AGENT_TIMEOUT_SECONDS=300           保底（兼容旧配置语义）
      AGENT_TIMEOUT_CAP_SECONDS=3600      硬上限（防预算过大拖死 run）

    示例（默认 8 轮）：300 + 8×(120+60) + 180 = 1920s ≈ 32min 上限。
    正常执行（实测 ~5min）永不误杀；真卡死由 LLM 熔断 + 此上限双重兜底。
    """
    node_timeout = node.metadata.get("timeout_seconds")
    if node_timeout:
        return float(node_timeout)
    from core.agent.base import DEFAULT_MAX_TOOL_ITERATIONS
    max_iters = int((agent_config or {}).get("max_tool_iterations") or DEFAULT_MAX_TOOL_ITERATIONS)
    llm_timeout = float(_os.getenv("LLM_TIMEOUT_SECONDS", "120"))
    tool_budget = float(_os.getenv("PER_ROUND_TOOL_BUDGET_SECONDS", "60"))
    final_budget = float(_os.getenv("FINAL_GEN_BUDGET_SECONDS", "180"))
    base = float(_os.getenv("AGENT_TIMEOUT_SECONDS", "300"))
    cap = float(_os.getenv("AGENT_TIMEOUT_CAP_SECONDS", "3600"))
    budget = base + max_iters * (llm_timeout + tool_budget) + final_budget
    return min(budget, cap)


def agent_result_has_content(result: Any) -> bool:
    """Agent 执行结果是否有实际内容（2026-08-09：reasoning 模型空产出检测）。

    reasoning 模型可能把输出预算全吃在思维链上 → result 为空（writer 实测多次
    空产出 → 节点 FAILED → run partial）。空结果触发重试（不重试显式 status:error）。
    """
    if result is None:
        return False
    if isinstance(result, dict):
        if result.get("status") == "error":
            return True  # 显式错误不重试（避免掩盖真实错误）
        content = (
            result.get("result")
            or result.get("content")
            or result.get("text")
            or result.get("output")
            or ""
        )
        return bool(str(content).strip())
    return bool(str(result).strip())


class NodeExecutor:
    """节点执行器
    
    根据 NodeType 分发执行逻辑到不同的处理器。
    """

    def __init__(
        self,
        graph: WorkflowGraph,
        handler_registry: Optional[HandlerRegistry] = None,
    ):
        self.graph = graph
        self.registry = handler_registry or HandlerRegistry.get_global()
        self._agent_cache: Dict[str, Any] = {}  # agent_id → BaseAgent instance
        self._emit_fn: Optional[Callable] = None
        self._adaptive_router: Optional[Any] = None  # AdaptiveRouter instance (lazy init)
        self._live_run_ctx: Optional[Any] = None  # 单活 RunContext，并行节点共享
        self.run_id: str = ""  # P0-4: 由 GraphRuntime.execute 注入（超时 emit 用）

    def set_live_run_ctx(self, run_ctx: Optional[Any]) -> None:
        """由 GraphRuntime 注入单活 RunContext，避免并行节点副本互相覆盖"""
        self._live_run_ctx = run_ctx

    def set_event_emitter(self, emit_fn: Callable) -> None:
        """注入 GraphRuntime 事件广播（quality / human 节点 SSE）"""
        self._emit_fn = emit_fn

    def _emit(self, event_type: str, data: dict) -> None:
        if self._emit_fn:
            self._emit_fn(event_type, data)

    def _get_adaptive_router(self, config: GraphRunConfig):
        """获取/初始化 AdaptiveRouter（仅 adaptive 模式）"""
        if config.mode != "adaptive":
            return None
        if self._adaptive_router is None:
            from core.run.adaptive_router import AdaptiveRouter
            from tools.llm_client import LLMClient
            llm = LLMClient()
            self._adaptive_router = AdaptiveRouter(llm, config)
        return self._adaptive_router

    async def execute(
        self,
        node: GraphNode,
        state: GraphState,
        run_config: GraphRunConfig,
    ) -> ExecutionResult:
        """
        执行单个节点
        
        Args:
            node: 要执行的节点定义
            state: 当前图状态
            run_config: 运行配置
            
        Returns:
            执行结果
        """
        start_time = time.time()
        started_at = datetime.now().isoformat()
        state.current_node_id = node.id

        # Dry run 检查
        if run_config.dry_run:
            return ExecutionResult(
                node_id=node.id,
                status=NodeStatus.COMPLETED,
                output={"dry_run": True, "node_type": node.type.value},
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
                duration_ms=0,
        )

        try:
            # ── 节点级工具触发：检查 PhaseSpec.tool_bindings ──
            tool_bindings = node.metadata.get("tool_bindings") or []
            if tool_bindings and node.type == NodeType.AGENT:
                for binding in tool_bindings:
                    trigger = binding.get("trigger", "")
                    if trigger == "phase_entry":
                        tool_id = binding.get("tool_id", "")
                        if tool_id:
                            _logger.info(
                                "节点 %s 确定性触发工具 %s (trigger=phase_entry)",
                                node.id, tool_id,
                            )
                            try:
                                await self._trigger_phase_tool(
                                    node, tool_id, binding.get("params", {}), state, run_config,
                                    preset_id=binding.get("preset", ""),
                                )
                            except Exception as e:
                                _logger.warning("phase_entry 工具触发失败（不阻塞 agent 执行）: %s", e)

            # 分发到不同的执行逻辑（2026-08-09 P0：统一超时保护，除 AGENT 外全部 wait_for）
            dispatch_coro = self._dispatch_node(node, state, run_config)
            node_timeout = self._resolve_node_timeout(node)
            if node_timeout is not None:
                try:
                    result = await asyncio.wait_for(dispatch_coro, timeout=node_timeout)
                except asyncio.TimeoutError:
                    result = self._node_timeout_result(node, node_timeout)
            else:
                result = await dispatch_coro

            end_time = time.time()

            # Adaptive 路由评估：在 Agent 节点执行后评估产出质量
            if (
                run_config.mode == "adaptive"
                and node.type == NodeType.AGENT
                and not run_config.dry_run
            ):
                result = await self._adaptive_evaluate(
                    node, state, run_config, result,
                )

            node_status = NodeStatus.COMPLETED
            if (
                node.type == NodeType.HUMAN_IN_THE_LOOP
                and isinstance(result, dict)
                and result.get("human_input_required")
            ):
                node_status = NodeStatus.PAUSED
            elif (
                isinstance(result, dict)
                and result.get("status") in ("error", "timeout")
            ):
                # 状态传导：agent 层返回 error/timeout（超时/异常/质量门超时）应标节点失败，
                # 否则不进 failed_nodes，_finalize 状态诚实失效（误判 completed）
                node_status = NodeStatus.FAILED

            exec_result = ExecutionResult(
                node_id=node.id,
                status=node_status,
                output=result,
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
                duration_ms=(end_time - start_time) * 1000,
            )
            state.set_node_output(node.id, exec_result)
            return exec_result

        except asyncio.CancelledError:
            # P0-5 修复：取消信号必须传播，不能吞掉伪装成暂停（否则整体超时/关停失效）
            _logger.info("节点执行被取消: node=%s", node.id)
            raise
        except Exception as e:
            tb_str = traceback.format_exc()
            exec_result = ExecutionResult(
                node_id=node.id,
                status=NodeStatus.FAILED,
                output=None,
                error=str(e),
                error_type=type(e).__name__,
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
                metadata={"traceback": tb_str},
            )
            state.set_node_output(node.id, exec_result)
            return exec_result

    # ==================== 统一超时覆盖（2026-08-09 P0） ====================

    async def _dispatch_node(
        self,
        node: GraphNode,
        state: GraphState,
        run_config: GraphRunConfig,
    ) -> Any:
        """按节点类型分发执行逻辑（原 execute 内 if/elif 分支提取）。"""
        if node.type == NodeType.AGENT:
            return await self._execute_agent(node, state, run_config)
        elif node.type == NodeType.FUNCTION:
            return await self._execute_function(node, state, run_config)
        elif node.type == NodeType.SUBGRAPH:
            return await self._execute_subgraph(node, state, run_config)
        elif node.type == NodeType.ROUTER:
            return await self._execute_router(node, state, run_config)
        elif node.type == NodeType.FORK:
            return await self._execute_fork(node, state, run_config)
        elif node.type == NodeType.MERGE:
            return await self._execute_merge(node, state, run_config)
        elif node.type == NodeType.LOOP:
            return await self._execute_loop(node, state, run_config)
        elif node.type == NodeType.CHECKPOINT:
            return await self._execute_checkpoint(node, state, run_config)
        elif node.type == NodeType.HUMAN_IN_THE_LOOP:
            return await self._execute_human(node, state, run_config)
        elif node.type == NodeType.EVENT_EMITTER:
            return await self._execute_event_emitter(node, state, run_config)
        elif node.type == NodeType.QUALITY_GATE:
            return await self._execute_quality_gate(node, state, run_config)
        else:
            raise ValueError(f"不支持的节点类型: {node.type}")

    def _resolve_node_timeout(self, node: GraphNode) -> Optional[float]:
        """按节点类型解析超时（秒）。返回 None = 不额外包裹（该类型已有内部保护）。

        优先级：
          1. node.metadata.timeout_seconds 显式配置（最优先，按节点覆盖）
          2. 按节点类型默认值（环境变量可配）
        AGENT 已在 _execute_agent 内部经 resolve_agent_timeout 预算化保护，
        外层不再重复包裹，避免双层 wait_for 竞态覆盖 Agent 专用超时结果。
        """
        if node.type == NodeType.AGENT:
            return None
        # 人机节点语义特殊（暂停等用户输入），不设硬超时——恢复由用户 resume 驱动
        if node.type == NodeType.HUMAN_IN_THE_LOOP:
            return None
        explicit = node.metadata.get("timeout_seconds")
        if explicit:
            return float(explicit)
        type_defaults: Dict[Any, float] = {
            NodeType.QUALITY_GATE: float(_os.getenv("QUALITY_GATE_TIMEOUT_SECONDS", "300")),
            NodeType.ROUTER: float(_os.getenv("ROUTER_TIMEOUT_SECONDS", "60")),
            NodeType.SUBGRAPH: float(_os.getenv("SUBGRAPH_TIMEOUT_SECONDS", "600")),
            NodeType.FUNCTION: float(_os.getenv("FUNCTION_TIMEOUT_SECONDS", "120")),
            NodeType.CHECKPOINT: float(_os.getenv("CHECKPOINT_TIMEOUT_SECONDS", "60")),
            NodeType.HUMAN_IN_THE_LOOP: float(_os.getenv("HUMAN_TIMEOUT_SECONDS", "300")),
        }
        return type_defaults.get(node.type, 300.0)

    def _node_timeout_result(self, node: GraphNode, timeout: float) -> dict:
        """节点超时结果 — 全部节点显式失败（不把超时伪装成 passed）。

        遵循核心原则：宁可显式 FAILED，也不要把超时伪装成 passed。
        超时必发 node_timeout SSE 事件。
        """
        _logger.warning(
            "节点超时（%ss）: node=%s type=%s",
            timeout, node.id, node.type.value,
        )
        self._emit("node_timeout", {
            "run_id": self.run_id,
            "node_id": node.id,
            "node_type": node.type.value,
            "timeout_seconds": timeout,
        })
        if node.type == NodeType.QUALITY_GATE:
            # 质量门超时 → 显式不通过（status=timeout, passed=False），不阻塞流程但绝不伪装通过
            return {
                "quality_gate": True,
                "passed": False,
                "status": "timeout",
                "failure_code": "quality_gate_timeout",
                "timeout": True,
                "violations": [],
                "violations_count": 0,
                "report": {"timeout": True, "status": "timeout"},
            }
        return {
            "node_id": node.id,
            "status": "error",
            "error_kind": "timeout",
            "failure_code": f"{node.type.value}_timeout",
            "result": f"[节点执行超时（{timeout}s），已终止]",
            "raw": f"[timeout after {timeout}s]",
        }

    # ==================== 节点级工具触发 ====================

    async def _trigger_phase_tool(
        self,
        node: GraphNode,
        tool_id: str,
        params: dict,
        state: GraphState,
        run_config: GraphRunConfig,
        preset_id: str = "",
    ) -> None:
        """在节点入口确定性触发工具（phase_entry trigger）。

        将工具结果注入到 node metadata 供 agent 读取，或直接写入文件。
        若指定 preset_id，先加载预设参数再合并调用方参数。
        """
        from core.agent.tools import ToolExecutor
        executor = ToolExecutor(output_dir=run_config.project_id or "outputs")

        # 读取工具预设（如风格预设：真人/漫画/动漫）
        if preset_id:
            preset = executor.registry.get_preset(tool_id, preset_id)
            if preset:
                params = {**preset["params"], **params}  # 预设参数 + 调用方覆盖

        result = await executor.execute_named(tool_id, params)
        # 把结果存到节点 metadata，agent 可在 prompt 中引用
        node.metadata["tool_results"] = node.metadata.get("tool_results", [])
        node.metadata["tool_results"].append({
            "tool_id": tool_id,
            "params": params,
            "result": result,
        })
        self._emit("phase_tool_triggered", {
            "node_id": node.id,
            "tool_id": tool_id,
            "success": not str(result).startswith("[错误]"),
        })

    # ==================== Adaptive 路由评估 ====================

    async def _adaptive_evaluate(
        self,
        node: GraphNode,
        state: GraphState,
        config: GraphRunConfig,
        node_output: Any,
    ) -> Any:
        """adaptive 模式下评估 Agent 节点产出，决定 CONTINUE/RETRY/ASK_HUMAN/SKIP。

        - RETRY: 注入 feedback 后递归重新执行节点
        - ASK_HUMAN: 暂停执行等待人工输入，恢复后重新执行
        - SKIP: 标记节点为 skipped，返回原结果
        - CONTINUE: 正常返回

        每次决策发出 router_decision SSE 事件。
        """
        from core.run.adaptive_router import (
            AdaptiveRouter,
            QualityCheckResult,
            RouterDecision,
        )
        from core.run.events import RouterDecisionEvent

        router = self._get_adaptive_router(config)
        if router is None:
            return node_output

        # 构建质量检查结果（从 state 中读取）
        violations = state.values.get("quality_violations", [])
        passed = state.values.get("quality_passed", True)
        quality_result = QualityCheckResult(
            score=1.0 if passed else 0.5,
            violations=violations if isinstance(violations, list) else [],
            passed=passed,
        )

        # 计算阶段索引
        phase_index = len(state.completed_nodes)
        total_phases = len([n for n in self.graph.nodes if n.type == NodeType.AGENT])

        output_str = str(node_output) if node_output else ""

        router_result = await router.evaluate(
            node_id=node.id,
            node_name=node.label or node.id,
            node_output=output_str,
            phase_index=phase_index,
            total_phases=total_phases,
            quality_result=quality_result,
        )

        # 发出 router_decision SSE 事件
        event = RouterDecisionEvent(
            node_id=node.id,
            node_name=node.label or node.id,
            decision=router_result.decision.value,
            reasoning=router_result.reasoning,
            feedback=router_result.feedback,
            retry_count=router_result.retry_count,
            total_iterations=router.total_iterations,
        )
        self._emit("router_decision", event.model_dump())

        match_router_decision = router_result.decision

        if match_router_decision == RouterDecision.RETRY:
            # 注入 feedback 到 state，递归重新执行
            if router_result.feedback:
                state.set(
                    f"_adaptive_feedback:{node.id}",
                    router_result.feedback,
                )
            _logger.info(
                "Adaptive RETRY node=%s retry_count=%d feedback=%s",
                node.id, router_result.retry_count,
                (router_result.feedback or "")[:100],
            )
            # 递归重新执行节点
            return await self._adaptive_evaluate(
                node, state, config,
                await self._execute_agent(node, state, config),
            )

        elif match_router_decision == RouterDecision.ASK_HUMAN:
            # 暂停执行，等待人工输入
            state.mark_paused(
                f"Adaptive router requests human input: {router_result.reasoning}"
            )
            state.set("_pause_context", {
                "type": "adaptive_ask",
                "node_id": node.id,
                "question": router_result.reasoning,
                "phase_id": str(node.metadata.get("phase_id", "")),
            })
            self._emit("human_input_required", {
                "node_id": node.id,
                "question": router_result.reasoning,
                "reason": "adaptive_router",
            })
            # 返回带暂停标记的结果（Runtime 会检测 PAUSED 状态）
            return {
                "human_input_required": True,
                "question": router_result.reasoning,
                "node_id": node.id,
            }

        elif match_router_decision == RouterDecision.SKIP:
            _logger.info(
                "Adaptive SKIP node=%s reasoning=%s",
                node.id, router_result.reasoning,
            )
            if isinstance(node_output, dict):
                node_output["_skipped"] = True
            return node_output

        elif match_router_decision == RouterDecision.CONTINUE:
            pass  # 正常继续

        return node_output

    # ==================== 各类节点的执行逻辑 ====================

    async def _execute_agent(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """执行 Agent 节点（D2 RunContext / D4 知识 / D6 协作钩子）"""
        from core.agent.base import BaseAgent
        from core.agent.registry import get_registry
        from core.run.collaboration_hooks import on_phase_complete, on_phase_enter
        from core.run.knowledge_inject import attach_knowledge_to_config, resolve_knowledge_text
        from core.run.phase_spec import PhaseSpec

        agent_defn = await get_registry().get(node.agent_id)
        if not agent_defn:
            raise ValueError(f"Agent 未找到: {node.agent_id}")

        if node.agent_id not in self._agent_cache:
            agent_instance = BaseAgent(agent_defn)
            self._agent_cache[node.agent_id] = agent_instance
        else:
            agent_instance = self._agent_cache[node.agent_id]

        run_ctx = self._get_run_context(config)
        if not run_ctx:
            raise RuntimeError("Graph Agent 执行需要 RunContext（D2）")

        project_id = run_ctx.project_id or config.project_id or ""
        output_dir = f"outputs/{project_id}" if project_id else "outputs"
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        from core.communication.bus import ConversationBus

        phase_id = str(node.metadata.get("phase_id") or node.id)
        agent_config = dict(agent_instance.config or {})
        # S4: 合并 AgentDefinition.extra 中的运行时参数（如模板声明的 max_tool_iterations）
        # 白名单式合并，避免把 template_id 等溯源元数据灌进 config
        _def_extra = getattr(agent_defn, "extra", None) or {}
        if isinstance(_def_extra, dict) and _def_extra.get("max_tool_iterations"):
            agent_config.setdefault("max_tool_iterations", _def_extra["max_tool_iterations"])
        extra = run_ctx.config_snapshot.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        use_tools = extra.get("use_tools", True)

        spec: Optional[PhaseSpec] = None
        if phase_id:
            spec = self._resolve_phase_spec(config, phase_id)
            if spec:
                run_ctx.current_phase_id = phase_id
                await on_phase_enter(
                    run_ctx,
                    spec,
                    node.agent_id,
                    agent_name=getattr(agent_instance, "name", node.agent_id),
                )
                knowledge_text = await resolve_knowledge_text(spec, run_ctx, node.agent_id)
                agent_config = attach_knowledge_to_config(agent_config, knowledge_text)

        agent_instance.plug(
            ConversationBus(project_id),
            output_dir=output_dir,
            config={**agent_config, "use_tools": use_tools},
        )

        shared_thread_exchanges: list = []
        if spec and spec.dialogue_policy == "shared_thread":
            from core.run.shared_thread import find_pending_handoff, run_shared_thread_dialogue

            pending = find_pending_handoff(run_ctx, node.agent_id)
            if pending and pending.from_agent:
                sender_id = pending.from_agent
                sender_instance = self._agent_cache.get(sender_id)
                if not sender_instance:
                    sender_defn = await get_registry().get(sender_id)
                    if sender_defn:
                        sender_instance = BaseAgent(sender_defn)
                shared_thread_exchanges = await run_shared_thread_dialogue(
                    run_ctx,
                    spec,
                    node.agent_id,
                    sender_id,
                    agent_instance.bus,
                    receiver_name=getattr(agent_instance, "name", node.agent_id),
                    sender_name=getattr(sender_instance, "name", sender_id) if sender_instance else sender_id,
                    handoff_summary=pending.summary,
                    receiver_agent=agent_instance,
                    sender_agent=sender_instance,
                    handoff_entry=pending,
                )
                log = run_ctx.config_snapshot.setdefault("shared_thread_log", [])
                if isinstance(log, list):
                    log.extend(shared_thread_exchanges)

        input_data = self._build_agent_input(node, state, run_ctx, agent_config)
        if shared_thread_exchanges:
            input_data["shared_thread"] = shared_thread_exchanges
        try:
            import asyncio as _asyncio
            # 预算化超时：轮数×单轮预算 + 终稿 + 保底（resolve_agent_timeout），
            # node.metadata.timeout_seconds 显式配置最优先
            _AGENT_TIMEOUT = int(resolve_agent_timeout(agent_config, node))
            # 空产出重试（2026-08-09）：reasoning 模型可能预算吃光产出为空
            # （writer 实测多次空产出 → FAILED → run partial）。最多 AGENT_EMPTY_RETRIES 次。
            _EMPTY_RETRIES = int(_os.getenv("AGENT_EMPTY_RETRIES", "2"))
            result = None
            for _attempt in range(_EMPTY_RETRIES + 1):
                try:
                    result = await _asyncio.wait_for(
                        agent_instance.execute(input_data),
                        timeout=_AGENT_TIMEOUT,
                    )
                except _asyncio.TimeoutError:
                    _logger.warning("Agent 超时（%ds）: agent=%s node=%s", _AGENT_TIMEOUT, node.agent_id, node.id)
                    self._emit("agent_timeout", {
                        "run_id": self.run_id,
                        "node_id": node.id,
                        "agent_id": node.agent_id,
                        "timeout_seconds": _AGENT_TIMEOUT,
                    })
                    result = {
                        "agent_id": node.agent_id,
                        "role": getattr(agent_instance, "role", ""),
                        "status": "error",            # 触发 skip_outputs，不污染下游 agent 上下文
                        "error_kind": "timeout",
                        "result": f"[Agent 执行超时（{_AGENT_TIMEOUT}s），已终止]",
                        "raw": f"[timeout after {_AGENT_TIMEOUT}s]",
                    }
                    break
                # 空产出 → 重试（reasoning 预算吃光 / 模型偶发返回空）
                if not agent_result_has_content(result):
                    _logger.warning(
                        "Agent %s 第 %d 次产出为空，重试: node=%s phase=%s",
                        node.agent_id, _attempt + 1, node.id, phase_id,
                    )
                    if _attempt < _EMPTY_RETRIES:
                        run_ctx.add_observation(
                            node.agent_id,
                            f"[平台重试] Agent 产出为空（attempt {_attempt + 1}），重新执行",
                            kind="system",
                        )
                        continue
                break
        except Exception as e:
            import traceback
            _logger.error(
                "Agent 执行异常: agent=%s node=%s error=%s\ntraceback:\n%s",
                node.agent_id, node.id, e,
                traceback.format_exc(),
            )
            # 节点级容错：默认 fail_fast（保持旧行为 raise），配置关则降级为 error 结果继续
            fail_fast = bool(node.metadata.get("fail_fast", True))
            if fail_fast:
                raise
            # result 只放通用消息（完整异常已记服务端日志，不外泄内部细节）
            result = {
                "agent_id": node.agent_id,
                "role": getattr(agent_instance, "role", ""),
                "status": "error",
                "error_kind": "exception",
                "result": "[Agent 执行异常，已终止；详情见服务端日志]",
                "raw": "",
            }

        payload = result if isinstance(result, dict) else {"result": result}
        # LLM 错误不得写入 agent_outputs，避免污染下游 Agent 上下文
        skip_outputs = isinstance(result, dict) and result.get("status") == "error"
        if not skip_outputs:
            # #6 并行节点写保护：通过 async 版加锁防竞态
            if hasattr(run_ctx, "set_agent_result_async"):
                await run_ctx.set_agent_result_async(
                    node.agent_id,
                    payload,
                    phase_id=phase_id,
                    agent_name=getattr(agent_instance, "name", node.agent_id),
                )
            else:
                run_ctx.set_agent_result(
                    node.agent_id,
                    payload,
                    phase_id=phase_id,
                    agent_name=getattr(agent_instance, "name", node.agent_id),
                )

        # 兜底：每个 agent 的产出自动写入工作区文件（不依赖 agent 主动调 file_write）
        # 跳过 LLM 错误返回，避免污染工作区
        if not skip_outputs:
            await self._persist_agent_output(run_ctx.project_id, phase_id, node.agent_id, payload)

        # 产物落盘强制检查（P0-3 三类状态）：agent 完成但无任何落盘产物 → 节点 FAILED。
        # 不静默放行——宁可显式 FAILED，也不要 run 看起来 COMPLETED 但产出残缺。
        # PRESENT=正常；MISSING=确实无产物；CHECK_FAILED=查询异常（≠MISSING，需治理）。
        if not skip_outputs:
            try:
                from core.vault.project_artifact_service import (
                    ProjectArtifactService, ArtifactCheckResult,
                )
                check = await ProjectArtifactService.check_phase_artifact(
                    run_ctx.project_id, node.agent_id, phase_id,
                    run_id=getattr(run_ctx, "run_id", ""),
                )

                if check == ArtifactCheckResult.PRESENT:
                    pass  # 正常
                elif check == ArtifactCheckResult.MISSING:
                    _logger.warning(
                        "Agent 完成但无落盘产物 → 节点标 FAILED: agent=%s phase=%s",
                        node.agent_id, phase_id,
                    )
                    run_ctx.record_degradation(
                        "critical", "agent_output",
                        f"Agent {node.agent_id} 完成但未产出任何落盘文件 (phase={phase_id})",
                        "节点标记 FAILED，不静默放行",
                        phase_id=phase_id, node_id=node.id,
                        code="agent_no_output", fallback_used="node_failed",
                        source="runtime._execute_agent",
                    )
                    if isinstance(result, dict):
                        result["status"] = "error"
                        result["error_kind"] = "no_output"
                        result["result"] = (
                            "[Agent 完成但未产出任何落盘文件，节点已标记 FAILED]"
                        )
                    else:
                        result = {
                            "agent_id": node.agent_id,
                            "status": "error",
                            "error_kind": "no_output",
                            "result": "[Agent 完成但未产出任何落盘文件，节点已标记 FAILED]",
                        }
                    payload = result if isinstance(result, dict) else {"result": result}
                    skip_outputs = True
                elif check == ArtifactCheckResult.CHECK_FAILED:
                    _logger.error(
                        "产物检查查询异常 → 节点标 FAILED: agent=%s phase=%s",
                        node.agent_id, phase_id,
                    )
                    run_ctx.record_degradation(
                        "critical", "artifact_check",
                        f"产物检查查询异常 (agent={node.agent_id} phase={phase_id})",
                        "不能确认产物归属，节点 FAILED，不放行",
                        phase_id=phase_id, node_id=node.id,
                        code="artifact_check_failed", fallback_used="node_failed",
                        source="runtime._execute_agent",
                    )
                    if isinstance(result, dict):
                        result["status"] = "error"
                        result["error_kind"] = "artifact_check_failed"
                        result["result"] = (
                            "[产物检查查询异常，节点已标记 FAILED]"
                        )
                    else:
                        result = {
                            "agent_id": node.agent_id,
                            "status": "error",
                            "error_kind": "artifact_check_failed",
                            "result": "[产物检查查询异常，节点已标记 FAILED]",
                        }
                    payload = result if isinstance(result, dict) else {"result": result}
                    skip_outputs = True
            except Exception as e:
                # 真实异常（如 import 失败）→ 节点 FAILED 而不是静默
                _logger.exception("产物检查链路异常: %s", e)
                run_ctx.record_degradation(
                    "critical", "artifact_check",
                    f"产物检查链路异常: {e}",
                    "节点 FAILED，不放行",
                )
                if isinstance(result, dict):
                    result["status"] = "error"
                    result["error_kind"] = "artifact_check_error"
                else:
                    result = {
                        "agent_id": node.agent_id,
                        "status": "error",
                        "error_kind": "artifact_check_error",
                        "result": "[产物检查链路异常，节点已标记 FAILED]",
                    }
                payload = result if isinstance(result, dict) else {"result": result}
                skip_outputs = True

        if spec:
            # LLM 错误返回不生成 handoff（避免污染下游 Agent）
            if not skip_outputs:
                nxt = self._find_next_agent_node(node.id)
                await on_phase_complete(
                    run_ctx,
                    spec,
                    node.agent_id,
                    payload,
                    to_agent=nxt.agent_id if nxt else "",
                    to_name=(nxt.label or nxt.agent_id) if nxt else "",
                    from_name=getattr(agent_instance, "name", node.agent_id),
                )

        config.run_context = run_ctx
        return result

    async def _persist_agent_output(
        self, project_id: str, phase_id: str, agent_id: str, payload: dict
    ) -> Optional[Path]:
        """把每个 agent 的产出自动写入工作区文件（兜底机制）

        不依赖 agent 主动调 file_write——每个 phase 完成后自动落盘。
        文件命名: outputs/<project_id>/<phase_id>.md
        """
        try:
            from core.agent.tools import ToolRegistry

            out_dir = ToolRegistry._normalize_output_dir(project_id)
            base = ToolRegistry.OUTPUTS_DIR / out_dir if out_dir else ToolRegistry.OUTPUTS_DIR
            base.mkdir(parents=True, exist_ok=True)

            # 提取可文本化的产出内容
            content = payload.get("result") if isinstance(payload, dict) else payload
            if isinstance(content, dict):
                # 嵌套 result 或 text 字段
                content = content.get("result") or content.get("text") or content.get("content") or ""
            content_str = str(content).strip() if content else ""
            if not content_str:
                _logger.debug("跳过空产出: phase=%s payload_keys=%s", phase_id, list(payload.keys()) if isinstance(payload, dict) else type(payload))
                return None

            # 提炼干净交付正文（工作台通用）：剥离 agent 思考/独白前缀、工具调用标记、
            # 设定承接/自我评价与对话尾巴，只保留最终交付内容（如小说正文）。
            # 规则快速路径优先；仍检测到污染则升级 LLM 精提炼（flash，限时），失败兜底规则结果。
            from core.communication.content_format import (
                refine_agent_deliverable, refine_with_llm, needs_llm_refine,
            )
            refined = refine_agent_deliverable(content_str, f"{phase_id}.md")
            if needs_llm_refine(refined):
                try:
                    import asyncio as _asyncio
                    from tools.llm_client import LLMClient
                    _llm = LLMClient({"model": "deepseek-v4-flash", "temperature": 0.0, "max_tokens": 8000})
                    llm_refined = await _asyncio.wait_for(
                        refine_with_llm(content_str, f"{phase_id}.md", _llm), timeout=90
                    )
                    if llm_refined.strip():
                        refined = llm_refined
                except Exception:
                    pass  # LLM 提炼失败/超时：用规则结果兜底
            if refined.strip():
                content_str = refined

            # 避免重复写入（如果 agent 自己已经通过 file_write 写了同 phase 文件，跳过）
            out_path = base / f"{phase_id}.md"
            header = f"# {phase_id}\n\n"
            if "agent_id" in payload:
                header += f"**Agent**: {payload['agent_id']}\n\n"
            if "agent_name" in payload:
                header += f"**名称**: {payload['agent_name']}\n\n"
            header += f"---\n\n"

            out_path.write_text(header + content_str, encoding="utf-8")
            _logger.info("Agent 产出已落盘: %s (%d bytes)", out_path, out_path.stat().st_size)

            # 同步写入 artifacts 表，让前端产物面板能展示
            try:
                from core.storage.database import get_db
                db = await get_db()
                rel_path = f"{project_id}/{phase_id}.md"
                # 补 id（此前漏 id 列导致 98% 产物 id NULL，前端审批/查看/版本失效）
                art_id = f"art_{phase_id}_{uuid.uuid4().hex[:8]}"
                await db.execute(
                    """INSERT INTO artifacts (id, project_id, agent_id, type, name, file_path, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        art_id,
                        project_id,
                        agent_id,
                        "document",
                        f"{phase_id}",
                        rel_path,
                        "{}",
                    ),
                )
                await db.commit()
            except Exception:
                pass  # 表写入失败不影响主流程

            return out_path
        except Exception as e:
            _logger.warning("Agent 产出落盘失败: %s: %s", phase_id, e)
            # 2026-08-09 P1：产物丢失不静默，记录 CRITICAL 降级
            if self._live_run_ctx and hasattr(self._live_run_ctx, "record_degradation"):
                try:
                    self._live_run_ctx.record_degradation(
                        "critical", "agent_output",
                        f"Agent {agent_id} 产出落盘失败 (phase={phase_id}): {e}",
                        "产物丢失，下游可能读不到该产出",
                    )
                except Exception:
                    pass
            return None

    async def _execute_function(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """执行 Function 节点"""
        func = self.registry.get(node.func_ref)
        if not func:
            raise ValueError(f"函数未注册: {node.func_ref}")

        if asyncio.iscoroutinefunction(func):
            result = await func(state)
        else:
            result = func(state)

        return result

    async def _execute_subgraph(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """递归执行 Subgraph 节点"""
        subgraph_def = node.metadata.get('_subgraph_def')
        if not subgraph_def:
            raise ValueError(f"Subgraph 定义缺失: {node.subgraph_id}")

        subgraph = WorkflowGraph.from_dict(subgraph_def)

        # Input mapping: 从父状态映射到子状态
        sub_state = GraphState(graph_id=subgraph.graph_id)
        for src_key, dst_key in node.input_mapping.items():
            value = state.get(src_key)
            if value is not None:
                sub_state.set(dst_key, value)

        # 递归执行子图
        runtime = GraphRuntime(subgraph)
        sub_result = await runtime.execute(
            initial_state=sub_state.snapshot(),
        )

        # Output mapping: 子状态映射回父状态
        for src_key, dst_key in node.output_mapping.items():
            value = sub_result.state.get(src_key)
            if value is not None:
                state.set(dst_key, value)

        return {"subgraph_result": dict(sub_result.state.values)}

    async def _execute_router(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """执行 Router 节点（选择一个 route_key 写入状态）"""
        # Router 的核心工作是由 Runtime 中的 Router 组件完成的
        # 这里只是做一个标记，实际路由决策在 resolve() 中
        routes = node.routes

        # 如果 router_type 是 HANDLER_FN，尝试调用
        if node.router_type == ConditionType.HANDLER_FN and node.id in self.registry._handlers:
            handler = self.registry.get(node.id)
            if handler:
                if asyncio.iscoroutinefunction(handler):
                    chosen = await handler(state)
                else:
                    chosen = handler(state)
                state.last_router_output = str(chosen)
                state.routing_context['_router_choice'] = chosen
                return {"route": chosen}

        # 默认：返回可用路由列表供外部 Router 决策
        return {
            "available_routes": list(routes.keys()),
            "router_node_id": node.id,
        }

    async def _execute_fork(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """Fork 节点（启动信号 + D6 broadcast）"""
        run_ctx = self._get_run_context(config)
        if run_ctx and node.fork_targets:
            from core.run.collaboration_hooks import on_parallel_fork

            await on_parallel_fork(
                run_ctx,
                fork_node_id=node.id,
                target_node_ids=list(node.fork_targets),
                graph=self.graph,
                reason="fork",
            )
            config.run_context = run_ctx

        return {
            "fork": True,
            "targets": node.fork_targets,
        }

    async def _execute_merge(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """Merge 节点（汇聚多路输出）"""
        sources = node.merge_sources
        strategy = node.merge_strategy

        outputs = []
        for src_id in sources:
            output = state.get_node_output(src_id)
            if output:
                outputs.append((src_id, output))

        best = None
        if strategy == MergeStrategy.ALL:
            result = {sid: o.output for sid, o in outputs}
        elif strategy == MergeStrategy.FIRST:
            result = outputs[0][1].output if outputs else None
        elif strategy == MergeStrategy.LAST:
            result = outputs[-1][1].output if outputs else None
        elif strategy == MergeStrategy.CONCAT:
            result = [o.output for _, o in outputs]
        elif strategy == MergeStrategy.DICT_MERGE:
            merged = {}
            for _, o in outputs:
                if isinstance(o.output, dict):
                    merged.update(o.output)
            result = merged
        elif strategy == MergeStrategy.WINNER_TAKES_ALL:
            best = max(outputs, key=lambda x: x[1].output.get('_score', 0)) if outputs else None
            result = best[1].output if best else None
        else:
            result = {sid: o.output for sid, o in outputs}

        run_ctx = self._get_run_context(config)
        if run_ctx and strategy == MergeStrategy.WINNER_TAKES_ALL:
            from core.run.collaboration_hooks import on_graph_status

            winner = best[0] if best else ""
            await on_graph_status(
                run_ctx,
                node_id=node.id,
                summary=f"并行竞争合并完成，候选: {', '.join(sources)}"
                + (f"，胜出: {winner}" if winner else ""),
                phase_id=str(node.metadata.get("phase_id") or ""),
            )
            config.run_context = run_ctx

            # --- 修正9：emit competition_complete SSE ---
            try:
                from core.events import broadcast_event
                variants = []
                for i, (sid, o) in enumerate(outputs):
                    out_dict = o.output if isinstance(o.output, dict) else {}
                    sc = float(out_dict.get('_score', 0))
                    variants.append({
                        "id": f"variant_{i}",
                        "strategy": "",
                        "preview": str(out_dict.get('content', '') or out_dict.get('result', ''))[:500],
                        "scores": {"creativity": sc * 0.25, "pacing": sc * 0.25, "emotional_impact": sc * 0.25, "consistency": sc * 0.25, "overall": sc / 10.0 if sc > 1 else sc},
                        "is_winner": (sid == winner),
                    })
                await broadcast_event(run_ctx.project_id, "competition_complete", {
                    "unit_number": getattr(run_ctx, 'current_unit', 0) or 0,
                    "variants": variants,
                    "winner_reason": f"综合评分最高" if winner else "",
                })
            except Exception as e:
                _logger.warning("竞争完成事件广播失败（客户端可能收不到）: %s", e)

        return result

    async def _execute_loop(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """Loop 节点控制"""
        current_count = state.increment_loop(node.id, node.max_iterations)
        return {
            "loop_iteration": current_count,
            "max_iterations": node.max_iterations,
            "should_continue": current_count < node.max_iterations,
        }

    async def _execute_checkpoint(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """Checkpoint 节点（触发持久化）。如果节点配置了 quality_check_target，
        则先执行质量检查再持久化。"""
        result: Dict[str, Any] = {
            "checkpoint": True,
            "state_snapshot": state.snapshot(),
        }

        # 如果 Checkpoint 节点配置了关联的 Agent 节点进行质量检查
        quality_target = node.metadata.get("quality_check_target")
        if quality_target:
            violations = await self._run_quality_check(quality_target, state, config=config)
            result["quality_check"] = {
                "target": quality_target,
                "violations_count": len(violations),
                "passed": all(
                    v["severity"] != "error" for v in violations
                ),
                "violations": violations,
            }
            # 将质量结果写入状态供路由使用
            state.set("quality_violations", violations)
            state.set("quality_passed", result["quality_check"]["passed"])

        return result

    def _emit_quality_check(
        self,
        node: GraphNode,
        phase_id: str,
        agent_id: str,
        passed: bool,
        violations_count: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """P1-9: 统一 quality_check SSE 发射 —— 主（DbQualityGateway）与回退两路径共用。

        修复前 db_quality_gateway 内部 + 两路径各 emit，同一质量门禁 SSE 双发。
        现在 graph 层是唯一发射点，payload 结构收敛到此处。
        """
        payload: Dict[str, Any] = {
            "phase": phase_id or agent_id,
            "phase_id": phase_id,
            "phase_label": str(node.label or phase_id or node.id),
            "agent_id": agent_id,
            "passed": passed,
            "violations_count": violations_count,
            "message": f"质量门禁{'通过' if passed else '未通过'}（{violations_count} 项）",
            "severity": "info" if passed else "error",
        }
        if extra:
            payload.update(extra)
        self._emit("quality_check", payload)

    async def _execute_quality_gate(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """质量门禁节点：对指定 Agent 节点的产出执行质量检查

        优先使用 DbQualityGateway（从 DB 加载规则），无 domain_id 时 fallback 到原逻辑。
        """
        # 从 metadata 获取要检查的节点 ID
        check_targets: List[str] = (
            node.metadata.get("quality_check_targets", [])
            or [n for n in state.completed_nodes
                if self.graph.get_node(n)
                and self.graph.get_node(n).type == NodeType.AGENT]  # type: ignore[arg-type]
        )

        phase_id = str(node.metadata.get("phase_id") or "")
        agent_id = check_targets[0] if check_targets else ""
        project_id = config.project_id or state.values.get("project_id", "")

        # 尝试从 run_context 或 node metadata 获取 domain_id
        domain_id = ""
        run_ctx = self._get_run_context(config)
        if run_ctx:
            domain_id = str(run_ctx.config_snapshot.get("domain_id") or "")
        if not domain_id:
            domain_id = str(node.metadata.get("domain_id") or "")

        # ====== 新路径：DbQualityGateway ======
        if domain_id and check_targets:
            from core.gateway.db_quality_gateway import DbQualityGateway

            target = check_targets[0]
            node_result = state.get_node_output(target)
            agent_output = node_result.output if node_result else None

            strictness = "standard"
            if run_ctx:
                strictness = str(run_ctx.config_snapshot.get("strictness", "standard"))

            # 质量规则加载的 phase：run phase（unit_N/agent_id）与领域 phase（draft）不匹配
            # → 用领域 phase_definitions 映射；run phase 在领域里则用它，否则空串（领域全部规则）。
            # 此前 phase_id=unit_1 匹配不到 draft.chapter_length 等规则 → 领域规则从不生效。
            _rule_phase_id = phase_id
            try:
                from core.run.knowledge_inject import _domain_phase_ids
                if domain_id and phase_id:
                    _dphs = await _domain_phase_ids(domain_id)
                    if phase_id not in _dphs:
                        _rule_phase_id = ""
            except Exception:
                pass

            report = await DbQualityGateway.check(
                project_id=project_id,
                domain_id=domain_id,
                phase_id=_rule_phase_id,
                agent_id=target,
                agent_output=agent_output,
                strictness=strictness,
                run_context=run_ctx.model_dump() if run_ctx else None,
            )

            passed = report.overall_passed
            all_violations = [r.__dict__ if hasattr(r, '__dict__') else r for r in report.violations]
            # 转为兼容格式
            all_violations = [
                {
                    "rule_id": v.get("rule_id", ""),
                    "severity": v.get("severity", "warning"),
                    "message": v.get("message", ""),
                    "fix_hint": v.get("fix_hint", ""),
                }
                for v in all_violations
            ]

            # P0-3: 协商约束覆盖校验接入主质量路径
            # 修复前 evaluate_constraints_async 只在无 domain_id 的回退路径执行，
            # 带 domain_id 的主流（DbQualityGateway）完全旁路 constraints_coverage。
            if run_ctx and run_ctx.negotiation_ledger:
                try:
                    from core.run.negotiation import evaluate_constraints_async

                    all_violations.extend(await evaluate_constraints_async(
                        run_ctx, phase_id, target, str(agent_output or ""),
                    ))
                except Exception as _ce:
                    # 2026-08-09 P1：协商约束覆盖校验失败不静默（协商闭环断裂）
                    _logger.warning("约束覆盖校验异常: %s", _ce)
                    run_ctx.record_degradation(
                        "critical", "constraint_coverage",
                        f"协商约束覆盖校验失败: {_ce}",
                        "约束闭环断裂，违规约束可能漏检",
                    )

            # constraints 违规并入后重算通过态（severity=error 视为不通过，与回退路径语义一致）
            passed = report.overall_passed and not any(
                v.get("severity") == "error" for v in all_violations
            )

            state.set("quality_violations", all_violations)
            state.set("quality_passed", passed)
            state.set("quality_report", report.to_dict())

            if run_ctx:
                run_ctx.add_quality_record(phase_id, target, all_violations)
                from core.run.collaboration_hooks import on_graph_status

                verdict = "通过" if passed else "未通过"
                await on_graph_status(
                    run_ctx,
                    node_id=node.id,
                    summary=f"质量门禁 {verdict}（{len(all_violations)} 项）",
                    phase_id=phase_id,
                    agent_id=target or "quality_gate",
                )

                # 质量通过 → 记录成功案例到 EvolutionEngine（进化数据积累）
                if passed and target:
                    try:
                        from core.evolution import EvolutionEngine
                        evo = EvolutionEngine()
                        evo.set_project(project_id)
                        score = max(1.0, 10.0 - len(all_violations) * 2.0)
                        await evo.log_success(
                            agent_id=target,
                            result=agent_output or {},
                            quality_score=score,
                            project_id=project_id,
                            domain_id=domain_id,
                            duration_seconds=int(state.values.get("phase_duration", 0) or 0),
                        )
                    except Exception:
                        pass  # 进化日志非致命

                # 教练模式：检查未通过时自动修复
                if not passed and agent_output and isinstance(agent_output, str):
                    # 多章节批量场景下跳过 autofix（2026-08-09 P0）：每个章节 agent.execute
                    # ~90s，N 章 × 多轮 autofix 会让质量门严重超时（曾实测 372s）。改为
                    # 直接以未通过态返回，由下游 LOOP_BACK/调度决定重试，避免拖死整条 run。
                    _is_multi_chapter = bool(
                        run_ctx and (getattr(run_ctx, "total_units", 0) or 0) > 1
                    )
                    if _is_multi_chapter:
                        _logger.warning(
                            "多章节场景跳过 autofix（total_units=%s）: node=%s phase=%s",
                            getattr(run_ctx, "total_units", 0), node.id, phase_id,
                        )
                        run_ctx.record_degradation(
                            severity="warning",
                            component="quality_gate",
                            message=f"多章节场景（{run_ctx.total_units} 章）跳过 autofix，质量未通过直接返回",
                            recovery_action="由调度层 LOOP_BACK 重试或标记未通过",
                        )
                    else:
                        await self._run_quality_autofix(
                            run_ctx, target, agent_output,
                            node, phase_id, project_id, state,
                        )

                # 质量未通过 → 记录失败案例（供 SelfOptimizer 分析失败模式）
                if not passed and target:
                    try:
                        from core.evolution import EvolutionEngine
                        evo = EvolutionEngine()
                        evo.set_project(project_id)
                        violation_msgs = [
                            v.get("message", str(v)) if isinstance(v, dict) else str(v)
                            for v in all_violations[:3]
                        ]
                        await evo.log_failure(
                            agent_id=target,
                            phase_id=phase_id or "",
                            reason="; ".join(violation_msgs) if violation_msgs else "质量门禁未通过",
                        )
                    except Exception:
                        pass  # 进化日志非致命

                config.run_context = run_ctx

            self._emit_quality_check(
                node, phase_id, target, passed, len(all_violations),
                extra={"domain_id": domain_id, "report": report.to_dict()},
            )

            return {
                "quality_gate": True,
                "targets_checked": check_targets,
                "violations_count": len(all_violations),
                "passed": passed,
                "violations": all_violations,
                "report": report.to_dict(),
            }

        # ====== Fallback：原有 DomainAdapter 逻辑 ======
        all_violations: List[dict] = []
        quality_profile = node.metadata.get("quality_profile") or {}
        for target in check_targets:
            violations = await self._run_quality_check(target, state, quality_profile, config=config)
            all_violations.extend(violations)

        passed = all(v["severity"] != "error" for v in all_violations)
        state.set("quality_violations", all_violations)
        state.set("quality_passed", passed)

        if run_ctx:
            run_ctx.add_quality_record(phase_id, agent_id, all_violations)
            from core.run.collaboration_hooks import on_graph_status

            verdict = "通过" if passed else "未通过"
            await on_graph_status(
                run_ctx,
                node_id=node.id,
                summary=f"质量门禁 {verdict}（{len(all_violations)} 项）",
                phase_id=phase_id,
                agent_id=agent_id or "quality_gate",
            )
            config.run_context = run_ctx

        self._emit_quality_check(node, phase_id, agent_id, passed, len(all_violations))

        return {
            "quality_gate": True,
            "targets_checked": check_targets,
            "violations_count": len(all_violations),
            "passed": passed,
            "violations": all_violations,
        }

    async def _run_quality_autofix(
        self,
        run_ctx,
        target: str,
        agent_output: Any,
        node: GraphNode,
        phase_id: str,
        project_id: str,
        state: GraphState,
    ) -> None:
        """质量门未通过时，用同 agent 临时实例执行自动修复（coach 模式）。

        注意：此处的 coach_agent 是临时 BaseAgent 实例，不走 _execute_agent，
        预算化超时对它不生效 → 超时保护在 quality_gate_with_autofix 内部完成（wait_for）。
        异常非致命：不阻断质量门返回。
        """
        try:
            from core.gateway.coach import quality_gate_with_autofix
            from core.agent.base import BaseAgent
            from core.agent.registry import get_registry

            registry = get_registry()
            defn = await registry.get(target)
            if defn:
                coach_agent = BaseAgent(defn)
                coach_agent.plug(
                    bus=getattr(run_ctx, "bus", None),
                    output_dir=f"outputs/{project_id}",
                    config={"use_tools": False},
                )
                coach_result = await quality_gate_with_autofix(
                    agent=coach_agent,
                    output=agent_output,
                    quality_config=node.metadata.get("quality_rules", {}),
                    run_context=run_ctx,
                    project_id=project_id,
                )
                _logger.info(
                    "QualityGate autofix: %d attempts, passed=%s, output=%d chars, failure=%s",
                    coach_result.attempts, coach_result.passed,
                    len(coach_result.output or ""), coach_result.failure_code,
                )
                # P0-2：autofix 通过才把目标节点标 COMPLETED；失败则保持原产出，
                # 由质量门 passed=False 驱动路由（重试 / 不通过）。
                if coach_result.passed:
                    if coach_result.output and coach_result.output != agent_output:
                        from core.graph.types import ExecutionResult as _ER, NodeStatus as _NS
                        state.set_node_output(
                            target,
                            _ER(
                                node_id=target,
                                status=_NS.COMPLETED,
                                output=coach_result.output,
                            ),
                        )
                elif coach_result.failure_code:
                    _logger.warning(
                        "QualityGate autofix 失败（%s）: agent=%s phase=%s，节点不放行",
                        coach_result.failure_code, target, phase_id,
                    )
        except Exception as e:
            _logger.warning("QualityGate autofix 异常: %s", e, exc_info=True)
            # 2026-08-09 P1：质量未校验放行不静默
            if run_ctx and hasattr(run_ctx, "record_degradation"):
                try:
                    run_ctx.record_degradation(
                        "warning", "quality_gate",
                        f"QualityGate autofix 异常: {e}",
                        "质量未校验放行",
                    )
                except Exception:
                    pass

    async def _run_quality_check(
        self,
        agent_id: str,
        state: GraphState,
        quality_profile: Optional[Dict[str, Any]] = None,
        *,
        config: Optional[GraphRunConfig] = None,
    ) -> List[dict]:
        """对指定 Agent 产出执行 QualityGateway 检查 (D5 — QualityProfile)"""
        try:
            from core.orchestration.phase_config import build_quality_gateway_for_profile

            gateway = build_quality_gateway_for_profile(quality_profile)
        except Exception as e:
            from core.logging import get_logger
            get_logger("graph.runtime").error(
                "质量门构造失败 agent=%s profile=%s: %s", agent_id, quality_profile, e
            )
            # fail-closed：构造失败视为一条 error 级违规，不静默放行控制点
            return [{
                "rule_id": "quality_gateway_build_error",
                "severity": "error",
                "message": f"质量门构造失败，已按未通过处理：{e}",
                "agent_id": agent_id,
            }]

        node_result = state.get_node_output(agent_id)
        if not node_result or not node_result.output:
            return []

        run_ctx = self._get_run_context(config) if config else None
        project_id = (run_ctx.project_id if run_ctx else "") or (config.project_id if config else "")
        phase_id = str((quality_profile or {}).get("phase_id") or agent_id)

        from core.gateway.rules import CheckInput

        check_data = CheckInput(
            agent_id=agent_id,
            result=node_result.output,
            text_content=str(node_result.output),
            metadata={
                "project_id": project_id,
                "phase_id": phase_id,
            },
        )
        violations = gateway.evaluate(agent_id, check_data)

        # 协商约束覆盖校验（D6 闭环：质疑 → 约束 → 语义级门禁可检查）
        if run_ctx and run_ctx.negotiation_ledger:
            try:
                from core.run.negotiation import evaluate_constraints_async

                violations.extend(await evaluate_constraints_async(
                    run_ctx, phase_id, agent_id, str(node_result.output),
                ))
            except Exception as e:
                _logger.warning("约束语义校验降级失败（该阶段约束可能未校验）: %s", e)

        return violations

    async def _execute_human(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """人机协作节点：暂停执行，等待用户 Review/Decision

        根据 metadata 中的 type 字段区分：
        - "review": 人工审核（对应 POST /api/review/resume）
        - "decision": 选择决策（对应 POST /api/decisions/{id}/resolve）
        - 默认: 通用暂停，通过 resume(user_input) 恢复
        """
        interaction_type = node.metadata.get("type", "review")
        question = node.metadata.get("question", "")
        options = node.metadata.get("options", [])
        phase_label = node.label or node.id

        state.mark_paused(f"等待用户输入 [{interaction_type}]: {question}")

        # 根据类型发射不同 SSE 事件
        if interaction_type == "review":
            result = {
                "human_input_required": True,
                "type": "review",
                "question": question or f"请审核阶段产出：{phase_label}",
                "phase_label": phase_label,
                "node_id": node.id,
            }
            self._emit("review_required", {
                "node_id": node.id,
                "question": result["question"],
                "phase_label": phase_label,
            })
        elif interaction_type == "decision":
            result = {
                "human_input_required": True,
                "type": "decision",
                "question": question,
                "options": options,
                "node_id": node.id,
            }
            self._emit("decision_required", {
                "node_id": node.id,
                "question": question,
                "options": options,
            })
        else:
            result = {
                "human_input_required": True,
                "question": question,
                "options": options,
                "node_id": node.id,
            }
            self._emit("human_input_required", {
                "node_id": node.id,
                "question": question,
                "options": options,
            })

        # 存储暂停上下文，供 resume() 使用
        pause_ctx = {
            "type": interaction_type,
            "node_id": node.id,
            "question": question,
            "options": options,
            "phase_id": node.metadata.get("phase_id", ""),
        }
        state.set("_pause_context", pause_ctx)

        run_ctx = self._get_run_context(config)
        if run_ctx:
            run_ctx.control.pending_human = True
            run_ctx.control.pause_reason = interaction_type
            from core.run.collaboration_hooks import on_graph_status

            await on_graph_status(
                run_ctx,
                node_id=node.id,
                summary=f"等待人工{interaction_type}: {question or phase_label}",
                phase_id=str(node.metadata.get("phase_id") or ""),
            )
            if interaction_type == "decision":
                from core.communication.decision_service import persist_pending_decision

                decision_id = await persist_pending_decision(
                    run_ctx.project_id,
                    question=question or phase_label,
                    options=options or [],
                    phase=str(node.metadata.get("phase_id") or phase_label),
                )
                run_ctx.control.pending_decision_id = decision_id
            config.run_context = run_ctx

        return result

    async def _execute_event_emitter(
        self, node: GraphNode, state: GraphState, config: GraphRunConfig
    ) -> Any:
        """事件发射器节点：向 SSE EventBus 广播自定义事件

        metadata.event_type: 事件类型名
        metadata.event_data: 事件数据（dict）
        """
        event_type = node.metadata.get("event_type", "graph_event")
        event_data = dict(node.metadata.get("event_data", {}))
        event_data["node_id"] = node.id

        # 广播到 SSE
        self._emit(event_type, event_data)

        return {
            "emitted_event": event_type,
            "event_data": event_data,
        }

    # ==================== 辅助方法 ====================

    def _find_next_agent_node(self, node_id: str) -> Optional[GraphNode]:
        """沿后继边查找下一个 Agent 节点（跳过 QG；忽略 quality retry 回环）"""
        current = self.graph.get_node(node_id)
        current_agent_id = str(current.agent_id or "") if current else ""
        visited: set[str] = set()
        queue = list(self.graph.get_successors(node_id))
        while queue:
            nid = queue.pop(0)
            if nid in visited or nid == node_id:
                continue
            visited.add(nid)
            candidate = self.graph.get_node(nid)
            if not candidate:
                continue
            if candidate.type == NodeType.AGENT:
                if current_agent_id and str(candidate.agent_id or "") == current_agent_id:
                    queue.extend(self.graph.get_successors(nid))
                    continue
                return candidate
            queue.extend(self.graph.get_successors(nid))
        return None

    def _build_agent_input(
        self,
        node: GraphNode,
        state: GraphState,
        run_ctx,
        agent_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构建传递给 Agent 的输入（RunContext + handoff + workspace refs）"""
        predecessors = self.graph.get_predecessors(node.id)
        phase_id = str(node.metadata.get("phase_id") or "")

        handoffs = []
        workspace_summaries: Dict[str, str] = {}
        for ref in run_ctx.workspace_refs:
            if ref.phase_id and phase_id and ref.phase_id != phase_id:
                continue
            key = ref.artifact_id or ref.path
            if key:
                workspace_summaries[key] = ref.summary or key

        agent_id = str(node.agent_id or "")
        for entry in reversed(run_ctx.collaboration_thread):
            if entry.protocol != "handoff":
                continue
            # 接受：定向给我的（to_agent == agent_id）或广播的（to_agent == "*"）
            is_directed = entry.to_agent and agent_id and entry.to_agent == agent_id
            is_broadcast = entry.to_agent == "*" and entry.from_agent != agent_id
            if not (is_directed or is_broadcast):
                continue
            meta = entry.metadata if isinstance(entry.metadata, dict) else {}
            handoffs.append({
                "from_agent": entry.from_agent,
                "summary": entry.summary,
                "content": meta.get("content") or meta.get("summary") or entry.summary,
                "result": meta.get("result_preview") or meta.get("result"),
                "instructions": meta.get("instructions", ""),
                "artifact_keys": meta.get("artifact_keys") or [],
            })
            if len(handoffs) >= 5:  # 最多收集 5 条（并行模式下可能有多个上游）
                break

        if not handoffs:
            for pred_id in predecessors:
                pred_output = state.get_node_output(pred_id)
                if pred_output and pred_output.output:
                    summary = str(pred_output.output)[:500]
                    handoffs.append({
                        "from_agent": pred_id,
                        "summary": summary,
                        "result": pred_output.output,
                    })

        # ── Deliberative 圆桌模式：注入所有同伴的产出 ──
        phase_spec_raw = node.metadata.get("phase_spec", {})
        if isinstance(phase_spec_raw, dict) and phase_spec_raw.get("collaboration") == "deliberative":
            loop_peer_ids = self.graph.metadata.get("loop_body_node_ids", [])
            for peer_nid in loop_peer_ids:
                if peer_nid == node.id:
                    continue
                peer_output = state.get_node_output(peer_nid)
                if peer_output and peer_output.output:
                    handoffs.append({
                        "from_agent": peer_nid,
                        "summary": str(peer_output.output)[:500],
                        "content": str(peer_output.output),
                        "result": peer_output.output,
                    })

        merged_config = dict(state.values.get("config", {}))
        if agent_config:
            merged_config.update(agent_config)
        merged_config.setdefault("task_description", run_ctx.task or state.get("task") or "")

        # Replay revision context — 如果该 phase 有注入的修改指令
        revision_context = None
        if hasattr(run_ctx, 'get_phase_context'):
            revision_context = run_ctx.get_phase_context(phase_id or node.id)

        result_input: Dict[str, Any] = {
            "run_context": run_ctx,
            "handoffs": handoffs,
            "config": merged_config,
            "accumulated": {aid: o.output for aid, o in state.node_outputs.items()},
            "phase_id": phase_id,
            "workspace_refs": workspace_summaries,
        }

        if revision_context:
            result_input["revision_mode"] = True
            result_input["revision_instructions"] = revision_context.get("instructions", "")
            result_input["previous_output"] = revision_context.get("previous_output", "")
            _logger.info(
                "Agent %s 执行于 revision mode (phase=%s)",
                node.agent_id, phase_id or node.id,
            )

        # 质量重试：注入违规反馈 / 强制降级模型
        retry_hint = (node.metadata or {}).get("retry_hint") or ""
        if retry_hint:
            result_input["retry_hint"] = retry_hint
        force_model = (node.metadata or {}).get("force_model")
        if force_model:
            result_input["force_model"] = force_model

        return result_input

    def _get_run_context(self, config: GraphRunConfig):
        if self._live_run_ctx is not None:
            return self._live_run_ctx
        if not config.run_context:
            return None
        from core.run.run_context import RunContext
        return RunContext.model_validate(config.run_context)

    def _resolve_phase_spec(self, config: GraphRunConfig, phase_id: str):
        from core.run.phase_spec import PhaseSpec
        for raw in config.phase_specs:
            if raw.get("id") == phase_id:
                return PhaseSpec.model_validate(raw)
        return None


# ============================================================
# ParallelExecutor — 并行执行器
# ============================================================

class ParallelExecutor:
    """并行执行器
    
    协调多个节点的并行执行，
    处理超时、取消、错误收集等。
    """

    def __init__(
        self,
        node_executor: NodeExecutor,
        max_concurrent: int = 5,
    ):
        self.executor = node_executor
        # 并行失败节点（不混入返回字典，保持 Dict[str, ExecutionResult] 契约）
        self.last_failures: List[str] = []
        self.max_concurrent = max_concurrent
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def execute_batch(
        self,
        nodes: List[GraphNode],
        state: GraphState,
        config: GraphRunConfig,
    ) -> Dict[str, ExecutionResult]:
        """
        并行执行一组节点
        
        Args:
            nodes: 要执行的节点列表
            state: 图状态（共享）
            config: 运行配置
            
        Returns:
            {node_id: ExecutionResult} 字典
        """
        if not self._semaphore:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)

        async def _run_with_limit(node: GraphNode) -> tuple:
            async with self._semaphore:
                result = await self.executor.execute(node, state, config)
                return (node.id, result)

        tasks = [_run_with_limit(node) for node in nodes]
        results_raw = await asyncio.gather(*tasks, return_exceptions=True)

        # gather 保序（按 tasks 顺序），zip 恢复 node 对应，异常节点标 error 而非静默消失
        from core.graph.types import ExecutionResult as _ER, NodeStatus as _NS

        results: Dict[str, ExecutionResult] = {}
        self.last_failures = []
        for node, item in zip(nodes, results_raw):
            if isinstance(item, Exception):
                import logging
                logging.getLogger("graph.parallel").error(
                    f"并行任务异常: node={node.id} {item}", exc_info=True
                )
                # 异常占位用 ExecutionResult（保持 Dict[str, ExecutionResult] 契约），
                # 完整异常已记服务端日志，result 只放通用消息不外泄内部细节
                results[node.id] = _ER(
                    node_id=node.id,
                    status=_NS.FAILED,
                    error=f"[并行执行异常: {type(item).__name__}]",
                    error_type=type(item).__name__,
                )
                self.last_failures.append(node.id)
            else:
                node_id, exec_result = item
                results[node_id] = exec_result

        return results


# ============================================================
# GraphRuntime — 主执行器（顶层入口）
# ============================================================

class GraphRuntime:
    """Workflow Graph 主执行器
    
    这是 Graph Engine 对外的唯一入口。
    负责：
    - 初始化（Scheduler + State + Event）
    - 执行主循环（调度 → 执行 → 路由 → Checkpoint）
    - 暂停/恢复/取消
    - 事件广播
    
    使用方式:
        graph = GraphBuilder("my_graph").agent(...).edge(...).build()
        runtime = GraphRuntime(graph)
        result = await runtime.execute()
    """

    def __init__(
        self,
        graph: WorkflowGraph,
        handler_registry: Optional[HandlerRegistry] = None,
    ):
        self.graph = graph
        self.status = GraphStatus.INITIALIZING
        self.run_id = ""
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

        # 核心组件
        self.scheduler = Scheduler(graph)
        self.router = Router(graph, handler_registry)
        self.node_executor = NodeExecutor(graph, handler_registry)
        self.node_executor.set_event_emitter(self._emit)
        self.parallel_executor = ParallelExecutor(self.node_executor, graph.config.get("max_parallel", 5))

        # 状态
        self.state: Optional[GraphState] = None
        self._live_run_ctx: Optional[Any] = None  # 单活 RunContext（并行共享）

        # 事件系统
        self._event_handlers: Dict[str, List[Callable]] = {}

        # 配置
        self.config: Optional[GraphRunConfig] = None

    def update_control(self, pause: Optional[bool] = None, cancel: Optional[bool] = None, reason: str = "") -> bool:
        """P0-7: 更新运行控制（pause/cancel），写入 _live_run_ctx（主循环读取处）

        此前 run.py 写 config.run_context（死信），主循环读 _live_run_ctx 副本，
        导致 pause/cancel 完全无效。此方法直接写 _live_run_ctx。
        """
        ctx = self._live_run_ctx
        if ctx is None:
            return False
        if pause is not None:
            ctx.control.pause_requested = pause
        if cancel is not None:
            ctx.control.cancel_requested = cancel
        if reason:
            ctx.control.termination_reason = reason
        return True

    # ==================== 公共 API ====================

    async def execute(
        self,
        initial_state: Optional[Dict[str, Any]] = None,
        run_config: Optional[GraphRunConfig] = None,
    ) -> 'GraphExecutionResult':
        """
        执行图（主入口）
        
        Args:
            initial_state: 初始状态数据
            run_config: 运行配置
            
        Returns:
            GraphExecutionResult 包含最终状态和元信息
        """
        self.run_id = str(uuid.uuid4())[:8]
        self.node_executor.run_id = self.run_id
        self.config = run_config or GraphRunConfig()

        # 单活 RunContext：并行节点共享同一对象，避免 model_validate 副本互相覆盖
        self._init_live_run_ctx()

        # Phase 5 新增：注入 RunContext 到 Scheduler（约束检查用）
        if self._live_run_ctx:
            self.scheduler.set_run_context(self._live_run_ctx)

        # 初始化状态
        self.state = GraphState(
            graph_id=self.graph.graph_id,
            run_id=self.run_id,
            max_iterations=self.config.max_iterations or self.graph.config.get("max_iterations", 100),
        )
        if initial_state:
            self.state.update(initial_state)
        if self.config.initial_state:
            self.state.update(self.config.initial_state)

        # 初始化调度器
        start_nodes = self.graph.get_start_nodes()
        self.scheduler.initialize(start_nodes)

        # 广播开始事件
        self.status = GraphStatus.READY
        self.start_time = time.time()
        self._emit("graph_start", {
            "run_id": self.run_id,
            "graph_id": self.graph.graph_id,
            "nodes": self.graph.node_ids,
            "start_nodes": start_nodes,
            "phase_ids": [
                p.get("id") for p in (self.config.phase_specs or []) if p.get("id")
            ],
            "phase_specs": self.config.phase_specs or [],
        })

        # ========== 主执行循环 ==========
        self.status = GraphStatus.RUNNING
        return await self._run_until_stop(iteration_count=0)

    async def _run_until_stop(self, iteration_count: int = 0) -> 'GraphExecutionResult':
        """主调度循环（execute / resume 共用）"""
        checkpoint_counter = 0
        checkpoint_every = self.graph.config.get("checkpoint_every_n_nodes", 5)

        try:
            while not self.state.should_stop:
                iteration_count += 1

                # ── 检查用户主动 pause/cancel 请求 ──
                run_ctx = self._get_run_context()
                if run_ctx:
                    if run_ctx.control.cancel_requested:
                        self.state.mark_failed("用户取消工作流")
                        self.status = GraphStatus.FAILED
                        break
                    if run_ctx.control.pause_requested:
                        self.status = GraphStatus.PAUSED
                        run_ctx.control.pause_requested = False
                        run_ctx.control.pause_reason = "用户主动暂停"
                        self._emit("workflow_paused", {
                            "run_id": self.run_id,
                            "reason": "用户主动暂停",
                        })
                        break

                schedule = self.scheduler.next()
                if schedule is None:
                    if self.scheduler.is_complete:
                        self.state.mark_completed("所有节点已完成")
                    elif self.scheduler.is_deadlocked:
                        if self.scheduler.failed:
                            self.state.mark_failed(f"存在失败节点: {sorted(self.scheduler.failed)}")
                        else:
                            self.state.mark_failed("死锁：存在不可达的节点")
                    break

                if not schedule.nodes:
                    break

                self._emit("schedule", {
                    "nodes": schedule.nodes,
                    "parallel": schedule.is_parallel_group,
                    "reason": schedule.reason,
                    "progress": self.scheduler.progress,
                })

                if schedule.is_parallel_group:
                    nodes_to_run = [self.graph.get_node(nid) for nid in schedule.nodes]
                    nodes_to_run = [n for n in nodes_to_run if n]

                    run_ctx = self._get_run_context()
                    if run_ctx:
                        from core.run.collaboration_hooks import on_parallel_fork

                        await on_parallel_fork(
                            run_ctx,
                            target_node_ids=schedule.nodes,
                            graph=self.graph,
                            reason="parallel_batch",
                        )
                        self.config.run_context = run_ctx

                    for node in nodes_to_run:
                        self._emit("node_started", {
                            "node_id": node.id,
                            "node_type": node.type.value,
                            "label": node.label or node.id,
                            "agent_id": node.agent_id,
                            "phase_id": node.metadata.get("phase_id", ""),
                        })

                    results = await self.parallel_executor.execute_batch(
                        nodes_to_run, self.state, self.config
                    )

                    for node_id, exec_result in results.items():
                        self._on_node_finished(node_id, exec_result)
                        self.scheduler.complete(node_id, exec_result.is_success)
                        next_nodes = await self.router.resolve(
                            node_id, exec_result, self.state
                        )
                        self._schedule_next(node_id, next_nodes)
                else:
                    node_id = schedule.nodes[0]
                    node = self.graph.get_node(node_id)
                    if not node:
                        self.scheduler.complete(node_id, False)
                        continue

                    await self._run_hooks(node, "pre")

                    self._emit("node_started", {
                        "node_id": node_id,
                        "node_type": node.type.value,
                        "label": node.label or node_id,
                        "agent_id": node.agent_id,
                        "phase_id": node.metadata.get("phase_id", ""),
                    })

                    exec_result = await self.node_executor.execute(
                        node, self.state, self.config
                    )

                    await self._run_hooks(node, "post")
                    self.scheduler.complete(node_id, exec_result.is_success)
                    self._on_node_finished(node_id, exec_result)

                    if exec_result.status == NodeStatus.PAUSED or (
                        self.state.is_terminal and self.state.termination_reason == "paused"
                    ):
                        self.status = GraphStatus.PAUSED
                        self._emit("graph_paused", {
                            "run_id": self.run_id,
                            "reason": self.state.termination_message,
                            "node_id": node_id,
                            "phase_id": node.metadata.get("phase_id", ""),
                        })
                        break

                    next_nodes = await self.router.resolve(
                        node_id, exec_result, self.state
                    )
                    self._schedule_next(node_id, next_nodes)

                checkpoint_counter += 1
                if self.config.checkpoint_enabled and checkpoint_counter % checkpoint_every == 0:
                    await self._save_checkpoint()

                if iteration_count > self.state.max_iterations * 2:
                    self.state.mark_failed("超过安全迭代上限")
                    break

            self.end_time = time.time()
            duration = (self.end_time - self.start_time) if self.start_time else 0

            if self.state.is_successful:
                self.status = GraphStatus.COMPLETED
            elif self.status != GraphStatus.PAUSED:
                self.status = GraphStatus.FAILED if self.state.termination_reason == "error" else GraphStatus.CANCELLED

            if self.config.checkpoint_enabled:
                await self._save_checkpoint()

            if self.status == GraphStatus.COMPLETED:
                final_event_name = "graph_completed"
            elif self.status == GraphStatus.PAUSED:
                final_event_name = "graph_paused"
            elif self.status == GraphStatus.FAILED:
                final_event_name = "graph_failed"
            else:
                final_event_name = "graph_cancelled"

            self._emit(final_event_name, {
                "run_id": self.run_id,
                "status": self.status.value,
                "duration_seconds": round(duration, 3),
                "iterations": iteration_count,
                "progress": self.scheduler.progress,
                "termination_reason": self.state.termination_reason,
                "final_state": self.state.snapshot(),
            })

            return GraphExecutionResult(
                run_id=self.run_id,
                status=self.status,
                state=self.state,
                duration_seconds=duration,
                iterations=iteration_count,
                scheduler_progress=self.scheduler.progress,
            )

        except Exception as e:
            self.status = GraphStatus.FAILED
            self.state.mark_failed(str(e))
            self._emit("graph_error", {
                "run_id": self.run_id,
                "error": str(e),
                "traceback": traceback.format_exc(),
            })
            raise

    async def resume(self, user_input: Optional[Any] = None) -> 'GraphExecutionResult':
        """从暂停状态恢复执行（Human review / decision 后继续）"""
        if self.status != GraphStatus.PAUSED:
            raise RuntimeError(f"无法恢复：当前状态为 {self.status}，不是 PAUSED")
        if not self.state:
            raise RuntimeError("无 GraphState，无法恢复")

        if user_input is not None:
            self.state.set("_user_input", user_input)

        # resume 时确保 live RunContext 仍指向同一对象
        self._init_live_run_ctx()

        self.state.termination_reason = None
        self.state.termination_message = ""
        self.status = GraphStatus.RUNNING

        pause_ctx = self.state.get("_pause_context") or {}
        paused_node = pause_ctx.get("node_id") or self.state.current_node_id
        if paused_node:
            fake_result = ExecutionResult(
                node_id=paused_node,
                status=NodeStatus.COMPLETED,
                output={"resumed": True},
            )
            next_nodes = await self.router.resolve(paused_node, fake_result, self.state)
            self._schedule_next(paused_node, next_nodes)

        self._emit("graph_resumed", {"run_id": self.run_id, "node_id": paused_node})
        return await self._run_until_stop(iteration_count=0)

    def cancel(self) -> None:
        """取消执行"""
        self.state.mark_cancelled("用户取消")
        self.status = GraphStatus.CANCELLED
        # P0-4：广播完整 status + reason，前端终态映射需要
        self._emit("graph_cancelled", {
            "run_id": self.run_id,
            "status": "cancelled",
            "reason": "用户取消",
            "termination_reason": "用户取消",
        })

    # ==================== 事件系统 ====================

    def on(self, event_name: str, handler: Callable) -> 'GraphRuntime':
        """注册事件监听器"""
        self._event_handlers.setdefault(event_name, []).append(handler)
        return self

    def _emit(self, event_name: str, data: Dict[str, Any]) -> None:
        """触发事件：先回调本地 handler，再通过 EventBus 广播 SSE"""
        # 1. 本地事件处理器
        handlers = self._event_handlers.get(event_name, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    # 强引用跟踪，防孤儿任务被 GC（item8 同类问题）
                    from core.task_tracker import create_task
                    create_task(handler(data), name=f"graph_evt_{event_name}")
                else:
                    handler(data)
            except Exception as e:
                import logging
                logging.getLogger("graph.events").warning(
                    f"事件处理器异常 [{event_name}]: {e}"
                )

        # 2. 全局 EventBus → SSE 广播
        try:
            from core.events import event_bus
            project_id = self.config.project_id
            event_bus.broadcast(
                event=event_name,
                data=data,
                project_id=project_id or None,
            )
        except Exception as e:
            _logger.warning("事件广播失败（SSE 客户端可能漏收 %s）: %s", event_name, e)

    # ==================== 内部方法 ====================

    def _on_node_finished(self, node_id: str, result: ExecutionResult) -> None:
        """节点完成的回调"""
        event_type = (
            "node_completed" if result.is_success
            else "node_failed"
        )
        node = self.graph.get_node(node_id)
        payload: Dict[str, Any] = {
            "node_id": node_id,
            "status": result.status.value,
            "duration_ms": result.duration_ms,
        }
        if result.error:
            payload["error"] = result.error
        if node:
            payload["phase_id"] = str(node.metadata.get("phase_id") or "")
            payload["agent_id"] = node.agent_id or ""
            payload["label"] = node.label or node_id
            payload["node_type"] = node.type.value
        self._emit(event_type, payload)

    def _schedule_next(self, source_node: str, target_nodes: List[str]) -> None:
        """将路由决策的目标节点加入调度"""
        for target_id in target_nodes:
            if target_id == END_NODE_ID:
                self.state.mark_completed(f"从 {source_node} 到达终点")
                continue

            edge = self.graph.get_edge(source_node, target_id)
            if edge and edge.edge_type == EdgeType.LOOP_BACK:
                self._requeue_quality_retry(source_node, target_id)
                continue

            if target_id not in self.scheduler.completed:
                if target_id not in self.scheduler.ready and target_id not in self.scheduler.running:
                    self.scheduler.ready.add(target_id)

        # 质量门通过但路由未产出后继 → 防御性显式调度（2026-08-09 P1，doc1 方案D）。
        # 防止 reviewer 等后继节点因路由异常未被调度导致 run 停在质量门后。
        if not target_nodes:
            _node = self.graph.get_node(source_node)
            if _node and _node.type == NodeType.QUALITY_GATE:
                if bool(self.state.values.get("quality_passed", True)):
                    for succ_id in self.graph.get_successors(source_node):
                        _edge = self.graph.get_edge(source_node, succ_id)
                        if _edge and _edge.edge_type == EdgeType.LOOP_BACK:
                            continue
                        if succ_id == END_NODE_ID:
                            self.state.mark_completed(f"从 {source_node} 到达终点")
                            continue
                        if (succ_id not in self.scheduler.completed
                                and succ_id not in self.scheduler.ready
                                and succ_id not in self.scheduler.running):
                            self.scheduler.ready.add(succ_id)
                            _logger.info("质量门通过，防御性调度后继节点: %s", succ_id)

    def _requeue_quality_retry(self, gate_node_id: str, agent_node_id: str) -> None:
        """质量门控未通过 → 将 Agent 阶段重新入队。

        review_mode: 重试 max_retries 次后暂停等待人工介入
        自主模式: 重试 max_retries 次后跳过该阶段继续执行（不再无限重试）
        """
        gate_node = self.graph.get_node(gate_node_id)
        qp = (gate_node.metadata.get("quality_profile") if gate_node else None) or {}
        review_mode = bool(self.config and self.config.review_mode)
        max_retries = int(qp.get("max_retries") or 3)
        # 自主模式也有最大重试次数上限，防止无限循环
        autonomous_max = int(qp.get("autonomous_max_retries") or max_retries)
        effective_max = max_retries if review_mode else autonomous_max

        retry_key = f"quality_retry:{gate_node_id}"
        attempt = int(self.state.values.get(retry_key, 0) or 0) + 1
        self.state.set(retry_key, attempt)

        phase_id = (gate_node.metadata.get("phase_id") if gate_node else "") or agent_node_id
        violations = self.state.values.get("quality_violations") or []

        if attempt > effective_max:
            detail = ""
            if violations and isinstance(violations[0], dict):
                detail = str(violations[0].get("message") or "")

            if review_mode:
                # review 模式：暂停等待人工介入
                msg = (
                    f"阶段 {phase_id} 未产出合格交付物，已自动重试 {effective_max} 次仍失败"
                    + (f"（{detail}）" if detail else "")
                )
                self.state.mark_failed(msg)
                self._emit("quality_retry_exhausted", {
                    "phase_id": phase_id,
                    "agent_node_id": agent_node_id,
                    "gate_node_id": gate_node_id,
                    "attempt": attempt,
                    "max_retries": effective_max,
                    "violations": violations,
                    "message": msg,
                })
                return
            else:
                # 自主模式：跳过该阶段，继续执行后续流程
                msg = (
                    f"阶段 {phase_id} 质量未通过，已重试 {effective_max} 次仍失败，跳过继续执行"
                    + (f"（{detail}）" if detail else "")
                )
                self._emit("quality_retry_skipped", {
                    "phase_id": phase_id,
                    "agent_node_id": agent_node_id,
                    "gate_node_id": gate_node_id,
                    "attempt": attempt,
                    "max_retries": effective_max,
                    "violations": violations,
                    "message": msg,
                })
                # 标记为已完成（跳过），让调度器继续后继节点
                self.scheduler.completed.add(gate_node_id)
                self.scheduler.completed.add(agent_node_id)
                if agent_node_id not in self.state.completed_nodes:
                    self.state.completed_nodes.append(agent_node_id)
                # 释放后继节点
                next_nodes = self.graph.get_successors(gate_node_id)
                for nid in next_nodes:
                    if nid not in self.scheduler.completed:
                        self.scheduler.pending[nid] = max(0, self.scheduler.pending.get(nid, 1) - 1)
                        if self.scheduler.pending[nid] <= 0:
                            self.scheduler.ready.add(nid)
                return

        # ── 重试换策略：第 2 次及以上重试时注入质量违规反馈 ──
        if attempt >= 2 and violations:
            violation_summary = "; ".join(
                v.get("message", str(v)) if isinstance(v, dict) else str(v)
                for v in violations[:3]
            )
            retry_hint = (
                f"\n\n⚠️ **质量重试反馈（第 {attempt} 次重试）**：\n"
                f"上次产出未通过质量门禁，具体问题：{violation_summary}\n"
                f"请针对以上问题调整产出内容，确保质量达标。"
            )
            # 注入到节点 metadata，NodeExecutor 会读取并追加到 prompt
            agent_node = self.graph.get_node(agent_node_id)
            if agent_node:
                agent_node.metadata["retry_hint"] = retry_hint
                agent_node.metadata["retry_attempt"] = attempt

        # ── 第 3 次重试切换到 fallback 模型（如果配置了）──
        if attempt >= 3:
            agent_node = self.graph.get_node(agent_node_id)
            if agent_node and not agent_node.metadata.get("fallback_applied"):
                from core.models.registry import get_model_registry
                from config import DEFAULT_LLM_MODEL
                registry = get_model_registry()
                model_id = DEFAULT_LLM_MODEL
                spec = registry.get_spec(model_id)
                if spec and spec.fallback:
                    agent_node.metadata["force_model"] = spec.fallback
                    agent_node.metadata["fallback_applied"] = True
                    self._emit("quality_retry_fallback", {
                        "phase_id": phase_id,
                        "agent_node_id": agent_node_id,
                        "fallback_model": spec.fallback,
                        "attempt": attempt,
                    })

        for nid in (agent_node_id, gate_node_id):
            self.scheduler.completed.discard(nid)
            if nid in self.state.completed_nodes:
                self.state.completed_nodes.remove(nid)

        if agent_node_id not in self.scheduler.ready and agent_node_id not in self.scheduler.running:
            self.scheduler.ready.add(agent_node_id)

        self._emit("quality_retry", {
            "phase_id": phase_id,
            "agent_node_id": agent_node_id,
            "gate_node_id": gate_node_id,
            "attempt": attempt,
            "max_retries": effective_max,
            "autonomous": not review_mode,
            "violations": violations,
            "message": (
                f"质量未通过，自动重试阶段 {phase_id}（第 {attempt}/{effective_max} 次）"
            ),
        })

    def _init_live_run_ctx(self) -> None:
        """从 config.run_context 反序列化一次，注入 NodeExecutor 供并行共享"""
        from core.run.run_context import RunContext
        raw = self.config.run_context if self.config else None
        if not raw:
            self._live_run_ctx = None
            self.node_executor.set_live_run_ctx(None)
            return
        if isinstance(raw, RunContext):
            self._live_run_ctx = raw
        else:
            self._live_run_ctx = RunContext.model_validate(raw)
        self.node_executor.set_live_run_ctx(self._live_run_ctx)

    def _get_run_context(self):
        """GraphRuntime 侧读取 RunContext（并行调度 broadcast 用）"""
        if self._live_run_ctx is not None:
            return self._live_run_ctx
        if not self.config or not self.config.run_context:
            return None
        from core.run.run_context import RunContext
        return RunContext.model_validate(self.config.run_context)

    async def _run_hooks(self, node: GraphNode, hook_type: str) -> None:
        """执行节点的钩子函数"""
        hooks = node.config.pre_hooks if hook_type == "pre" else node.config.post_hooks
        registry = self.node_executor.registry
        for hook_ref in hooks:
            handler = registry.get(hook_ref)
            if handler:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(node, self.state)
                    else:
                        handler(node, self.state)
                except Exception as e:
                    import logging
                    logging.getLogger("graph.hooks").warning(
                        f"钩子执行异常 [{hook_ref}] on {node.id}: {e}"
                    )

    async def _save_checkpoint(self) -> None:
        """保存 checkpoint 到 SQLite（通过 StateManager）

        将 GraphState 序列化为 JSON 写入 projects.checkpoint_state，
        支持乐观锁和断点恢复。
        """
        if not self.state:
            return

        project_id = self.config.project_id
        if not project_id:
            self._emit("checkpoint_skipped", {
                "run_id": self.run_id,
                "reason": "no project_id in config",
            })
            return

        try:
            from core.agent.state_manager import StateManager
            from core.run.run_context import RunContext

            run_ctx = None
            if self.config.run_context:
                # P0-2: config.run_context 可能已是 RunContext 对象（A/B 合一），直接复用
                # 避免 checkpoint 时另建副本导致 resume 后再分裂。
                run_ctx = (
                    self.config.run_context
                    if isinstance(self.config.run_context, RunContext)
                    else RunContext.model_validate(self.config.run_context)
                )

            if run_ctx:
                snap = self.state.snapshot()
                paused_node = ""
                if self.status == GraphStatus.PAUSED:
                    pause_ctx = self.state.get("_pause_context") or {}
                    paused_node = pause_ctx.get("node_id", "")

                run_ctx.sync_from_graph_state(
                    snap.get("values", {}),
                    {
                        "run_id": self.run_id,
                        "completed_nodes": list(self.state.completed_nodes),
                        "current_node": self.state.current_node_id,
                        "runtime_status": self.status.value,
                    },
                )
                run_ctx.capture_graph_resume(
                    graph_def=self.graph.to_dict(),
                    graph_state=snap,
                    scheduler=self.scheduler.snapshot(),
                    runtime_status=self.status.value,
                    paused_node_id=paused_node,
                    graph_run_config=self.config.model_dump(),
                )
                if self.status == GraphStatus.PAUSED:
                    run_ctx.control.pending_human = True
                elif self.status == GraphStatus.COMPLETED:
                    run_ctx.control.pending_human = False
                await StateManager.save_run_context(project_id, run_ctx)
                self.config.run_context = run_ctx
            else:
                _logger.warning("checkpoint 跳过：缺少 RunContext project=%s", project_id)

            self._emit("checkpoint_saved", {
                "run_id": self.run_id,
                "project_id": project_id,
            })
        except Exception as e:
            # checkpoint 冲突时重试 1 次（并行节点同时完成可能导致乐观锁冲突）
            if "CheckpointConflict" in type(e).__name__ or "checkpoint_version" in str(e):
                import asyncio as _asyncio
                _logger.warning("checkpoint 乐观锁冲突，0.1s 后重试 1 次: %s", e)
                await _asyncio.sleep(0.1)
                try:
                    await StateManager.save_run_context(project_id, run_ctx)
                    self._emit("checkpoint_saved", {
                        "run_id": self.run_id,
                        "project_id": project_id,
                    })
                    return
                except Exception as e2:
                    _logger.warning("checkpoint 重试仍失败（不阻塞执行）: %s", e2)
            else:
                _logger.warning("Checkpoint 保存失败: %s", e, exc_info=True)


# ============================================================
# GraphExecutionResult — 执行结果包装
# ============================================================

class GraphExecutionResult(BaseModel):
    """图执行的最终结果"""
    run_id: str
    status: GraphStatus
    state: Optional[GraphState] = None
    duration_seconds: float = 0.0
    iterations: int = 0
    scheduler_progress: Dict[str, int] = Field(default_factory=dict)
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.status == GraphStatus.COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)
