# Contributing

感谢你想为 **Agent Loom 引擎层**贡献代码。这是一个 AI-native 多 Agent 协作框架，核心是领域知识与规则体系 + Planner 统一计划契约（PlanSpec）+ 工作流图执行 + 协商闭环。

> 本仓库是开源**引擎层**（open-core）。平台产品层（完整 Web 控制台、产品体验、领域知识规则）为闭源，不在本仓库——贡献请聚焦引擎能力本身。

## 环境要求

- Python 3.11（`.venv/bin/python`，系统 Python 3.9 遇 `X | None` 会崩）
- `DEEPSEEK_API_KEY`（运行 demo 需要，纯跑测试不需要）

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY

# 跑 demo：自然语言 → 领域识别 → 计划分解 → Agent 匹配
.venv/bin/python examples/run_demo.py
```

## 跑测试

```bash
# 引擎测试（规划 / 图编排 / 协商 / 质量门 / 骨架等）
.venv/bin/python -m pytest tests/ -x -q
```

## 提 PR 流程

1. Fork 仓库，从 `oss`（默认分支）切分支；
2. 修改 + 测试，确保改动相关测试通过；
3. 开 PR，描述改动动机与验证方式。

## 架构速览

- `core/run/plan_spec.py` — 统一计划契约（PlanSpec/PhasePlan），`normalize_plan` 兼容旧数据；
- `core/run/template_compiler.py` — 计划 → WorkflowGraph 编译（`compile_from_plan` 无损构建 PhaseSpec）；
- `core/orchestration/` — Planner 管线（intent/domain_prior/decomposer/validator/critic/refiner/revision/matcher）；
- `core/graph/runtime.py` — 工作流图执行引擎（唯一执行导演）；
- `core/agent/base.py` — BaseAgent 执行（ContextBuilder → tool loop → Memory，含 `ask_peer`）；
- `core/run/negotiation.py` — 协商闭环；`core/skeleton/` — 长内容骨架；`core/gateway/` — 三层 Gateway + 质量门。

更多架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与各模块头部注释。

## 约定

- 计划契约的 `phase_id` 是唯一引用键，dependencies 不能引用 title；
- 不新增第二套执行器（执行仍由工作流图执行引擎承担）；
- 每个 PR 带对应测试；
- 日志用 loguru，格式化用 `{}` 而不是 `%s`（`%s` 不生效）；
- 用 Python 3.11（3.9 遇 `X | None` 类型标注会崩）；
- 不要在本仓库引入平台产品层依赖（`web` / `api` / 真实 LLM 客户端逻辑）；
- 不提交密钥与环境文件。
