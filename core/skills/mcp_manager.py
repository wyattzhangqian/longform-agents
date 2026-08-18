"""MCP 连接管理 — JSON-RPC over SSE / stdio"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

import aiohttp

from core.logging import get_logger
from core.skills.models import MCPServerDefinition
from core.skills.registry import get_skill_registry

_logger = get_logger("mcp")


class MCPConnectionManager:
    """管理与外部 MCP Server 的连接"""

    _request_id = 0

    @classmethod
    def _next_id(cls) -> int:
        cls._request_id += 1
        return cls._request_id

    async def connect_and_list_tools(self, server_id: str) -> List[dict]:
        """连接 MCP Server 并返回 tools/list"""
        server = await get_skill_registry().get_mcp_server(server_id)
        if not server:
            raise ValueError(f"MCP Server 不存在: {server_id}")
        if not server.enabled:
            raise ValueError(f"MCP Server 已禁用: {server_id}")

        if server.transport == "sse":
            tools = await self._list_tools_sse(server)
        else:
            tools = await self._list_tools_stdio(server)

        await get_skill_registry().update_mcp_server(server_id, {"health_status": "healthy"})
        return tools

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: Optional[dict] = None,
    ) -> Any:
        server = await get_skill_registry().get_mcp_server(server_id)
        if not server:
            raise ValueError(f"MCP Server 不存在: {server_id}")
        if not server.enabled:
            raise ValueError(f"MCP Server 已禁用: {server_id}")

        if server.transport == "sse":
            return await self._call_tool_sse(server, tool_name, arguments or {})
        return await self._call_tool_stdio(server, tool_name, arguments or {})

    async def _jsonrpc_sse(
        self,
        server: MCPServerDefinition,
        method: str,
        params: Optional[dict] = None,
    ) -> Any:
        url = server.url.rstrip("/")
        if not url.endswith("/mcp"):
            url = f"{url}/mcp" if not url.endswith("/") else f"{url}mcp"

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if server.auth_header:
            headers["Authorization"] = server.auth_header

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"MCP HTTP {resp.status}: {text[:200]}")
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(data["error"].get("message", str(data["error"])))
                return data.get("result")

    async def _list_tools_sse(self, server: MCPServerDefinition) -> List[dict]:
        await self._jsonrpc_sse(server, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agent-platform", "version": "1.0.0"},
        })
        result = await self._jsonrpc_sse(server, "tools/list", {})
        return result.get("tools", []) if isinstance(result, dict) else []

    async def _call_tool_sse(
        self,
        server: MCPServerDefinition,
        tool_name: str,
        arguments: dict,
    ) -> Any:
        result = await self._jsonrpc_sse(server, "tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if isinstance(result, dict) and "content" in result:
            parts = []
            for block in result["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts) if parts else str(result)
        return result

    async def _list_tools_stdio(self, server: MCPServerDefinition) -> List[dict]:
        proc = await asyncio.create_subprocess_exec(
            server.command,
            *server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**dict(__import__("os").environ), **server.env} if server.env else None,
        )
        try:
            await self._stdio_request(proc, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent-platform", "version": "1.0.0"},
            })
            result = await self._stdio_request(proc, "tools/list", {})
            return result.get("tools", []) if isinstance(result, dict) else []
        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    async def _call_tool_stdio(
        self,
        server: MCPServerDefinition,
        tool_name: str,
        arguments: dict,
    ) -> Any:
        proc = await asyncio.create_subprocess_exec(
            server.command,
            *server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**dict(__import__("os").environ), **server.env} if server.env else None,
        )
        try:
            await self._stdio_request(proc, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent-platform", "version": "1.0.0"},
            })
            result = await self._stdio_request(proc, "tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
            if isinstance(result, dict) and "content" in result:
                parts = []
                for block in result["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                return "\n".join(parts) if parts else str(result)
            return result
        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    async def _stdio_request(
        self,
        proc: asyncio.subprocess.Process,
        method: str,
        params: Optional[dict],
    ) -> Any:
        req_id = self._next_id()
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }) + "\n"
        proc.stdin.write(payload.encode())
        await proc.stdin.drain()

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        if not line:
            raise RuntimeError("MCP stdio 无响应")
        data = json.loads(line.decode())
        if data.get("id") != req_id:
            raise RuntimeError(f"MCP 响应 ID 不匹配: {data}")
        if "error" in data:
            raise RuntimeError(data["error"].get("message", str(data["error"])))
        return data.get("result")


_manager: Optional[MCPConnectionManager] = None


def get_mcp_manager() -> MCPConnectionManager:
    global _manager
    if _manager is None:
        _manager = MCPConnectionManager()
    return _manager
