"""ModelRegistry — 平台模型注册表 + 默认模型（env + DB settings 合并）"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core.models.types import Modality, ModelSpec, PLATFORM_DEFAULT

_registry: Optional["ModelRegistry"] = None


class ModelRegistry:
    """全局模型 catalog 与平台默认 binding"""

    def __init__(self) -> None:
        self._specs: Dict[str, ModelSpec] = {}
        self._defaults: Dict[Modality, str] = {}
        self._runtime_overrides: Dict[str, Any] = {}  # api_key, base_url 等
        self._seed_from_config()

    def _seed_from_config(self) -> None:
        from config import (
            AVAILABLE_IMAGE_MODELS,
            AVAILABLE_LLM_MODELS,
            AVAILABLE_MUSIC_MODELS,
            AVAILABLE_VIDEO_MODELS,
            DEFAULT_IMAGE_MODEL,
            DEFAULT_LLM_JUDGE_MODEL,
            DEFAULT_LLM_MODEL,
            DEFAULT_MUSIC_MODEL,
            DEFAULT_VIDEO_MODEL,
            DEEPSEEK_API_KEY,
            DEEPSEEK_BASE_URL,
            LLM_CONFIG,
            OPENAI_API_KEY,
            SUNO_API_KEY,
            LLM_JUDGE_MODEL,
            VOLCENGINE_API_KEY,
        )

        for mid, info in AVAILABLE_LLM_MODELS.items():
            self._specs[mid] = ModelSpec(
                id=mid,
                modality="llm",
                provider=info.get("provider", "deepseek"),
                label=info.get("label", mid),
                description=info.get("description", ""),
                base_url=info.get("base_url", DEEPSEEK_BASE_URL),
                default_temperature=float(LLM_CONFIG.get("temperature", 0.7)),
                default_max_tokens=int(LLM_CONFIG.get("max_tokens", 4096)),
                api_key_env="DEEPSEEK_API_KEY",
                fallback=info.get("fallback", ""),
            )

        for mid, info in AVAILABLE_IMAGE_MODELS.items():
            self._specs[mid] = ModelSpec(
                id=mid,
                modality="image",
                provider=info.get("provider", "volcengine"),
                label=info.get("label", mid),
                description=info.get("description", ""),
                base_url=info.get("base_url", ""),
                api_key_env=info.get("api_key_env", "VOLCENGINE_API_KEY"),
            )

        for mid, info in AVAILABLE_VIDEO_MODELS.items():
            self._specs[mid] = ModelSpec(
                id=mid,
                modality="video",
                provider=info.get("provider", "volcengine"),
                label=info.get("label", mid),
                description=info.get("description", ""),
                base_url=info.get("base_url", ""),
                api_key_env=info.get("api_key_env", "VOLCENGINE_API_KEY"),
            )

        for mid, info in AVAILABLE_MUSIC_MODELS.items():
            self._specs[mid] = ModelSpec(
                id=mid,
                modality="music",
                provider=info.get("provider", "suno"),
                label=info.get("label", mid),
                description=info.get("description", ""),
                base_url=info.get("base_url", ""),
                api_key_env=info.get("api_key_env", "SUNO_API_KEY"),
            )

        self._defaults = {
            "llm": normalize_llm_model_id(LLM_CONFIG.get("model") or DEFAULT_LLM_MODEL),
            "judge": normalize_llm_model_id(LLM_JUDGE_MODEL or DEFAULT_LLM_JUDGE_MODEL),
            "image": DEFAULT_IMAGE_MODEL,
            "video": DEFAULT_VIDEO_MODEL,
            "music": DEFAULT_MUSIC_MODEL,
        }
        self._runtime_overrides = {
            "llm_api_key": DEEPSEEK_API_KEY or "",
            "llm_base_url": DEEPSEEK_BASE_URL,
            # 启动时 env 注入的供应商凭证（按 provider 缓存，供模型按 provider 兜底）
            "VOLCENGINE": VOLCENGINE_API_KEY or "",
            "OPENAI": OPENAI_API_KEY or "",
            "SUNO": SUNO_API_KEY or "",
            # Settings 按模态录入的通用凭证（不绑供应商；api_key_for 末尾兜底）
            "image": "",
            "video": "",
            "music": "",
        }

    async def _load_catalog_from_db(self) -> bool:
        """从 DB 加载 catalog。返回 True 表示 DB 中有数据。"""
        try:
            from core.storage.database import get_db
            db = await get_db()
            cursor = await db.execute("SELECT * FROM model_catalog WHERE enabled = 1")
            rows = await cursor.fetchall()
            if not rows:
                return False
            for row in rows:
                r = dict(row)
                self._specs[r["id"]] = ModelSpec(
                    id=r["id"],
                    modality=r["modality"],
                    provider=r["provider"],
                    label=r["label"],
                    description=r["description"],
                    base_url=r["base_url"],
                    api_key_env=r["api_key_env"],
                    default_temperature=r.get("default_temperature", 0.7),
                    default_max_tokens=r.get("default_max_tokens", 4096),
                    fallback=r.get("fallback", ""),
                )
            return True
        except Exception:
            return False

    async def _seed_catalog_to_db(self) -> None:
        """将 env var catalog 写入 DB（幂等，INSERT OR IGNORE）"""
        from core.storage.database import get_db
        db = await get_db()
        for mid, spec in self._specs.items():
            await db.execute(
                """INSERT OR IGNORE INTO model_catalog
                   (id, modality, provider, label, description, base_url, api_key_env,
                    default_temperature, default_max_tokens, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'seed')""",
                (mid, spec.modality, spec.provider, spec.label, spec.description,
                 spec.base_url, spec.api_key_env, spec.default_temperature, spec.default_max_tokens))
        await db.commit()

    def apply_settings(self, settings: Dict[str, Any]) -> None:
        """合并 system_settings（DB）；api_key 仍以 env 优先"""
        if settings.get("llm_model"):
            self._defaults["llm"] = normalize_llm_model_id(str(settings["llm_model"]))
        if settings.get("llm_judge_model"):
            self._defaults["judge"] = normalize_llm_model_id(str(settings["llm_judge_model"]))
        if settings.get("image_model"):
            self._defaults["image"] = str(settings["image_model"])
        if settings.get("video_model"):
            self._defaults["video"] = str(settings["video_model"])
        if settings.get("music_model"):
            self._defaults["music"] = str(settings["music_model"])
        if settings.get("llm_base_url") and not os.getenv("DEEPSEEK_BASE_URL"):
            self._runtime_overrides["llm_base_url"] = str(settings["llm_base_url"])
        # DB API Key 仅当对应 env 未设置时生效。
        # LLM 按 provider（DeepSeek）；多模态按模态录入，不绑供应商——
        # 用户用任意模型时填对应模态的通用 Key 即可，模型自带 api_key_env 时优先读 env。
        if settings.get("llm_api_key") and not os.getenv("DEEPSEEK_API_KEY"):
            self._runtime_overrides["llm_api_key"] = str(settings["llm_api_key"])
        for modality in ("image", "video", "music"):
            val = settings.get(f"{modality}_api_key")
            if val:
                self._runtime_overrides[modality] = str(val)

    def get_spec(self, model_id: str) -> Optional[ModelSpec]:
        if not model_id or model_id == PLATFORM_DEFAULT:
            return None
        return self._specs.get(model_id)

    def get_default_id(self, modality: Modality) -> str:
        return self._defaults.get(modality, "")

    def list_by_modality(self, modality: Modality) -> List[ModelSpec]:
        return [s for s in self._specs.values() if s.modality == modality]

    def resolve_model_id(self, binding_model_id: str, modality: Modality) -> str:
        if not binding_model_id or binding_model_id == PLATFORM_DEFAULT:
            return self.get_default_id(modality)
        if modality == "llm":
            # 健壮性（2026-08-08）：LLM 绑定模型必须归一化——Agent 若配了不存在的
            # 模型（如 claude-sonnet-4-6）会 fallback 到默认，否则 LLM 调用 400
            # 直接导致 phase/run 失败。image/video/music 保留原样（第三方 provider）。
            return normalize_llm_model_id(binding_model_id)
        return binding_model_id

    def api_key_for(self, spec: ModelSpec) -> str:
        if spec.modality == "llm":
            env_key = os.getenv("DEEPSEEK_API_KEY", "")
            if env_key:
                return env_key
            return self._runtime_overrides.get("llm_api_key", "")
        if spec.modality in ("image", "video", "music"):
            # 优先级：模型自带 api_key_env → env → provider 缓存 → 模态通用 Key（Settings 录入，不绑供应商）
            env_name = spec.api_key_env
            if env_name:
                env_value = os.getenv(env_name, "")
                if env_value:
                    return env_value
            provider_key = (spec.provider or "").upper()
            if provider_key:
                pv = self._runtime_overrides.get(provider_key, "")
                if pv:
                    return pv
            return self._runtime_overrides.get(spec.modality, "")
        return ""

    def base_url_for(self, spec: ModelSpec) -> str:
        if spec.modality == "llm":
            return os.getenv("DEEPSEEK_BASE_URL", "") or self._runtime_overrides.get("llm_base_url", spec.base_url)
        return spec.base_url

    def defaults_snapshot(self) -> Dict[str, str]:
        return dict(self._defaults)

    def catalog_for_api(self) -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = {"llm": [], "image": [], "video": [], "music": []}
        for spec in self._specs.values():
            if spec.modality not in out:
                continue
            out[spec.modality].append({
                "value": spec.id,
                "label": spec.label or spec.id,
                "description": spec.description,
                "provider": spec.provider,
                "modality": spec.modality,
            })
        return out


def normalize_llm_model_id(model: Optional[str]) -> str:
    from config import normalize_llm_model
    return normalize_llm_model(model)


def get_model_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry
