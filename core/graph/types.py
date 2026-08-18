"""Workflow Graph Engine — 核心类型定义

Platform-Core v3.1: 声明式工作流图引擎的类型系统。
所有图结构、节点、边、条件、状态都定义为 Pydantic 模型，
确保编译期类型检查和运行时序列化能力。

核心概念:
- WorkflowGraph: 有向图定义 (Nodes + Edges)
- GraphNode: 节点（Agent/Function/Router/Subgraph/...）
- GraphEdge: 有向边（Always/Conditional/Loop/Error）
- GraphCondition: 边的遍历条件
- GraphState: 运行时共享状态容器
"""

from __future__ import annotations
from enum import Enum
from typing import (
    Any, Callable, Dict, List, Optional, Set, Union,
    Literal, Type, TypeVar, get_type_hints,
)
from datetime import datetime
from pydantic import BaseModel, Field, model_validator


# ============================================================
# 枚举类型
# ============================================================

class NodeType(str, Enum):
    """节点类型枚举"""

    # === 执行节点 ===
    AGENT = "agent"                      # Agent 执行节点
    FUNCTION = "function"                # 纯函数 / Python callable
    SUBGRAPH = "subgraph"               # 子图（嵌套组合）

    # === 控制流节点 ===
    ROUTER = "router"                   # 条件路由器（N 路分支）
    SWITCH = "switch"                   # 开关（二选一）
    MERGE = "merge"                     # 多路汇聚
    FORK = "fork"                       # 分叉（并行启动多路）

    # === 特殊节点 ===
    START = "start"                     # 图入口（隐式，无需显式定义）
    END = "end"                         # 图出口（隐式）
    INPUT = "input"                     # 输入映射节点
    OUTPUT = "output"                   # 输出映射节点

    # === 高级节点 ===
    LOOP = "loop"                       # 循环节点
    HUMAN_IN_THE_LOOP = "human"         # 人机协作节点
    EVENT_EMITTER = "event"             # 事件发射器
    CHECKPOINT = "checkpoint"           # 显式检查点节点
    QUALITY_GATE = "quality_gate"       # 质量门禁节点（自动执行 QualityGateway 检查）


class EdgeType(str, Enum):
    """边类型枚举"""
    ALWAYS = "always"                   # 无条件转移
    CONDITIONAL = "conditional"          # 条件边
    DEFAULT = "default"                 # 默认边（其他条件都不满足时走）
    PARALLEL_JOIN = "parallel_join"     # 并行汇合（等所有前驱完成）
    LOOP_BACK = "loop_back"             # 回环边
    ERROR_EDGE = "error"                # 异常处理边
    TIMEOUT_EDGE = "timeout"            # 超时处理边


class ConditionType(str, Enum):
    """条件评估类型"""
    EXPRESSION = "expression"           # 表达式求值 (state.xxx > 0.5)
    HANDLER_FN = "handler_fn"           # Python callable
    LLM_ROUTER = "llm_router"           # LLM 动态路由决策
    CONSENSUS = "consensus"             # 共识检测（多 Agent 投票）
    CUSTOM = "custom"                   # 自定义条件类


class NodeStatus(str, Enum):
    """节点执行状态"""
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"


class GraphStatus(str, Enum):
    """图执行状态"""
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MergeStrategy(str, Enum):
    """并行汇聚策略"""
    ALL = "all"                         # 保留所有输出 (dict)
    FIRST = "first"                     # 取第一个完成的
    LAST = "last"                       # 取最后一个完成的
    WINNER_TAKES_ALL = "winner"         # 类似旧 parallel 的竞争模式
    CONCAT = "concat"                   # 列表拼接
    DICT_MERGE = "dict_merge"           # 字典深度合并


# ============================================================
# 特殊节点 ID 常量
# ============================================================

START_NODE_ID = "__start__"
END_NODE_ID = "__end__"


# ============================================================
# GraphCondition — 遍历条件
# ============================================================

class GraphCondition(BaseModel):
    """边的遍历条件
    
    支持 4 种求值策略:
    1. EXPRESSION: 声明式表达式字符串，基于 state 求值
    2. HANDLER_FN: Python async callable
    3. LLM_ROUTER: LLM 动态决策
    4. CONSENSUS: 多 Agent 共识投票
    """
    
    condition_type: ConditionType = ConditionType.EXPRESSION
    expression: str = ""                        # 表达式 (EXPRESSION 类型用)
    label: str = ""                              # 可读标签（用于日志/UI）
    priority: int = 0                            # 优先级（多条边匹配时取最高）
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # --- handler_fn 相关 ---
    # 注意: Callable 不能直接序列化，这里存的是引用标识
    handler_ref: str = ""                        # 函数引用名（HANDLER_FN 用）

    # --- LLM Router 相关 ---
    llm_prompt_template: str = ""                # LLM 提示模板
    llm_options: List[str] = Field(default_factory=list)  # 可选目标列表

    # --- Consensus 相关 ---
    consensus_threshold: float = 0.7             # 共识阈值
    consensus_participants: List[str] = Field(default_factory=list)  # 参与者节点列表

    @model_validator(mode='after')
    def _validate_condition(self) -> 'GraphCondition':
        """根据 condition_type 校验必填字段"""
        if self.condition_type == ConditionType.EXPRESSION and not self.expression:
            raise ValueError("EXPRESSION condition requires 'expression' field")
        if self.condition_type == ConditionType.HANDLER_FN and not self.handler_ref:
            raise ValueError("HANDLER_FN condition requires 'handler_ref' field")
        if self.condition_type == ConditionType.LLM_ROUTER and not self.llm_options:
            raise ValueError("LLM_ROUTER condition requires 'llm_options'")
        return self


# ============================================================
# GraphEdge — 有向边
# ============================================================

class GraphEdge(BaseModel):
    """有向边：连接两个节点，可选带遍历条件"""
    
    from_node: str                            # 源节点 ID
    to_node: str                              # 目标节点 ID
    edge_type: EdgeType = EdgeType.ALWAYS
    condition: Optional[GraphCondition] = None  # 遍历条件（CONDITIONAL 时必须）
    label: str = ""                            # 边标签（用于可视化）
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def _validate_edge(self) -> 'GraphEdge':
        if self.edge_type == EdgeType.CONDITIONAL and not self.condition:
            raise ValueError("CONDITIONAL edge requires a 'condition'")
        return self

    @property
    def is_conditional(self) -> bool:
        return self.edge_type in (EdgeType.CONDITIONAL, EdgeType.LOOP_BACK)

    @property
    def is_error_handling(self) -> bool:
        return self.edge_type == EdgeType.ERROR_EDGE


# ============================================================
# GraphNode — 节点定义
# ============================================================

class NodeConfig(BaseModel):
    """节点的运行配置"""
    timeout_seconds: Optional[int] = None      # 超时时间（秒）
    retry_count: int = 0                       # 失败重试次数
    retry_delay: float = 0.0                   # 重试延迟（秒）
    fail_on_error: bool = True                 # 出错是否终止图执行
    run_in_background: bool = False            # 是否后台异步执行

    # Hook 配置
    pre_hooks: List[str] = Field(default_factory=list)   # 前置钩子 (handler_ref)
    post_hooks: List[str] = Field(default_factory=list)   # 后置钩子 (handler_ref)


class GraphNode(BaseModel):
    """图节点定义
    
    根据 NodeType 不同，config 中的有效字段不同:
    - AGENT: agent_id 必填
    - FUNCTION: func_ref 必填
    - SUBGRAPH: subgraph_id 必填
    - ROUTER: routes 必填
    """
    
    id: str                                    # 节点唯一 ID
    type: NodeType = NodeType.AGENT
    label: str = ""                             # 显示标签
    description: str = ""                       # 节点描述

    # === Agent 节点专用 ===
    agent_id: Optional[str] = None              # Agent Registry 中的 agent_id

    # === Function 节点专用 ===
    func_ref: Optional[str] = None              # 函数引用名

    # === Subgraph 节点专用 ===
    subgraph_id: Optional[str] = None           # 子图的 graph_id
    input_mapping: Dict[str, str] = Field(default_factory=dict)   # state key 映射
    output_mapping: Dict[str, str] = Field(default_factory=dict)  # state key 映射

    # === Router 节点专用 ===
    routes: Dict[str, str] = Field(default_factory=dict)  # {route_key: target_node_id}
    router_type: ConditionType = ConditionType.HANDLER_FN

    # === Loop 节点专用 ===
    loop_body: List[str] = Field(default_factory=list)     # 循环体内节点 ID 列表
    max_iterations: int = 10                  # 最大迭代次数
    terminate_condition: Optional[GraphCondition] = None   # 终止条件

    # === Fork/Merge 专用 ===
    fork_targets: List[str] = Field(default_factory=list)   # Fork 目标列表
    merge_sources: List[str] = Field(default_factory=list)  # Merge 来源列表
    merge_strategy: MergeStrategy = MergeStrategy.ALL

    # === 通用配置 ===
    config: NodeConfig = Field(default_factory=NodeConfig)

    # 元数据
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def _validate_node(self) -> 'GraphNode':
        """根据节点类型校验必填字段"""
        if self.type == NodeType.AGENT and not self.agent_id:
            raise ValueError(f"AGENT node '{self.id}' requires 'agent_id'")
        if self.type == NodeType.FUNCTION and not self.func_ref:
            raise ValueError(f"FUNCTION node '{self.id}' requires 'func_ref'")
        if self.type == NodeType.SUBGRAPH and not self.subgraph_id:
            raise ValueError(f"SUBGRAPH node '{self.id}' requires 'subgraph_id'")
        if self.type == NodeType.ROUTER and not self.routes:
            raise ValueError(f"ROUTER node '{self.id}' requires 'routes'")
        if self.type == NodeType.FORK and not self.fork_targets:
            raise ValueError(f"FORK node '{self.id}' requires 'fork_targets'")
        return self


# ============================================================
# StateSchema — 状态 schema 定义
# =================================================-----------

class StateFieldDef(BaseModel):
    """状态字段定义"""
    name: str
    type_hint: str = "Any"                    # 类型提示字符串
    default: Any = None
    required: bool = True
    description: str = ""


class StateSchema(BaseModel):
    """图的状态 Schema 定义
    
    用于类型约束和文档生成。
    运行时不做强类型检查（Python 动态特性），但可用于：
    - IDE 自动补全
    - 序列化时的字段验证
    - UI 表单生成
    - 文档自动生成
    """
    
    fields: List[StateFieldDef] = Field(default_factory=list)
    description: str = ""

    def add_field(
        self,
        name: str,
        type_hint: str = "Any",
        default: Any = None,
        required: bool = True,
        description: str = "",
    ) -> 'StateSchema':
        """链式添加字段"""
        self.fields.append(StateFieldDef(
            name=name,
            type_hint=type_hint,
            default=default,
            required=required,
            description=description,
        ))
        return self

    def get_field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    def to_dict_schema(self) -> Dict[str, Any]:
        """转换为 JSON Schema 格式"""
        properties = {}
        required = []
        for f in self.fields:
            properties[f.name] = {
                "type": f.type_hint.lower(),
                "description": f.description,
                "default": f.default,
            }
            if f.required:
                required.append(f.name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }


# ============================================================
# ExecutionResult — 节点执行结果
# ============================================================

class ExecutionResult(BaseModel):
    """单个节点的执行结果"""
    
    node_id: str
    status: NodeStatus = NodeStatus.COMPLETED
    
    output: Any = None                          # 节点产出
    error: Optional[str] = None                 # 错误信息
    error_type: Optional[str] = None            # 错误类型名

    # 时间统计
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: float = 0.0

    # 元数据
    metadata: Dict[str, Any] = Field(default_factory=dict)
    iterations: int = 1                         # 实际执行轮次（循环场景）

    @property
    def is_success(self) -> bool:
        return self.status == NodeStatus.COMPLETED

    @property
    def is_failure(self) -> bool:
        return self.status == NodeStatus.FAILED


# ============================================================
# GraphState — 运行时共享状态
# ============================================================

class GraphState(BaseModel):
    """图运行时的全局共享状态
    
    这是所有节点读写数据的唯一通道。
    设计原则：
    - 类型安全: 通过 Schema 约束（不强校验）
    - 可序列化: 全部数据可 JSON 化
    - 不可变快照: 支持创建 checkpoint 快照
    - 路由友好: next_nodes 字段支持运行时动态路由
    """

    # ---- 业务数据 ----
    values: Dict[str, Any] = Field(default_factory=dict)

    # ---- 节点输出缓存 ----
    node_outputs: Dict[str, ExecutionResult] = Field(default_factory=dict)

    # ---- 路由决策 ----
    # 运行时由 Router 写入，下游节点读取
    routing_context: Dict[str, Any] = Field(default_factory=dict)
    last_router_output: Optional[str] = None    # 上一次路由器的输出 route_key

    # ---- 控制信息 ----
    current_node_id: Optional[str] = None       # 当前正在执行的节点
    completed_nodes: List[str] = Field(default_factory=list)
    failed_nodes: List[str] = Field(default_factory=list)
    iteration_count: int = 0                    # 全局迭代计数
    max_iterations: int = 100                   # 安全上限

    # ---- 循环控制 ----
    loop_counters: Dict[str, int] = Field(default_factory=dict)  # loop_node_id → 当前次数

    # ---- 终止标志 ----
    termination_reason: Optional[str] = None    # completed | error | paused | max_iterations | cancelled
    termination_message: str = ""

    # ---- 元数据 ----
    graph_id: str = ""
    run_id: str = ""                            # 本次运行的唯一 ID
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    # ==================== 数据访问 API ====================

    def get(self, key: str, default: Any = None) -> Any:
        """读取业务数据"""
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写入业务数据"""
        self.values[key] = value
        self._touch()

    def update(self, data: Dict[str, Any]) -> None:
        """批量写入业务数据"""
        self.values.update(data)
        self._touch()

    def delete(self, key: str) -> None:
        """删除业务数据"""
        self.values.pop(key, None)
        self._touch()

    def has(self, key: str) -> bool:
        return key in self.values

    # ==================== 节点输出 API ====================

    def set_node_output(self, node_id: str, result: ExecutionResult) -> None:
        """记录节点执行结果"""
        self.node_outputs[node_id] = result
        if result.is_success:
            if node_id not in self.completed_nodes:
                self.completed_nodes.append(node_id)
        else:
            if node_id not in self.failed_nodes:
                self.failed_nodes.append(node_id)
        self._touch()

    def get_node_output(self, node_id: str) -> Optional[ExecutionResult]:
        """获取节点执行结果"""
        return self.node_outputs.get(node_id)

    def get_node_result_value(self, node_id: str) -> Any:
        """获取节点的纯产出值（去掉包装）"""
        result = self.node_outputs.get(node_id)
        return result.output if result else None

    def is_node_completed(self, node_id: str) -> bool:
        return node_id in self.completed_nodes

    def is_node_failed(self, node_id: str) -> bool:
        return node_id in self.failed_nodes

    # ==================== 循环控制 API ====================

    def increment_loop(self, loop_node_id: str, max_iters: int) -> int:
        """递增循环计数，返回当前次数"""
        current = self.loop_counters.get(loop_node_id, 0) + 1
        self.loop_counters[loop_node_id] = current
        self.iteration_count += 1
        self._touch()
        return current

    def get_loop_count(self, loop_node_id: str) -> int:
        return self.loop_counters.get(loop_node_id, 0)

    # ==================== 终止判定 API ====================

    def mark_completed(self, message: str = "") -> None:
        self.termination_reason = "completed"
        self.termination_message = message
        self._touch()

    def mark_failed(self, error: str) -> None:
        self.termination_reason = "error"
        self.termination_message = error
        self._touch()

    def mark_paused(self, reason: str = "") -> None:
        self.termination_reason = "paused"
        self.termination_message = reason
        self._touch()

    def mark_cancelled(self, reason: str = "") -> None:
        self.termination_reason = "cancelled"
        self.termination_message = reason
        self._touch()

    @property
    def is_terminal(self) -> bool:
        """是否已到达终止状态"""
        return self.termination_reason is not None

    @property
    def is_successful(self) -> bool:
        return self.termination_reason == "completed"

    @property
    def should_stop(self) -> bool:
        """是否应该停止执行"""
        if self.is_terminal:
            return True
        if self.iteration_count >= self.max_iterations:
            self.termination_reason = "max_iterations"
            self.termination_message = f"达到最大迭代次数 ({self.max_iterations})"
            return True
        return False

    # ==================== 快照 & 序列化 ====================

    def snapshot(self) -> Dict[str, Any]:
        """创建状态快照（用于 checkpoint）"""
        return self.model_dump(exclude_none=True)

    @classmethod
    def from_snapshot(cls, data: Dict[str, Any]) -> 'GraphState':
        """从快照恢复"""
        return cls.model_validate(data)

    def _touch(self) -> None:
        self.updated_at = datetime.now().isoformat()


# ============================================================
# WorkflowGraph — 图定义（顶层）
# ============================================================

class WorkflowGraph(BaseModel):
    """工作流图定义（声明式）
    
    这是一个纯声明式的数据结构，不包含任何运行时逻辑。
    可以完整序列化为 JSON/YAML，用于持久化和传输。
    
    组成:
    - nodes: 节点集合
    - edges: 有向边集合
    - state_schema: 状态类型约束
    - metadata: 图级元数据
    """
    
    graph_id: str
    version: str = "1.0.0"
    label: str = ""
    description: str = ""

    # ---- 结构 ----
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)

    # ---- 状态 ----
    state_schema: Optional[StateSchema] = None

    # ---- 元数据 ----
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    # ---- 运行时配置（非状态）----
    config: Dict[str, Any] = Field(default_factory=lambda: {
        "max_parallel": 5,
        "default_timeout": 300,
        "checkpoint_every_n_nodes": 5,
        "enable_trace": True,
    })

    # ==================== 查询 API ====================

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        """按 ID 查找节点"""
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_edges_from(self, node_id: str) -> List[GraphEdge]:
        """获取从指定节点出发的所有出边"""
        return [e for e in self.edges if e.from_node == node_id]

    def get_edges_to(self, node_id: str) -> List[GraphEdge]:
        """获取指向指定节点的所有入边"""
        return [e for e in self.edges if e.to_node == node_id]

    def get_successors(self, node_id: str) -> List[str]:
        """获取直接后继节点 ID 列表"""
        return list({e.to_node for e in self.get_edges_from(node_id)})

    def get_predecessors(self, node_id: str) -> List[str]:
        """获取直接前驱节点 ID 列表"""
        return list({e.from_node for e in self.get_edges_to(node_id)})

    def get_edge(self, from_node: str, to_node: str) -> Optional[GraphEdge]:
        """查找指定的边"""
        for e in self.edges:
            if e.from_node == from_node and e.to_node == to_node:
                return e
        return None

    def get_start_nodes(self) -> List[str]:
        """获取入口节点（无入边或仅从 START 来的节点）"""
        start = []
        for node in self.nodes:
            in_edges = self.get_edges_to(node.id)
            if len(in_edges) == 0:
                start.append(node.id)
            elif len(in_edges) == 1 and in_edges[0].from_node == START_NODE_ID:
                start.append(node.id)
        return start

    def get_end_nodes(self) -> List[str]:
        """获取出口节点（无出边或只到 END 的节点）"""
        end = []
        for node in self.nodes:
            out_edges = self.get_edges_from(node.id)
            if len(out_edges) == 0:
                end.append(node.id)
            elif len(out_edges) == 1 and out_edges[0].to_node == END_NODE_ID:
                end.append(node.id)
        return end

    def get_nodes_by_type(self, node_type: NodeType) -> List[GraphNode]:
        """按类型筛选节点"""
        return [n for n in self.nodes if n.type == node_type]

    # ==================== 图查询 API ====================

    @property
    def node_ids(self) -> List[str]:
        return [n.id for n in self.nodes]

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def has_conditions(self) -> bool:
        """图中是否存在条件边"""
        return any(e.is_conditional for e in self.edges)

    @property
    def has_loops(self) -> bool:
        """图中是否存在回环"""
        return any(e.edge_type == EdgeType.LOOP_BACK for e in self.edges)

    @property
    def has_parallel(self) -> bool:
        """图中是否存在并行分叉"""
        fork_nodes = self.get_nodes_by_type(NodeType.FORK)
        return len(fork_nodes) > 0 or any(
            len(self.get_successors(n.id)) > 1
            for n in self.nodes
            if n.type not in (NodeType.ROUTER,)
        )

    # ==================== 序列化 ====================

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化的字典"""
        return self.model_dump(exclude_none=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkflowGraph':
        """从字典构建图"""
        return cls.model_validate(data)

    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        return self.model_dump_json(indent=2, exclude_none=True)

    @classmethod
    def from_json(cls, json_str: str) -> 'WorkflowGraph':
        """从 JSON 字符串构建图"""
        return cls.model_validate_json(json_str)


# ============================================================
# GraphRunConfig — 单次运行配置
# ============================================================

class GraphRunConfig(BaseModel):
    """单次图执行的运行配置"""
    
    # 控制参数
    max_iterations: Optional[int] = None       # 覆盖图默认值
    timeout_seconds: Optional[int] = None      # 全局超时
    dry_run: bool = False                      # 试运行（不真正执行 Agent）

    # 初始状态输入
    initial_state: Dict[str, Any] = Field(default_factory=dict)

    # Checkpoint
    checkpoint_enabled: bool = True
    resume_from_run_id: Optional[str] = None   # 从某次运行恢复

    # 项目上下文（用于 checkpoint 持久化和 SSE 事件路由）
    project_id: str = ""

    # 事件回调
    event_handlers: Dict[str, List[str]] = Field(default_factory=dict)

    # 元数据
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # D2/D3 — RunContext 与 PhaseSpec 快照（GraphRuntime 读写）
    phase_specs: List[Dict[str, Any]] = Field(default_factory=list)
    # P0-2: run_context 允许直接持有 RunContext 对象（而非仅 dict），保证运行中
    # CollaborationGraph.run_context 与 GraphRuntime._live_run_ctx 指向同一对象，
    # 根治 interject/pause/resume 的 A/B 身份断裂。checkpoint 序列化时 model_dump()
    # 仍输出 dict，向后兼容冷恢复。
    run_context: Any = Field(default_factory=dict)
    mode: str = "sequential"
    review_mode: bool = False

    # D-Adaptive — 自适应路由配置（mode="adaptive" 时生效）
    max_retries_per_node: int = 3       # 单节点最大重试次数（硬上限）
    max_total_iterations: int = 20      # 全局总迭代上限（硬上限）
