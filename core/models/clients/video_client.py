"""VideoGenClient — 生视频模型调用（按 Agent model_profile.video 配置）

从 core/models/clients/ 收敛：与 ModelRouter 内聚，base_url 归一化逻辑统一维护。
"""

from __future__ import annotations

import aiohttp
import json
from typing import Any, Dict

from core.logging import get_logger

_logger = get_logger("video_client")


class VideoGenClient:
    """多 provider 生视频客户端（Volcengine Ark / OpenAI 兼容 API）"""

    # 归一化：用户可能填入完整 endpoint（含 /videos/generations），需剥离到 API 根
    _STRIP_SUFFIXES = ("/videos/generations", "/videos/generation")

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

    async def generate(self, prompt: str, *, image_url: str = "") -> Dict[str, Any]:
        if not self.api_key:
            return {
                "ok": False,
                "error": f"未配置 {self.provider or 'video'} API Key（请按 provider 配置对应凭证）",
            }
        if not prompt.strip() and not image_url:
            return {"ok": False, "error": "prompt 与 image_url 均为空"}

        # 各 provider 任务 API 差异较大；统一返回结构化占位，供 workflow 写 manifest / 后续对接
        url = f"{self.base_url}/videos/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {"model": self.model, "prompt": prompt}
        if image_url:
            payload["image_url"] = image_url

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        return {
                            "ok": False,
                            "error": text[:500],
                            "status": resp.status,
                            "hint": "若 endpoint 未开通，请先在 provider 控制台配置生视频模型",
                        }
                    try:
                        body = json.loads(text)
                    except json.JSONDecodeError:
                        body = {"raw": text[:500]}
                    return {"ok": True, "body": body}
        except Exception as e:
            _logger.warning("生视频调用失败: %s", e)
            return {"ok": False, "error": str(e)}
