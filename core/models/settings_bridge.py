"""Settings Bridge — 启动时 / Settings 变更时 reload 平台默认模型"""

from __future__ import annotations

import json
from typing import Any, Dict

from core.logging import get_logger

_logger = get_logger("models.settings_bridge")


def _parse_setting_value(raw_value: str) -> Any:
    if not raw_value:
        return ""
    try:
        return json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        return raw_value


async def load_settings_dict() -> Dict[str, Any]:
    from core.storage.database import get_db

    db = await get_db()
    cursor = await db.execute("SELECT key, value FROM system_settings")
    rows = await cursor.fetchall()
    return {r["key"]: _parse_setting_value(r["value"]) for r in rows}


async def reload_model_defaults() -> None:
    """从 DB system_settings 合并到 ModelRegistry（env 优先项在 registry 内处理）"""
    from core.models.registry import get_model_registry

    try:
        settings = await load_settings_dict()
        get_model_registry().apply_settings(settings)
        _logger.debug("ModelRegistry 已 reload 平台默认: %s", get_model_registry().defaults_snapshot())
    except Exception as e:
        _logger.warning("ModelRegistry reload 失败（使用 env 默认）: %s", e)

    # 刷新 LLM Key 缓存（DB → config._db_key_cache，供 LLMClient 同步取用）
    # 多模态 Key 由 apply_settings 写入 registry._runtime_overrides[modality]，不走此缓存。
    try:
        import config
        val = settings.get("llm_api_key")
        if val and isinstance(val, str) and len(val) > 5:
            config._db_key_cache["DEEPSEEK"] = val
    except Exception:
        pass
