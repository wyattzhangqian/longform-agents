# Changelog

Longform Agents 的变更记录。项目自 2026-05 起经真实迭代开发；引擎层仓库于 2026-08-18 按开源边界独立重建，
历史干净、不含平台产品层与私有数据（见 [README](README.md) 的「开源边界」与 Development & Provenance）。

## [v0.1.0-alpha] — 2026-08-18

引擎层首个开源版本，标记当前可用状态。

### 核心能力（2026-05~08 真实迭代沉淀）

- **Planner 规划管线**：意图识别 → 领域先验 → 自适应分解 → 确定性校验 → 语义批评（`plan_critic`）→ 修订（`plan_refiner`）→ Agent 历史评分匹配（`agent_matcher`）；支持自然语言人工修订（`plan_revision`）
- **统一计划契约 PlanSpec**：`phase_id` 唯一引用键，Planner 输出 = 前端展示 = 数据库 JSON = 运行时 PhaseSpec = 工作流图，五处结构一致、字段无损
- **工作流图执行引擎**：单点编排，Scheduler 拓扑排序 + 并行组 + 循环检测；Router 4 策略；11 种节点；checkpoint 乐观锁断点恢复
- **协商闭环**：Agent 执行中 `ask_peer` 真实调用同伴、跨阶段约束台账（`negotiation_ledger`）、质量门覆盖率校验、人类插话
- **长内容骨架**：L4 全书设定 + L1 逐章 Brief + `current_unit` 推进，多遍规划控制角色漂移与情节矛盾
- **领域数据驱动**：`quality_domains` / `quality_rules` DB 驱动（网文/漫画/漫剧/音乐流程），扩展新领域写 DB 即可
- **三层 Gateway**：permission · budget · sandbox，降级可观测（RunDiagnostics，`run_degraded` 显式失败不伪装成功）
- **SSE 事件流**：环形缓冲 + Last-Event-ID 断线重连

### 工程化

- 引擎测试（规划 / 图编排 / 协商 / 质量门 / 骨架等核心链路）
- CI（GitHub Actions，引擎测试）；MIT License；可跑通 demo（`examples/run_demo.py`）

### 开源边界（2026-08-18）

- 按 open-core 边界重建：**主框架（引擎层）全部开源**；平台产品层（完整 Web 控制台、体验优化、完整领域知识规则与内容数据）保持私有
- 公开版用最小 LLM 客户端桩（接口公开、实现私有），平台真实客户端不在此仓库
- 仓库历史自本版本起独立，不含产品层与私有数据

---

*后续变更将按 `[语义化版本] — 日期` 追加。*
