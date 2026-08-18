"""公开版最小 LLM 客户端（open-core 公开层）

完整平台版在私有仓库维护（含多供应商 key 解析、thinking 控制、熔断、用量统计等）。
公开版只提供一个最小可用的 OpenAI 兼容客户端，让引擎可以 import 并基本运行：
key 从环境变量读取（DEEPSEEK_API_KEY），不含任何业务私逻辑。

用法:
    client = LLMClient({"model": "deepseek-v4-flash"})
    text = await client.chat("你好")
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

DEFAULT_BASE_URL = "https://api.deepseek.com"


class LLMClient:
    """最小 OpenAI 兼容聊天客户端（aiohttp，流式 + 工具调用）"""

    def __init__(self, config: Optional[dict] = None, **kwargs):
        cfg = dict(config or {})
        cfg.update(kwargs)
        self.model = (
            cfg.get("model")
            or cfg.get("model_id")
            or os.getenv("LLM_MODEL", "deepseek-v4-flash")
        )
        self.api_key = cfg.get("api_key") or os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = (
            cfg.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL", "")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.temperature = float(cfg.get("temperature") or 0.7)
        self.max_tokens = int(cfg.get("max_tokens") or 4096)
        self.timeout = float(
            cfg.get("timeout_seconds") or os.getenv("LLM_TIMEOUT_SECONDS", "120") or 120
        )

    # ---------- 底层 ----------

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    async def _post(self, payload: dict) -> dict:
        if not self.api_key:
            raise RuntimeError("未配置 API key（设置 DEEPSEEK_API_KEY 环境变量）")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url(),
                headers=self._headers(),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"LLM 调用失败 HTTP {resp.status}: {body}")
                return body

    @staticmethod
    def _content_of(body: dict) -> str:
        choices = body.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content") or ""

    @staticmethod
    def _tool_calls_of(body: dict) -> List[Dict[str, Any]]:
        choices = body.get("choices") or []
        if not choices:
            return []
        msg = choices[0].get("message") or {}
        out: List[Dict[str, Any]] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {}
            out.append(
                {
                    "id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "args": args,
                }
            )
        return out

    # ---------- 非流式 ----------

    async def chat_messages(self, messages, temperature=None, max_tokens=None, **kw) -> str:
        body = await self._post(
            {
                "model": self.model,
                "messages": messages,
                "temperature": temperature if temperature is not None else self.temperature,
                "max_tokens": max_tokens or self.max_tokens,
                "stream": False,
            }
        )
        return self._content_of(body)

    async def chat_messages_with_reasoning(self, messages, **kw):
        text = await self.chat_messages(messages, **kw)
        return text, ""  # 公开版不解析思维链

    async def chat(self, prompt: str, system_prompt: str = "", temperature=None, max_tokens=None, **kw) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return await self.chat_messages(messages, temperature=temperature, max_tokens=max_tokens, **kw)

    async def chat_with_tools(self, messages, tools=None, **kw) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        body = await self._post(payload)
        return {"content": self._content_of(body), "tool_calls": self._tool_calls_of(body)}

    async def chat_json(self, prompt: str, system_prompt: str = "") -> Any:
        text = await self.chat(prompt, system_prompt=system_prompt)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # ---------- 流式 ----------

    async def chat_messages_stream(
        self,
        messages,
        tools=None,
        tool_calls_sink: Optional[list] = None,
        **kw,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        slots: Dict[int, dict] = {}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url(),
                headers=self._headers(),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"LLM 流式调用失败 HTTP {resp.status}: {err[:200]}")
                buffer = ""
                async for raw in resp.content:
                    buffer += raw.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if delta.get("reasoning_content"):
                            yield f"__REASONING__:{delta['reasoning_content']}"
                        if delta.get("content"):
                            yield delta["content"]
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                slot = slots.setdefault(idx, {"id": "", "name": "", "args": ""})
                                fn = tc.get("function") or {}
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["args"] += fn["arguments"]
        if tool_calls_sink is not None and slots:
            for idx, slot in sorted(slots.items()):
                try:
                    args = json.loads(slot["args"]) if slot["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls_sink.append({"id": slot["id"], "name": slot["name"], "args": args})

    async def chat_stream(self, prompt: str, system_prompt: str = "", **kw) -> AsyncIterator[str]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        async for chunk in self.chat_messages_stream(messages, **kw):
            yield chunk
