"""Workflow Graph Engine — Platform-Core v3.1

声明式工作流图引擎，支持：
- 条件分支 / 动态路由
- DAG 并行执行 (Fork/Merge)
- 循环控制 (Loop)
- 子图嵌套 (Subgraph)
- 人机协作 (Human-in-the-loop)
- Checkpoint 持久化

快速开始:
    from core.graph import GraphBuilder, GraphRuntime
    
    # 构建图
    graph = (
        GraphBuilder("my_pipeline")
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
    
    # 执行图
    runtime = GraphRuntime(graph)
    result = await runtime.execute(initial_state={"task": "分析市场趋势"})
    
    if result.is_success:
        print(result.state.get("final_output"))
"""

from .types import (
    # 核心类型
    WorkflowGraph,
    GraphNode,
    GraphEdge,
    GraphCondition,
    GraphState,
    ExecutionResult,
    GraphRunConfig,

    # Schema
    StateSchema,
    StateFieldDef,

    # 枚举
    NodeType,
    EdgeType,
    ConditionType,
    NodeStatus,
    GraphStatus,
    MergeStrategy,

    # 常量
    START_NODE_ID,
    END_NODE_ID,
)

from .builder import (
    GraphBuilder,
    RouteBuilder,
    create_graph,
    sequential_graph,
    parallel_graph,
    roundtable_graph,
)

from .runtime import (
    GraphRuntime,
    GraphExecutionResult,
    Scheduler,
    Router,
    NodeExecutor,
    ParallelExecutor,
    HandlerRegistry,
)

from .validators import (
    GraphValidator,
    ValidationResult,
    ValidationError,
)

__all__ = [
    # 核心类型
    "WorkflowGraph",
    "GraphNode",
    "GraphEdge",
    "GraphCondition",
    "GraphState",
    "ExecutionResult",
    "GraphRunConfig",

    # Schema
    "StateSchema",
    "StateFieldDef",

    # 枚举
    "NodeType",
    "EdgeType",
    "ConditionType",
    "NodeStatus",
    "GraphStatus",
    "MergeStrategy",

    # 常量
    "START_NODE_ID",
    "END_NODE_ID",

    # Builder
    "GraphBuilder",
    "RouteBuilder",
    "create_graph",
    "sequential_graph",
    "parallel_graph",
    "roundtable_graph",

    # Runtime
    "GraphRuntime",
    "GraphExecutionResult",
    "Scheduler",
    "Router",
    "NodeExecutor",
    "ParallelExecutor",
    "HandlerRegistry",

    # Validators
    "GraphValidator",
    "ValidationResult",
    "ValidationError",
]

__version__ = "3.1.0"
