# Longform Agents 架构

> 本仓库公开的是**引擎层**的整体架构。平台产品层（Web 控制台、HTTP 产品接口、模型/凭证管理、领域内容数据）为闭源产品层，不在本仓库——架构图上以「闭源」标注，概念位置公开。

## 分层总览

```
配置层（DB 驱动）      agents · quality_domains · quality_rules · model_catalog
      │
Planner 体系          core/orchestration/ + core/run/plan_spec.py
      │                意图 → 领域先验 → 分解 → 校验 → 批评/修订 → Agent 匹配 → PlanSpec
      ▼
编译                  core/run/template_compiler.py    PlanSpec → WorkflowGraph（PhaseSpec + strategy 贯通）
      │
执行运行时            core/graph/runtime.py + core/agent/base.py + core/run/run_context.py + core/run/negotiation.py
      │                Scheduler / Router / NodeExecutor / ParallelExecutor / StateManager
      ▼
支撑系统              core/skeleton/（长内容骨架）· core/gateway/（三层 Gateway + 质量门）· core/run/diagnostics.py（降级追踪）
      │
基础设施              SQLite(WAL) · SSE EventBus · LLM 客户端 · Auth · Vault
```

## 核心数据流

```
自然语言任务
  → Planner 管线（intent → domain_prior → decomposer → plan_validator → plan_critic/refiner → plan_revision → agent_matcher）
  → PlanSpec（统一计划契约：phase_id 是唯一引用键；字段无损，旧计划兼容读取）
  → template_compiler.compile_from_plan（无损构建 PhaseSpec + strategy 贯通）
  → WorkflowGraph（阶段=节点，依赖=边；可并行阶段被 Scheduler 并行组调度）
  → 工作流图执行引擎（拓扑排序 + 入度追踪 + 循环检测；Router 4 策略：AST/HANDLER/LLM/共识）
  → BaseAgent 执行（ContextBuilder → tool loop → Memory；_ask_peer 子图真实调用同伴）
      · 每阶段产物经 DbQualityGateway 按领域规则校验
      · 跨阶段约束经 negotiation_ledger 提取→注入→覆盖率校验→回写
      · 长内容任务经骨架系统锚定上下文（L4 全书设定 + L1 逐章 Brief + current_unit）
  → 产物 / checkpoint（乐观锁断点恢复）/ 降级诊断（run_degraded 显式失败不伪装成功）
```

## 关键模块

| 模块 | 职责 |
|------|------|
| `core/orchestration/` | Planner 管线：intent（意图）· domain_prior（领域先验）· decomposer（自适应分解）· plan_validator（确定性校验）· plan_critic/refiner（语义批评/修订）· plan_revision（用户指令修订）· agent_matcher（历史评分匹配） |
| `core/run/plan_spec.py` | 统一计划契约 `PlanSpec`/`PhasePlan` + `normalize_plan`（旧数据兼容） |
| `core/run/phase_spec.py` | 运行时阶段契约 `PhaseSpec`（质量/知识/工具策略） |
| `core/run/template_compiler.py` | 计划→工作流图编译；缓存 key 覆盖完整 spec + strategy |
| `core/graph/runtime.py` | 工作流图执行引擎（唯一执行导演）：Scheduler / Router / NodeExecutor(11 节点) / ParallelExecutor / StateManager |
| `core/agent/base.py` | BaseAgent：ContextBuilder → tool loop → Memory；`_ask_peer` 子图协作 |
| `core/run/run_context.py` | RunContext 单一真相（Pydantic + checkpoint 乐观锁 + 降级记录） |
| `core/run/negotiation.py` | 协商闭环：约束提取 → 注入 → 覆盖率校验 → 回写台账 |
| `core/skeleton/` | 长内容骨架：L4 全局设定 + L1 逐章 Brief + current_unit 推进 |
| `core/gateway/` | 三层 Gateway（permission / budget / sandbox）+ DbQualityGateway（质量规则并行检查） |
| `core/run/diagnostics.py` | RunDiagnostics 降级追踪（run_id/phase_id/node_id/code/fallback_used） |

## 数据模型（db/schema.sql）

核心表：`agents`（含 `model_profile` 三维度配置）· `quality_domains`/`quality_rules`（领域治理，DB 驱动）· `projects`/`plans`（PlanSpec 落库）· `run_checkpoints`/`run_locks`（断点恢复 + 乐观锁）· `artifacts`（产物，按 run_id 归属）。

领域治理是**数据驱动**的：新增领域 = 写 `quality_domains` + `quality_rules`（JSON/DB），不改 `core/` 代码。Demo 见 `examples/run_demo.py`（演示最小领域 + Agent 切片 + 自然语言规划链路）。

## 可靠性语义

- 质量门超时 → 显式 `passed=False` + failure_code，不伪装通过
- Autofix 超时/异常 → `passed=False` + CRITICAL 降级
- 产物检查三类状态：PRESENT / MISSING / CHECK_FAILED（按 run_id 归属校验）
- 核心原则：**宁可 run 显式 FAILED，也不伪装 COMPLETED 但残缺**
