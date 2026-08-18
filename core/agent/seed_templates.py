"""平台内置 Agent 模板（P1-11: 从 fixtures/data/agent_templates.json 加载）

40 个专业内容创作模板（网文/漫画/漫剧/音乐/研究报告），每个含完整 system_prompt。
启动时通过 ensure_seed_templates() 自动注册到 agent_templates 表（幂等）。

P1-11: 模板定义已从本 Python 模块（原 2290 行内联）下沉到
fixtures/data/agent_templates.json，本模块只负责加载与注册。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.logging import get_logger

_logger = get_logger("seed_templates")

# 数据文件目录：core/agent/seed_templates.py → 项目根/fixtures/data
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "data"


def _load_templates() -> list:
    """从 fixtures/data/agent_templates.json 加载模板（P1-11 数据驱动）。"""
    from core.agent.template import AgentTemplate

    try:
        with open(_DATA_DIR / "agent_templates.json", encoding="utf-8") as f:
            raw = json.load(f)
        templates = [AgentTemplate.model_validate(t) for t in raw]
        _logger.info("模板种子加载: {} 个（fixtures/data/agent_templates.json）", len(templates))
        return templates
    except Exception as e:
        _logger.error(
            "P1-11 模板种子加载失败 {}: {} —— 启动将缺内置模板",
            _DATA_DIR / "agent_templates.json", e,
        )
        return []


SEED_TEMPLATES = _load_templates()


async def ensure_seed_templates(registry=None) -> int:
    """将 platform 模板注册到 agent_templates 表（幂等）

    在数据库初始化时调用，确保所有内置模板可用。
    已存在的模板（相同 ID）会被跳过；平台模板以数据文件定义为准同步更新。

    Returns:
        本次新注册的模板数量
    """
    from core.agent.template import get_template_registry

    if registry is None:
        registry = get_template_registry()

    count = 0
    for tmpl in SEED_TEMPLATES:
        existing = await registry.get(tmpl.id)
        if existing is not None:
            # platform 模板以数据文件定义为准同步更新（register 覆盖）：
            # 否则 ensure_seed_templates 幂等跳过会导致对数据文件的修改永不生效
            await registry.register(tmpl)
            continue
        await registry.register(tmpl)
        count += 1

    return count
