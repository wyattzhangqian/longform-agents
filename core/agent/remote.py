"""RemoteAgent — 通过 HTTP 调用外部 Agent 服务（P3）

协议（POST runtime_info.endpoint）:
  请求: { agent_id, project_id, task, input, system_prompt }
  响应: { result: dict, raw_text?: str } 或直接返回 result dict
"""

from __future__ import annotations
import json
from typing import Any, Dict, Optional

import aiohttp

from core.agent.base import BaseAgent
from core.agent.types import AgentDefinition, AgentRuntimeInfo
from core.logging import get_logger

_logger = get_logger("remote_agent")


class RemoteAgent(BaseAgent):
    """远程 Agent — 编排器仍 local leader，执行委托给 HTTP endpoint"""

    def __init__(self, definition: AgentDefinition):
        super().__init__(definition)
        self.runtime: AgentRuntimeInfo = definition.runtime_info

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.runtime.auth_header:
            token = self.runtime.auth_header.strip()
            if not token.lower().startswith("bearer "):
                token = f"Bearer {token}"
            headers["Authorization"] = token
        return headers

    def _health_url(self) -> str:
        if self.runtime.health_endpoint:
            return self.runtime.health_endpoint
        base = self.runtime.endpoint.rstrip("/")
        return f"{base}/health"

    async def ping(self) -> dict:
        """健康检查远程 Agent"""
        url = self._health_url()
        timeout = aiohttp.ClientTimeout(total=min(10, self.runtime.timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=self._headers()) as resp:
                body = await resp.text()
                return {
                    "ok": resp.status < 400,
                    "status_code": resp.status,
                    "url": url,
                    "body_preview": body[:200],
                }

    async def execute(self, input_data: dict) -> Any:
        endpoint = (self.runtime.endpoint or "").strip()
        if not endpoint:
            raise ValueError(f"RemoteAgent {self.id} 未配置 runtime_info.endpoint")

        if self.bus:
            await self.bus.send_status(f"🌐 远程调用 {self.name} → {endpoint}")

        state = input_data.get("state")
        payload = {
            "agent_id": self.id,
            "agent_name": self.name,
            "project_id": getattr(state, "project_id", "") if state else "",
            "task": getattr(state, "task", "") if state else input_data.get("config", {}).get("task_description", ""),
            "input": {
                k: v for k, v in input_data.items()
                if k != "state"
            },
            "system_prompt": self.system_prompt,
        }

        timeout = aiohttp.ClientTimeout(total=self.runtime.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, json=payload, headers=self._headers()) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"RemoteAgent HTTP {resp.status}: {text[:500]}")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return {"result": text, "raw_text": text}

        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data
