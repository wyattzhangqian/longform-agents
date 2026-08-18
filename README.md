# Agent Loom

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)]() [![CI](https://github.com/wyattzhangqian/agent-loom/actions/workflows/ci.yml/badge.svg)](https://github.com/wyattzhangqian/agent-loom/actions/workflows/ci.yml)

> 面向长内容与复杂任务的多 Agent 协作**引擎**。把一个自然语言目标编译成可执行计划，再由多个 Agent 按阶段依赖协作执行，全程做长内容一致性、协商与质量门约束。

---

## 开源边界（open-core）

本仓库是 Agent Loom 的**开源引擎层**：规划、图编排、Agent 协作、长内容骨架、协商闭环与质量门。

完整产品层（Web 控制台、平台服务、模型与凭证管理、领域内容数据）保持私有，不在本仓库。

---

## 引擎核心能力

### 🧠 Planner 规划管线

把一句自然语言目标编译成一份可验证、可编辑、可执行的协作计划：

- **意图识别** → **领域先验**（按任务领域套用标准流程，不从零发明）→ **自适应分解**（拆成带依赖的阶段）
- **确定性校验**：自动检查步骤遗漏、依赖错误、可并行化（`plan_validator`）
- **语义批评与修订**：`plan_critic` → `plan_refiner`，发现问题自动修订
- **用户指令修订**：生成后用自然语言改计划（「把调研和竞品分析并行」），按意图重排而非推翻重来（`plan_revision`）
- **Agent 历史评分匹配**：按历史表现给阶段匹配 Agent（`agent_matcher`）

统一计划契约 **PlanSpec**（`core/run/plan_spec.py`）：`phase_id` 是唯一引用键，Planner 输出 = 前端展示 = 数据库 JSON = 运行时 PhaseSpec = 工作流图，五处结构一致。

### ⚙️ 图运行时（GraphRuntime）

`core/graph/runtime.py` 是唯一执行导演：

- **Scheduler**：拓扑排序 + 入度追踪 + 并行组检测 + 循环检测
- **Router**：4 种路由策略（AST 表达式 / HANDLER / LLM 路由 / 共识）
- **NodeExecutor**：11 种节点类型
- **ParallelExecutor**：Semaphore + 并发执行保序
- **StateManager**：checkpoint + 乐观锁断点恢复

### 💬 多 Agent 协作

- **Agent 间提问（`ask_peer`）**：执行中 Agent 真正调用同伴执行，非 LLM 自问自答（子图嵌套）
- **协商闭环**（`core/run/negotiation.py`）：跨阶段约束提取 → 注入下游 prompt → 质量门校验覆盖率 → 回写台账，保证前后不丢约束

### 🏗️ 长内容骨架

`core/skeleton/`：L4 全书设定 + L1 逐章 Brief + `current_unit` 推进，逐章写作自动锚定上下文位置，降低角色漂移与情节矛盾。

### 🛡️ 质量门与 Gateway

- **质量门**：每个阶段产出后自动按领域规则检查（内容要求、格式、约束覆盖率），不达标就标记重做或失败——不是摆设，是「宁可显式失败也不假装成功」的关卡
- **三层 Gateway**（`core/gateway/`）：permission（allow/ask/deny）+ budget（Run 级 + Agent 级）+ sandbox（shell 执行隔离）

### 🗂️ 领域数据驱动

领域治理走 **`quality_domains` / `quality_rules`**（DB 驱动）：领域定义、质量规则、阶段骨架都是数据，**扩展新领域写 DB 即可，不改 `core/` 代码**。

---

## 当前状态

> Alpha 阶段：API 与数据模型仍在演进（见 [CHANGELOG.md](CHANGELOG.md)）。

| 能力 | 状态 | 说明 |
|------|------|------|
| Planner 规划（意图→领域→分解→校验→修订→匹配） | ✅ | 统一 PlanSpec 契约 |
| 串行 / 并行 / DAG 依赖执行 | ✅ | 图运行时调度 |
| Agent 间提问（`ask_peer`） | ✅ | 真实调用同伴执行 |
| 协商台账 | ✅ | 跨阶段约束注入与校验 |
| 质量门 | ✅ | 按领域质量规则检查产物 |
| checkpoint 断点恢复 | ✅ | 乐观锁 + 运行历史 |
| 长内容骨架 | ✅ | L4 设定 + L1 Brief 锚定 |
| 领域数据驱动 | ✅ | `quality_domains` DB 体系 |
| 人类插话 / 圆桌 / 自适应路由 | 🧪 | 引擎侧已具备运行入口，完整交互在平台产品层 |

---

## 快速开始

本仓库是**引擎层**（open-core 公开部分）。完整控制台在闭源的平台产品层。

```bash
# 1. 装依赖
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY

# 2. 跑一个 demo：自然语言 → 领域识别 → 计划分解 → Agent 匹配
.venv/bin/python examples/run_demo.py

# 3. 跑引擎测试（自动初始化数据库）
.venv/bin/python -m pytest tests/ -x -q
```

想看整体结构，读 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；想最快看到概念真实运转，跑 `examples/run_demo.py`。接入你自己的应用时，从 `core/orchestration/planner.py`（规划入口）与 `core/graph/runtime.py`（图运行时）入手。

---

## 架构概览

```
引擎层 (core/)       Planner 管线 · GraphRuntime · Agent 基座 · 骨架 · 协商 · Gateway
基础设施             SQLite · SSE EventBus · LLM 客户端 · Auth · Vault
```

关键模块：

- `core/orchestration/` — Planner 管线（intent → domain_prior → decomposer → validator → critic/refiner → revision → agent_matcher）
- `core/graph/runtime.py` — GraphRuntime 图运行时（唯一执行导演）
- `core/agent/base.py` — BaseAgent 执行（ContextBuilder → tool loop → Memory，含 `ask_peer`）
- `core/run/plan_spec.py` / `phase_spec.py` / `template_compiler.py` — 计划契约 + 计划→图编译
- `core/run/run_context.py` — RunContext 状态真相（checkpoint + 乐观锁 + 降级追踪）
- `core/run/negotiation.py` — 协商闭环
- `core/skeleton/` — 长内容骨架系统
- `core/gateway/` — 三层 Gateway（permission · budget · sandbox）
- `db/schema.sql` — 引擎数据模型基线（领域 / 质量规则 / 骨架均为数据驱动）

---

## ⚠️ 安全说明

- 默认配置不启用认证，仅适用于本机或受信环境
- 生产部署前必须：启用认证（`AUTH_ENABLED=true`）、配置独立密钥、限制 CORS 白名单、隔离工具执行环境
- Shell 工具沙箱是尽力而为的防护，不是安全隔离边界

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## License

[MIT](LICENSE) © 2026 Qian Zhang
