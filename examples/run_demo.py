#!/usr/bin/env python3
"""Agent Loom 引擎跑通 demo（open-core 公开层）

克隆仓库 → 填 DEEPSEEK_API_KEY → 跑本脚本，即可看到引擎的核心链路：
    自然语言任务 → 领域识别 → 计划分解 → Agent 匹配 → 结构化计划

只依赖：一个 LLM API Key + 本仓库代码。不涉及平台产品层。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


async def seed_demo() -> None:
    """写入一个最小的 demo 领域 + 两个 demo Agent（公开的知识切片）。"""
    from core.agent.registry import get_registry
    from core.gateway.domain_registry import DomainRegistry

    # 1) demo 领域：创意写作的最小流程（阶段即 PlanSpec 的 phase 骨架）
    domains = await DomainRegistry.list_domains()
    if not any(d["id"] == "demo_writing" for d in domains):
        await DomainRegistry.create_domain(
            {
                "id": "demo_writing",
                "name": "创意写作 Demo",
                "description": "演示用的最小创作流程：大纲 → 初稿 → 润色",
                "emoji": "✍️",
                "source": "demo",
                "phase_definitions": [
                    {"phase_id": "outline", "name": "大纲", "type": "plan"},
                    {"phase_id": "draft", "name": "初稿", "type": "write"},
                    {"phase_id": "polish", "name": "润色", "type": "edit"},
                ],
                "metadata": {},
            }
        )

    # 2) 两个 demo Agent（匹配器只认 active，跨领域匹配靠 capabilities）
    registry = get_registry()
    for aid, name, role, caps in [
        ("demo_writer", "Demo 写作 Agent", "writer", ["写作", "初稿"]),
        ("demo_editor", "Demo 编辑 Agent", "editor", ["润色", "审校"]),
    ]:
        if await registry.get(aid):
            continue
        await registry.register(
            {
                "id": aid,
                "name": name,
                "type": "domain",
                "status": "active",
                "role": role,
                "emoji": "🤖",
                "capabilities": caps,
                "model": "deepseek-v4-flash",
                "temperature": 0.7,
                "max_tokens": 4096,
                "model_profile": {
                    "model_id": "deepseek-v4-flash",
                    "temperature": 0.7,
                    "max_tokens": 4096,
                    "extra": {"thinking": "disabled"},
                },
                "input_schema": {"type": "object"},
                "output_schema": {"type": "string"},
            }
        )


async def main() -> int:
    from config import DEEPSEEK_API_KEY, LLM_CONFIG

    if not (DEEPSEEK_API_KEY or LLM_CONFIG.get("api_key")):
        print("❌ 未检测到 LLM API Key。请复制 .env.example 为 .env 并填入 DEEPSEEK_API_KEY。")
        return 1

    from core.storage.database import close_db, init_db

    await init_db()
    try:
        await seed_demo()

        from core.run.natural_runner import plan_natural

        task = "写一篇 300 字的短文：一个夏天的傍晚。"
        print(f"🗒  任务：{task}\n")

        result = await plan_natural(task, save_draft=False)
        plan = result["plan"]
        phases = plan.get("phases", [])

        print(f"🧠 领域：{result.get('domain_id') or '通用'}\n")
        print("📋 生成计划（PlanSpec）：")
        for i, ph in enumerate(phases, 1):
            deps = ",".join(ph.get("dependencies") or []) or "—"
            title = ph.get("title") or ph.get("name") or ph.get("phase_id")
            agent = ph.get("agent_id") or "待匹配"
            print(f"  {i}. [{ph.get('phase_id')}] {title}  负责: {agent}  依赖: {deps}")

        print("\n✅ 规划链路跑通。")
        print("   同份 PlanSpec 可经 core/run/template_compiler.py 编译成工作流图，")
        print("   由 core/graph/runtime.py 驱动执行（本 demo 只演示到规划层）。")
    finally:
        await close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
