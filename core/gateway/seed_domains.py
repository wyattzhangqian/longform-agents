"""平台内置领域 + 规则种子数据（6 领域，60 条规则）

P1-11: 种子数据已下沉到 fixtures/data/*.json（从 core/ 代码移出），
本模块只负责加载，不再内联 450 行领域/规则字面量。

启动时由 seed_platform_domains() 写入 DB（幂等）。
所有种子数据 source='platform'，不可编辑/删除。

领域清单（fixtures/data/quality_domains.json）:
  - general         通用层（8 条）
  - comic_drama     漫剧（12 条）
  - comic_static    漫画（10 条）
  - music_production 音乐制作（10 条）
  - web_novel       网文小说（10 条）
  - research_report 研究报告（10 条）
"""

from __future__ import annotations

import json
from pathlib import Path

from core.logging import get_logger

_logger = get_logger("seed_domains")

# 数据文件目录：core/gateway/seed_domains.py → 项目根/fixtures/data
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "data"


def _load_json(name: str, fallback: list) -> list:
    """从 fixtures/data 加载种子数据；加载失败回退空列表并明确报错。"""
    try:
        with open(_DATA_DIR / name, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _logger.error(
            "P1-11 种子数据加载失败 {}（路径 {}）: {} —— 启动将缺领域/规则",
            name, _DATA_DIR / name, e,
        )
        return fallback


PLATFORM_DOMAINS: list = _load_json("quality_domains.json", [])
PLATFORM_RULES: list = _load_json("quality_rules.json", [])
