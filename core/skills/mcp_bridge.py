"""MCPToolBridge — 将 MCP 工具注册为 ToolRegistry 代理 handler"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from core.agent.tools import ToolDefinition, ToolRegistry
from core.logging import get_logger
from core.skills.mcp_manager import get_mcp_manager
from core.skills.registry import get_skill_registry

_logger = get_logger("mcp_bridge")

# 默认禁止自动执行的 shell 类工具（可配置扩展）
BLOCKED_TOOL_PATTERNS = {"shell", "bash", "exec", "run_terminal"}


class MCPToolBridge:
    """MCP → ToolRegistry 桥接"""

    _instance: Optional["MCPToolBridge"] = None

    def __init__(self):
        self._registered: Set[str] = set()

    @classmethod
    def get_instance(cls) -> "MCPToolBridge":
        if cls._instance is None:
            cls._instance = MCPToolBridge()
        return cls._instance

    @staticmethod
    def proxy_tool_name(server_id: str, tool_name: str) -> str:
        safe_server = server_id.replace("-", "_")
        safe_tool = tool_name.replace("-", "_")
        return f"mcp_{safe_server}__{safe_tool}"

    @staticmethod
    def parse_proxy_name(proxy_name: str) -> Optional[tuple[str, str]]:
        if not proxy_name.startswith("mcp_") or "__" not in proxy_name:
            return None
        rest = proxy_name[4:]
        server_part, tool_part = rest.split("__", 1)
        return server_part, tool_part

    async def ensure_registered(self) -> None:
        """根据 DB 中的 MCP Skill 注册代理工具"""
        registry = get_skill_registry()
        skills = await registry.list(source="mcp")
        tool_registry = ToolRegistry.get_instance()
        manager = get_mcp_manager()

        for skill in skills:
            mcp_cfg = skill.manifest.mcp or {}
            server_id = mcp_cfg.get("server_id") or skill.mcp_config.get("server_id")
            tool_names = mcp_cfg.get("tool_names") or []
            if not server_id:
                continue

            server = await registry.get_mcp_server(server_id)
            if not server or not server.enabled:
                continue

            tool_schemas: Dict[str, dict] = skill.mcp_config.get("tool_schemas") or {}

            for tn in tool_names:
                proxy = self.proxy_tool_name(server_id, tn)
                if proxy in self._registered:
                    continue

                schema = tool_schemas.get(tn, {})
                desc = schema.get("description", f"MCP 工具 {tn}（server: {server_id}）")
                params = schema.get("inputSchema") or schema.get("parameters") or {
                    "type": "object",
                    "properties": {},
                }

                blocked = any(p in tn.lower() for p in BLOCKED_TOOL_PATTERNS)
                handler = self._make_handler(server_id, tn, blocked)

                tool_registry.register(ToolDefinition(
                    name=proxy,
                    description=desc + (" [需审批]" if blocked else ""),
                    parameters=params,
                    handler=handler,
                    category="mcp",
                    requires_approval=blocked,
                ))
                self._registered.add(proxy)
                _logger.info("注册 MCP 代理工具: %s", proxy)

    def _make_handler(self, server_id: str, tool_name: str, blocked: bool):
        async def handler(**kwargs) -> Any:
            if blocked:
                return f"[需审批] MCP 工具 {tool_name} 默认禁止自动执行，请通过 Decision 门禁批准"
            manager = get_mcp_manager()
            return await manager.call_tool(server_id, tool_name, kwargs)

        return handler

    async def import_tools_as_skill(
        self,
        server_id: str,
        tool_names: Optional[list] = None,
        skill_name: Optional[str] = None,
    ) -> dict:
        """从 MCP Server 导入工具并创建 Skill draft"""
        manager = get_mcp_manager()
        tools = await manager.connect_and_list_tools(server_id)
        server = await get_skill_registry().get_mcp_server(server_id)
        if not server:
            raise ValueError(f"MCP Server 不存在: {server_id}")

        selected = tools
        if tool_names:
            names_set = set(tool_names)
            selected = [t for t in tools if t.get("name") in names_set]

        if not selected:
            raise ValueError("未找到可导入的 MCP 工具")

        names = [t["name"] for t in selected]
        tool_schemas = {t["name"]: t for t in selected}

        skill_id = f"skill_mcp_{server_id}_{uuid_suffix()}"
        manifest = {
            "version": "1.0.0",
            "kind": "mcp",
            "tools": [],
            "mcp": {"server_id": server_id, "tool_names": names},
            "prompt_snippet": f"## MCP 技能：{server.name}\n可用 MCP 工具: {', '.join(names)}",
        }

        return {
            "draft": {
                "id": skill_id,
                "name": skill_name or f"{server.name} MCP",
                "description": f"从 MCP Server [{server.name}] 导入 {len(names)} 个工具",
                "source": "mcp",
                "manifest": manifest,
                "mcp_config": {"server_id": server_id, "tool_schemas": tool_schemas},
            },
            "tools": selected,
        }


def uuid_suffix() -> str:
    import uuid
    return uuid.uuid4().hex[:8]
