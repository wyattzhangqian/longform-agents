# Agent Loom

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)]() [![CI](https://github.com/wyattzhangqian/longform-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/wyattzhangqian/longform-agents/actions/workflows/ci.yml)

> 面向长内容与复杂任务的多 Agent 协作平台。Planner 把一句自然语言目标编译成可执行计划，多个 Agent 按阶段依赖协作执行，全程做长内容一致性、协商与质量门约束。

---

## 🛡️ 开源边界（open-core，先读这段）

本仓库是 **Agent Loom 的开源引擎层**——平台的**主框架全部在这里**：规划、图编排、Agent 协作、长内容骨架、协商闭环、质量门、领域数据驱动体系。

**没有公开的是平台产品层的「细节」**：
- 完整平台 Web 控制台与产品交互（工作台/资产管理/编排可视化）
- 平台产品上的体验优化与运营细节
- **完整的领域知识规则与内容数据**（各领域内置的质量规则、骨架模板、知识包——它们随平台产品层维护）

也就是说：**框架能力你都能在本仓库看到、跑通、扩展**；而「把一个领域做到多好」的产品化沉淀（规则调优、体验细节）属于平台产品层。

---

## 核心能力

### 🧠 Planner 规划管线

把一句自然语言目标，编译成一份可验证、可编辑、可执行的协作计划：

- **意图识别**：识别目标、交付物、任务类型，信息不足影响计划时才反问澄清
- **领域先验**：按任务领域自动套用标准流程（如网文「大纲 → 初稿 → 润色 → 审校」），不从零发明
- **自适应分解**：把目标拆成带依赖关系的阶段（`phase_id` 唯一引用）
- **自动校验与修订**：规划后自动检查步骤遗漏、依赖错误、可并行化问题，发现问题自动修订（`plan_critic` → `plan_refiner`）
- **人工修订**：生成计划后可用自然语言修改（「把调研和竞品分析并行」「加一个人工审核」），按意图重排而非推翻重来（`plan_revision`）
- **Agent 历史评分匹配**：按历史表现给阶段匹配 Agent（`agent_matcher`）

统一计划契约 **PlanSpec**（`core/run/plan_spec.py`）：`phase_id` 是唯一引用键，Planner 输出 = 前端展示 = 数据库 JSON = 运行时 PhaseSpec = 工作流图，五处结构一致、无损读写。

### 🔧 工作流图执行引擎

Agent Loom 用工作流图执行引擎调度多阶段任务：每个阶段是图中的一个节点，阶段间依赖是边，互不依赖的阶段可并行。

- **Scheduler**：拓扑排序 + 入度追踪 + 并行组检测 + 循环检测
- **Router**：4 种路由策略（AST 表达式 / HANDLER / LLM 路由 / 共识）
- **NodeExecutor**：11 种节点类型
- **ParallelExecutor**：并发执行保序
- **StateManager**：checkpoint + 乐观锁断点恢复
- 支持**串行、并行、DAG 依赖**，以及实验性的**圆桌协作**与**自适应路由**（按产出决定继续/重试/跳过/请求人工）

### 💬 多 Agent 协作

不是「A 做完丢给 B」的流水线接力，而是对话式协作：

- **Agent 间提问（`ask_peer`）**：执行中 Agent 真正调用同伴执行，非 LLM 自问自答（子图嵌套）
- **协商闭环**（`core/run/negotiation.py`）：跨阶段约束提取 → 注入下游 prompt → 质量门校验覆盖率 → 回写台账（`negotiation_ledger`），保证前后不丢约束
- **人类插话**：运行中支持人工指令干预，不用等整个流程跑完（完整交互在平台产品层）

### 🏗️ 长内容骨架

`core/skeleton/`：L4 全书设定 + L1 逐章 Brief + `current_unit` 推进。多遍规划/写作时自动锚定上下文位置，降低角色漂移与情节矛盾——这是约束机制，用于减少漂移而非对输出一致性的绝对保证。

### 🛡️ 质量门与 Gateway

- **质量门**：每个阶段产出后自动按领域规则检查（内容要求、格式、约束覆盖率），不达标就标记重做或失败——**宁可显式失败，也不假装成功**
- **三层 Gateway**（`core/gateway/`）：permission（allow/ask/deny）+ budget（Run 级 + Agent 级）+ sandbox（shell 执行隔离）
- **降级可观测**：关键降级记录 run_id/phase/node/code/fallback（RunDiagnostics），run 完成时透传 degraded 状态

### 🗂️ 领域数据驱动

领域治理走 **`quality_domains` / `quality_rules`**（DB 驱动）：领域定义、质量规则、阶段骨架都是数据，**扩展新领域写 DB 即可，不改 `core/` 代码**。内置内容制作领域的标准流程：

| 领域 | 流程 | 说明 |
|------|------|------|
| 网文小说 | 大纲 → 初稿 → 润色 → 审校 | 仅需 LLM |
| 漫画 | 编剧 → 分镜 → 线稿 → 上色 | 需图像模型 |
| 漫剧 | 配音脚本 → 分镜 → 后期 | 需图像/视频模型 |
| 音乐 | 作词 → 作曲 → 编曲 → 混音 → 母带 | 需音乐模型 |

> ⚠️ 上述领域的内置质量规则、骨架模板与知识包的**完整版属于平台产品层**；本仓库提供领域驱动机制 + `examples/run_demo.py` 的领域知识切片，可跑通完整「领域 → 规划 → Agent 匹配」链路。生产化领域内容随平台产品层演进。

---

## 当前状态

> Alpha 阶段：API 与数据模型仍在演进（见 [CHANGELOG.md](CHANGELOG.md)）。

| 能力 | 状态 | 说明 |
|------|------|------|
| Planner 任务规划 | ✅ | 自然语言 → 多阶段协作计划（PlanSpec） |
| 计划自动校验与修订 | ✅ | `plan_critic` → `plan_refiner` |
| 计划人工修订 | ✅ | 自然语言调整计划 |
| 领域先验注入 | ✅ | 按领域套用标准流程 |
| 串行 / 并行 / DAG 执行 | ✅ | 图运行时调度 |
| SSE 运行事件流 | ✅ | 规划与执行过程实时推送 |
| Agent 间提问（`ask_peer`） | ✅ | 真实调用同伴 |
| 协商台账 | ✅ | 跨阶段约束注入与校验 |
| 质量门 | ✅ | 按领域规则检查产物 |
| checkpoint 断点恢复 | ✅ | 乐观锁 + 运行历史 |
| 长内容骨架 | ✅ | L4 设定 + L1 Brief 锚定 |
| 网文流程 | ✅ | 仅需 LLM |
| 漫画 / 漫剧 / 音乐流程 | ⚠️ | 需配置对应多模态模型 |
| 圆桌协作 / 自适应路由 | 🧪 | 实验性，引擎侧已有运行入口 |
| 人类插话 | 🧪 | 引擎侧已具备，完整交互在平台产品层 |

---

## Development & Provenance

Agent Loom 以「**人设计架构、AI 辅助编码**」的方式开发。

- **设计来自真实运行**：单点图编排（GraphRuntime）、协商闭环、长内容骨架、三层 Gateway 等核心决策由人设计，并在多轮真实内容生成运行中迭代打磨而来——不是概念堆叠。
- **编码 AI 辅助**：代码编写与测试生成大量借助 AI 辅助工具完成。
- **git 历史说明**：本仓库（引擎层）自 2026-08-18 起以开源边界独立重建，历史干净、不含平台产品层与私有数据。产品层自 2026-05 起持续迭代，与其 git 历史分开维护。
- **成熟度**：Alpha。引擎层 API 与数据模型仍在演进（见 [CHANGELOG.md](CHANGELOG.md)）。

---

## 快速开始

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
- `core/gateway/` — 三层 Gateway（permission · budget · sandbox）+ DbQualityGateway
- `db/schema.sql` — 引擎数据模型基线（领域 / 质量规则 / 骨架均为数据驱动）

---

## ⚠️ 安全说明

- 默认配置不启用认证，仅适用于本机或受信环境
- 生产部署前必须：启用认证（`AUTH_ENABLED=true`）、配置独立密钥、限制 CORS 白名单、隔离工具执行环境
- Shell 工具沙箱是尽力而为的防护，不是安全隔离边界
- 不要向不可信用户开放 shell 工具 / MCP / 远程 Agent 能力

---

## 已知限制

- Alpha 阶段，API 与数据结构可能演进
- 本仓库为引擎层：完整平台控制台、产品体验优化、完整领域知识规则在平台产品层（闭源）
- 漫画/漫剧/音乐流程依赖外部多模态模型服务，需配置 API Key
- LLM 输出的事实正确性与内容一致性不做保证
- 生产多用户部署需补账号隔离与权限（平台产品层处理）

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## License

[MIT](LICENSE) © 2026 Qian Zhang
