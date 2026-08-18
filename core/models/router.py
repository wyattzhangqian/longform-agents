"""ModelRouter — 解析 Agent 绑定 → 运行时 client 配置"""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

import ipaddress
import logging
import socket
import urllib.parse

from core.models.registry import get_model_registry
from core.models.types import Modality, ModelBinding, PLATFORM_DEFAULT

if TYPE_CHECKING:
    from core.agent.types import AgentDefinition, AgentModelProfile

_logger = logging.getLogger(__name__)


def is_safe_base_url(url: str) -> bool:
    """自定义 base_url 安全校验：仅允许 http(s) 且解析到公网 IP。

    防 SSRF/密钥外泄（2026-08-18）：恶意 agent 配置可通过 custom_base_url 把含
    API Key 的请求发往任意地址；校验目标必须是公网可达的 http(s) 端点，拒绝
    私网/环回/链路本地/元数据/保留地址（127.0.0.1、169.254.169.254、10.* 等）。
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        if not infos:
            return False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


class ModelRouter:
    """合并 platform default + Agent model_profile → 可传给 LLMClient / ImageClient 的配置"""

    @staticmethod
    def resolve_binding(
        binding: Optional[ModelBinding],
        modality: Modality,
        *,
        legacy_model: str = "",
        legacy_temperature: Optional[float] = None,
        legacy_max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        registry = get_model_registry()
        bind = binding or ModelBinding()

        # 兼容旧字段：binding 为 platform_default 且 legacy 有值
        model_id = bind.model_id
        if model_id == PLATFORM_DEFAULT and legacy_model:
            model_id = legacy_model

        resolved_id = registry.resolve_model_id(model_id, modality)
        spec = registry.get_spec(resolved_id)

        temperature = bind.temperature if bind.temperature is not None else legacy_temperature
        max_tokens = bind.max_tokens if bind.max_tokens is not None else legacy_max_tokens

        if spec:
            if temperature is None:
                temperature = spec.default_temperature
            if max_tokens is None:
                max_tokens = spec.default_max_tokens
            cfg: Dict[str, Any] = {
                "model": resolved_id,
                "provider": bind.provider or spec.provider,
                "base_url": registry.base_url_for(spec),
                "api_key": registry.api_key_for(spec),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "modality": modality,
                "fallback_model": spec.fallback or "",
            }
        else:
            # 自定义 model id（不在 catalog）— 优先使用 extra 中的自定义配置
            extra = bind.extra or {}
            custom_model_id = extra.get("custom_model_id", "")
            custom_base_url = extra.get("custom_base_url", "")
            custom_api_key = ""

            # 安全：自定义 base_url 必须解析到公网 http(s)，否则忽略整个自定义配置
            # （防恶意配置把含 API Key 的请求发往任意地址导致密钥外泄，2026-08-18）
            if custom_base_url and not is_safe_base_url(custom_base_url):
                _logger.warning("自定义 base_url 校验失败，忽略该自定义模型配置（防密钥外泄）: %s", custom_base_url)
                custom_base_url = ""
            else:
                # API Key: 优先用 api_key_ref 引用平台 Key（仅白名单引用名），兼容旧 custom_api_key
                api_key_ref = extra.get("api_key_ref", "")
                if api_key_ref:
                    from config import get_api_key_for_ref
                    custom_api_key = get_api_key_for_ref(api_key_ref)
                else:
                    # 兼容旧数据：直接存的 custom_api_key
                    custom_api_key = extra.get("custom_api_key", "")

            # 如果 extra 中有 custom_model_id，用它替换 sentinel 值
            effective_model = custom_model_id if custom_model_id else resolved_id

            if custom_base_url or custom_api_key:
                # 用户手动填写的自定义模型配置
                cfg = {
                    "model": effective_model,
                    "provider": bind.provider or modality,
                    "base_url": custom_base_url,
                    "api_key": custom_api_key,
                    "temperature": temperature or extra.get("default_temperature", 0.7),
                    "max_tokens": max_tokens or extra.get("default_max_tokens", 4096),
                    "modality": modality,
                }
            else:
                # 兜底：使用 LLM 全局配置（仅 LLM 场景合理）
                from config import LLM_CONFIG
                cfg = {
                    "model": effective_model,
                    "provider": bind.provider or LLM_CONFIG.get("provider", "deepseek"),
                    "base_url": registry._runtime_overrides.get("llm_base_url", LLM_CONFIG.get("base_url", "")),
                    "api_key": registry._runtime_overrides.get("llm_api_key", LLM_CONFIG.get("api_key", "")),
                    "temperature": temperature if temperature is not None else LLM_CONFIG.get("temperature", 0.7),
                    "max_tokens": max_tokens if max_tokens is not None else LLM_CONFIG.get("max_tokens", 4096),
                    "modality": modality,
                }

        # 仅允许 extra 覆盖非敏感字段，防止 api_key/base_url/model/provider 被劫持
        _SAFE_EXTRA_KEYS = {"temperature", "max_tokens", "top_p", "top_k",
                            "timeout", "max_retries", "headers", "modality",
                            "thinking"}
        extra_safe = {k: v for k, v in (bind.extra or {}).items()
                      if k in _SAFE_EXTRA_KEYS or k.startswith("x-")}
        cfg.update(extra_safe)
        return cfg

    @staticmethod
    def resolve_agent(agent: "AgentDefinition", modality: Modality = "llm") -> Dict[str, Any]:
        profile: AgentModelProfile = agent.model_profile
        binding: Optional[ModelBinding] = None
        if modality == "llm":
            binding = profile.llm
        elif modality == "image":
            binding = profile.image
        elif modality == "video":
            binding = profile.video
        elif modality == "music":
            binding = profile.music
        elif modality == "judge":
            binding = profile.judge or profile.llm

        return ModelRouter.resolve_binding(
            binding,
            modality,
            legacy_model=agent.model if modality == "llm" else "",
            legacy_temperature=agent.temperature if modality == "llm" else None,
            legacy_max_tokens=agent.max_tokens if modality == "llm" else None,
        )


def resolve_llm_config(agent: Optional["AgentDefinition"] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """Studio / Planner 等无 Agent 上下文时的 LLM 配置解析"""
    if agent is not None:
        return ModelRouter.resolve_agent(agent, "llm")
    bind = ModelBinding(model_id=model or PLATFORM_DEFAULT)
    return ModelRouter.resolve_binding(bind, "llm")
