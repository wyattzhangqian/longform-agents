# Changelog

Agent Loom 的变更记录。项目自 2026-05 起经真实迭代开发，公开仓库于 2026-08-14 为移除个人开发数据而重建（见 [README](README.md) 的 Development & Provenance），故公开 git 历史仅覆盖 2026-08-14 之后。

## [v0.1.0-alpha] — 2026-08-18

首个开源发布 tag，标记当前可用状态。

### 核心能力（2026-05~08 真实迭代沉淀）

- **Planner 规划管线**：意图识别 → 领域先验 → 自适应分解 → 确定性校验 → 语义批评（`plan_critic`）→ 修订（`plan_refiner`）→ Agent 匹配；支持自然语言人工修订计划
- **图执行引擎**：GraphRuntime 单点编排，串行 / 并行 / DAG 依赖 / 圆桌 / 自适应路由，checkpoint 断点恢复与乐观锁
- **协商闭环**：Agent 执行中 `ask_peer` 向同伴提问、跨阶段约束台账（`negotiation_ledger`）、质量门覆盖率校验、人类插话
- **长内容骨架**：全书设定持久化 + 逐章 Brief 锚定，多遍规划控制角色漂移与情节矛盾
- **领域知识库**：JSON 数据驱动（网文/漫画/漫剧/音乐），规划 / 执行 / 质量三阶段自动注入，扩展新领域无需改代码
- **三层 Gateway**：permission · budget · sandbox · quality，Fernet 加密凭据，降级可观测（RunDiagnostics）
- **SSE 事件流**：环形缓冲 + Last-Event-ID 断线重连

### 工程化

- 后端测试 650+（安全 / 容错 / 契约 / 集成），前端类型检查 + 单元测试
- CI（GitHub Actions）、Docker / Docker Compose、Python SDK（`sdk/`）
- MIT License、CONTRIBUTING、公开 README

### 开源卫生（2026-08-18）

- 移出开发过程材料（本地运维脚本、内部治理/设计文档），公开仓库只保留技术结果
- 私有忽略规则迁至本机 `.git/info/exclude`，`.gitignore` 仅保留共享规则
- 移除代码注释中的迭代过程标记（Round / 轮次）
- 修复 docker-compose 明文默认密码（未设置时 fail-closed）
- README 增加 Development & Provenance 说明
