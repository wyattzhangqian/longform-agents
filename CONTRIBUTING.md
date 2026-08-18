# Contributing

感谢你想为 agent-loom 贡献代码。这是一个 AI-native 多 Agent 协作平台，核心是 GraphRuntime 单点编排 + 统一计划契约（PlanSpec）+ 协商闭环。

## 环境要求

- Python 3.11（`.venv/bin/python`，系统 Python 3.9 遇 `X | None` 会崩）
- Node 18+（前端）
- `DEEPSEEK_API_KEY`（运行工作流需要，测试不需要）

## 快速开始

```bash
# 后端
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入你的模型 Key

# 前端
cd web && npm install && cd ..

# 启动后端（首次会自动初始化数据库）
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8110 --reload

# 启动前端（另开终端）
cd web && npm run dev
```

可选：运行 `python3 scripts/bootstrap.sh` 注入演示数据（fixtures 项目/模板/工具调用），无需 LLM Key 即可体验工具层。

## 跑测试

```bash
# 后端（~555 个测试，asyncio auto-mode）
.venv/bin/python -m pytest

# 前端（类型检查 + 测试）
cd web && npx tsc --noEmit && npx vitest run
```

## 提 PR 流程

1. Fork 仓库，从 `main` 切分支；
2. 修改 + 测试，确保全量测试通过；
3. 提交前确保全量测试通过（可选：配置 git pre-push hook 自动跑 pytest）；
4. 开 PR，描述改动动机与验证方式。

## 架构速览

- `core/run/plan_spec.py` — 统一计划契约（PlanSpec/PhasePlan），`normalize_plan` 兼容旧数据；
- `core/run/template_compiler.py` — 计划 → WorkflowGraph 编译（`compile_from_plan` 无损构建 PhaseSpec）；
- `core/orchestration/` — Planner 模块（intent/domain_prior/decomposer/validator/critic/refiner/matcher/feedback）；
- `core/graph/runtime.py` — GraphRuntime 运行时（唯一执行导演）；
- `api/routes/` — 薄控制器，委托 `core/`；
- `web/` — React 前端（Zustand + TanStack Query）。

更多架构说明见 [README.md](README.md) 与各模块头部注释。

## 约定

- **改 Agent 配置要同时改顶层列 + `model_profile.llm`**（运行时权威是 profile，只改顶层列是白改）；
- 计划契约的 `phase_id` 是唯一引用键，dependencies 不能引用 title；
- 不新增第二套执行器（执行仍由 GraphRuntime 承担）；
- 每个 PR 带对应测试；
- 日志用 loguru，格式化用 `{}` 而不是 `%s`（`%s` 不生效）；
- 用 Python 3.11（3.9 遇 `X | None` 类型标注会崩）。
