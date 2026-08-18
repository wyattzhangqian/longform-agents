"""ImageGenClient — 生图模型调用（按 Agent model_profile.image 配置）

从 core/models/clients/ 收敛：与 ModelRouter 内聚，base_url 归一化逻辑统一维护。
"""

from __future__ import annotations

import aiohttp
from typing import Any, Dict

from core.logging import get_logger

_logger = get_logger("image_client")


class ImageGenClient:
    """多 provider 生图客户端（首期：OpenAI 兼容 + Volcengine Ark）"""

    # 归一化：用户可能填入完整 endpoint（含 /images/generations），需剥离到 API 根
    _STRIP_SUFFIXES = ("/images/generations", "/images/generation")

    def __init__(self, config: Dict[str, Any]):
        self.model = config.get("model", "")
        self.provider = config.get("provider", "")
        self.api_key = config.get("api_key", "")
        base = (config.get("base_url") or "").rstrip("/")
        for suffix in self._STRIP_SUFFIXES:
            if base.endswith(suffix):
                base = base[: -len(suffix)].rstrip("/")
                break
        self.base_url = base

    async def generate(self, prompt: str, *, size: str = "1920x1920", n: int = 1,
                        width: int = 0, height: int = 0, **kwargs) -> Dict[str, Any]:
        """生成图片。width/height 优先级高于 size 字符串。"""
        if not self.api_key:
            return {
                "ok": False,
                "error": f"未配置 {self.provider or 'image'} API Key（请设置 VOLCENGINE_API_KEY 或 OPENAI_API_KEY）",
            }
        if not prompt.strip():
            return {"ok": False, "error": "prompt 为空"}

        # 工具传入 width/height 时自动转为 size 字符串
        if width > 0 and height > 0:
            size = f"{width}x{height}"

        url = f"{self.base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "prompt": prompt, "n": n, "size": size}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    body = await resp.json()
                    if resp.status >= 400:
                        return {"ok": False, "error": str(body)[:500], "status": resp.status}
                    data = body.get("data") or []
                    urls = [d.get("url") or d.get("b64_json", "")[:80] for d in data if isinstance(d, dict)]
                    return {"ok": True, "urls": urls, "raw": body}
        except Exception as e:
            _logger.warning("生图调用失败: %s", e)
            return {"ok": False, "error": str(e)}
