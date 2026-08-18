"""SkillResolver — 将 skill_ids 展开为 tools / prompt / namespaces"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from core.skills.registry import TOOL_TO_PLATFORM_SKILL, get_skill_registry
from core.skills.mcp_bridge import MCPToolBridge
from core.skills.tool_materializer import has_custom_tools, skill_tool_namespace


class SkillResolver:
    """展开 Agent 挂载的技能包"""

    @staticmethod
    async def expand(
        skill_ids: Optional[List[str]] = None,
        legacy_tools: Optional[List[str]] = None,
        include_instruction_tools: bool = False,
    ) -> Dict[str, Any]:
        """展开 skill_ids → 运行时可用资源

        include_instruction_tools=False 时，kind=instruction 的 Skill 仅贡献 prompt，
        不展开 tool 白名单（Tool 与 Skill 平级后由 Agent.tool_ids 直接挂载）。

        Returns:
            tools, tool_ids, prompt_snippets, namespaces, skill_ids
        """
        registry = get_skill_registry()
        skill_ids = list(skill_ids or [])
        legacy_tools = list(legacy_tools or [])

        # 兼容旧 agents.tools：映射到平台 Skill ID
        if not skill_ids and legacy_tools:
            for t in legacy_tools:
                sid = TOOL_TO_PLATFORM_SKILL.get(t)
                if sid and sid not in skill_ids:
                    skill_ids.append(sid)
                elif t.startswith("skill_") and t not in skill_ids:
                    skill_ids.append(t)

        tools: Set[str] = set()
        tool_ids: Set[str] = set()
        namespaces: Set[str] = set()
        prompt_snippets: List[str] = []

        # S4 修正：MCP 注册移到生命周期事件（main.py startup + Skill create），
        # 不再在读路径 expand() 中重复触发（读路径不应有写副作用）
        for sid in skill_ids:
            skill = await registry.get(sid)
            if not skill or skill.status != "active":
                continue

            manifest = skill.manifest
            if manifest.prompt_snippet:
                prompt_snippets.append(manifest.prompt_snippet.strip())

            # instruction 类 Skill 仅注入 prompt；工具由 Agent.tool_ids 或 executable/mcp 类 Skill 提供
            kind = getattr(manifest, "kind", "instruction") or "instruction"
            if not include_instruction_tools and kind == "instruction":
                continue

            md = manifest.model_dump() if hasattr(manifest, "model_dump") else manifest
            if has_custom_tools(md):
                ns = skill_tool_namespace(sid)
                namespaces.add(ns)
                for td in md.get("tool_definitions") or []:
                    if isinstance(td, dict) and td.get("name"):
                        tools.add(td["name"])
                        tool_ids.add(f"{ns}/{td['name']}")

            for t in manifest.tools:
                if t not in tools:
                    tools.add(t)
                    tool_ids.add(t)

            mcp_cfg = manifest.mcp or {}
            server_id = mcp_cfg.get("server_id") or skill.mcp_config.get("server_id")
            tool_names = mcp_cfg.get("tool_names") or []
            if server_id and tool_names:
                for tn in tool_names:
                    # P2-12 修复：bridge 未定义（仅导入类），改为静态方法调用。
                    # 修复前此处 NameError，一旦创建 MCP skill 并挂载 Agent 即崩。
                    proxy_name = MCPToolBridge.proxy_tool_name(server_id, tn)
                    tools.add(proxy_name)
                    tool_ids.add(proxy_name)

        # legacy_tools 中直接的 namespace/name
        for t in legacy_tools:
            if "/" in t:
                tool_ids.add(t)
                tools.add(t.split("/", 1)[-1])
                namespaces.add(t.split("/", 1)[0])
            elif t not in tools and t not in TOOL_TO_PLATFORM_SKILL:
                tools.add(t)
                tool_ids.add(t)

        return {
            "tools": sorted(tools),
            "tool_ids": sorted(tool_ids),
            "prompt_snippets": prompt_snippets,
            "namespaces": sorted(namespaces),
            "skill_ids": skill_ids,
        }

    @staticmethod
    async def invoke_skill(
        skill_id: str,
        args: Optional[dict] = None,
        tool_name: Optional[str] = None,
        output_dir: str = "",
    ) -> dict:
        """调试沙箱：执行 Skill 关联的第一个 tool 或指定 tool"""
        import time

        from core.agent.tools import ToolExecutor, ToolRegistry

        registry = get_skill_registry()
        skill = await registry.get(skill_id)
        if not skill:
            return {"success": False, "error": f"Skill 不存在: {skill_id}"}

        expanded = await SkillResolver.expand([skill_id])
        available = expanded["tools"]
        namespaces = expanded["namespaces"]
        if not available:
            return {"success": False, "error": "该 Skill 未关联可执行工具"}

        target = tool_name or available[0]
        if target not in available:
            return {"success": False, "error": f"工具 {target} 不在 Skill 范围内，可用: {available}"}

        args = args or {}
        executor = ToolExecutor(ToolRegistry.get_instance(), output_dir=output_dir or "outputs")

        tool_def = executor.registry.resolve(target, namespaces=namespaces)
        if tool_def and tool_def.parameters.get("properties"):
            allowed = set(tool_def.parameters["properties"].keys())
            args = {k: v for k, v in args.items() if k in allowed}

        thought = {"needs_tool": True, "tool_name": target, "tool_args": args}
        start = time.perf_counter()
        result = await executor.execute(
            thought,
            allowed_tools=available,
            namespaces=namespaces,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        return {
            "success": True,
            "skill_id": skill_id,
            "tool_name": target,
            "result": result,
            "elapsed_ms": elapsed_ms,
            "logs": [f"invoke {target} with {args}"],
        }
