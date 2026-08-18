"""MusicGenClient — AI 音乐生成（对接 API Box / Suno 中转服务）

支持 provider: apibox (默认) / acedata / suno
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import aiohttp

from core.logging import get_logger

_logger = get_logger("music_client")

# 轮询配置
POLL_INTERVAL = 5.0       # 轮询间隔（秒）
POLL_TIMEOUT = 360.0      # 最大等待时间（秒）


class MusicGenClient:
    """AI 音乐生成客户端 — 对接 API Box (apibox.erweima.ai) 等中转服务"""

    def __init__(self, config: Dict[str, Any]):
        self.model = config.get("model", "V4_5ALL")
        self.provider = config.get("provider", "apibox")
        self.api_key = config.get("api_key", "")
        self.base_url = (config.get("base_url") or "https://apibox.erweima.ai").rstrip("/")
        self._auth = {"Authorization": f"Bearer {self.api_key}"}

    # ── 公共接口 ──

    async def generate(
        self,
        prompt: str,
        *,
        lyrics: str = "",
        instrumental: bool = False,
        style: str = "",
        duration: int = 0,  # API Box 不需要，忽略
    ) -> Dict[str, Any]:
        """生成音乐 → {ok, url?, file_path?, task_id?, error?}"""
        if not self.api_key:
            return {"ok": False, "error": "未配置 SUNO_API_KEY，请在 .env 中设置"}
        if not prompt.strip() and not lyrics.strip():
            return {"ok": False, "error": "prompt 与 lyrics 均为空"}

        full_prompt = prompt.strip()
        if lyrics.strip():
            full_prompt = f"{full_prompt}\n\n{lyrics[:2000]}" if full_prompt else lyrics[:2000]
        if style.strip():
            full_prompt = f"{full_prompt}，风格: {style.strip()}"

        try:
            async with aiohttp.ClientSession() as session:
                task_id = await self._submit(session, full_prompt, instrumental)
                if not task_id:
                    return {"ok": False, "error": "提交音乐生成任务失败，请检查 API Key 和网络"}
                _logger.info("音乐任务已提交: task_id=%s", task_id)

                result = await self._poll(session, task_id)
                if not result:
                    return {"ok": False, "error": f"音乐生成超时（>{POLL_TIMEOUT}s），task_id={task_id}"}

                audio_url = result.get("audio_url", "")
                if not audio_url:
                    return {"ok": False, "error": "生成完成但未返回音频URL"}

                local_path = await self._download(session, audio_url, task_id)
                return {
                    "ok": True,
                    "url": audio_url,
                    "file_path": local_path,
                    "task_id": task_id,
                    "title": result.get("title", ""),
                    "duration": result.get("duration", 0),
                }

        except Exception as e:
            _logger.warning("音乐生成失败: %s", e)
            return {"ok": False, "error": str(e)}

    # ── 内部 ──

    async def _submit(
        self, session: aiohttp.ClientSession, prompt: str, instrumental: bool,
    ) -> Optional[str]:
        """POST /api/v1/generate → taskId"""
        headers = {**self._auth, "Content-Type": "application/json"}
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "instrumental": instrumental,
            "model": self.model,
        }
        try:
            async with session.post(
                f"{self.base_url}/api/v1/generate",
                json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    _logger.warning("提交失败 HTTP %s: %s", resp.status, text[:300])
                    return None
                data = await resp.json() if text else {}
                # API Box: {"code": 200, "data": {"taskId": "xxx"}}
                if data.get("code") != 200:
                    _logger.warning("提交失败: %s", data.get("msg", text[:200]))
                    return None
                return data.get("data", {}).get("taskId") or data.get("taskId")
        except Exception as e:
            _logger.warning("提交异常: %s", e)
            return None

    async def _poll(
        self, session: aiohttp.ClientSession, task_id: str,
    ) -> Optional[Dict[str, Any]]:
        """GET /api/v1/generate/record-info?taskId=... → {audio_url, title, duration}"""
        deadline = time.monotonic() + POLL_TIMEOUT

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                async with session.get(
                    f"{self.base_url}/api/v1/generate/record-info",
                    params={"taskId": task_id},
                    headers=self._auth,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status >= 400:
                        continue
                    data = await resp.json()
                    if data.get("code") != 200:
                        continue
                    inner = data.get("data", {})
                    status = (inner.get("status") or "").upper()

                    if status == "SUCCESS":
                        suno_data = inner.get("response", {}).get("sunoData", [])
                        if suno_data:
                            clip = suno_data[0]
                            return {
                                "audio_url": clip.get("audioUrl") or clip.get("streamAudioUrl", ""),
                                "title": clip.get("title", ""),
                                "duration": clip.get("duration", 0),
                            }
                        return None

                    if status in ("CREATE_TASK_FAILED", "SENSITIVE_WORD_ERROR"):
                        _logger.warning("任务失败: task_id=%s status=%s", task_id, status)
                        return None

            except Exception:
                continue

        return None

    async def _download(
        self, session: aiohttp.ClientSession, url: str, task_id: str,
    ) -> str:
        """下载音频到 outputs/music/"""
        from pathlib import Path
        output_dir = Path("outputs/music")
        output_dir.mkdir(parents=True, exist_ok=True)

        ext = ".mp3"
        if ".wav" in url: ext = ".wav"
        elif ".flac" in url: ext = ".flac"
        local_path = output_dir / f"{task_id}{ext}"

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"下载失败 HTTP {resp.status}")
            content = await resp.read()

        local_path.write_bytes(content)
        _logger.info("音乐已下载: %s (%d bytes)", local_path, len(content))
        return str(local_path)
