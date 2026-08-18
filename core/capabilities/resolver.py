"""CapabilityResolver — Agent 三维度并列：tool_ids + skill_ids + namespaces"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.skills.resolver import SkillResolver

if TYPE_CHECKING:
    from core.agent.types import AgentDefinition


class CapabilityResolver:
    """展开 Agent 挂载的工具与技能（Tool 与 Skill 平级，在此合并）

    工具声明优先级（S3 规范）：
    - tool_ids: 直接可用的内置工具名（最高优先，如 generate_image）
    - skill_ids → SkillResolver: Skill 贡献的工具 + prompt_snippet
    - agent.tools (legacy): 旧数据兼容，deprecated，不推荐新使用

    三者合并为一个 set，无冲突（同名工具最终指向同一个 ToolDefinition）。
    """

    @staticmethod
    async def expand(agent: "AgentDefinition") -> Dict[str, Any]:
        legacy = [t.name for t in (agent.tools or [])]
        skill_ctx = await SkillResolver.expand(
            skill_ids=agent.skill_ids,
            legacy_tools=legacy,
            include_instruction_tools=False,
        )

        tools = set(skill_ctx.get("tools") or [])
        tool_ids = set(skill_ctx.get("tool_ids") or [])
        namespaces = set(skill_ctx.get("namespaces") or [])

        for tid in agent.tool_ids or []:
            if not tid:
                continue
            tool_ids.add(tid)
            if "/" in tid:
                ns, bare = tid.split("/", 1)
                namespaces.add(ns)
                tools.add(bare)
            else:
                tools.add(tid)

        # 多模态能力自动注入：配了模型就自动具备对应工具能力，无需手动配 tool_ids
        mp = agent.model_profile
        if mp.image:
            tools.add("generate_image")
        if mp.video:
            tools.add("generate_video")
        if mp.music:
            tools.add("generate_music")

        return {
            "tools": sorted(tools),
            "tool_ids": sorted(tool_ids),
            "prompt_snippets": skill_ctx.get("prompt_snippets") or [],
            "namespaces": sorted(namespaces),
            "skill_ids": skill_ctx.get("skill_ids") or [],
        }

    @staticmethod
    async def expand_ids(
        tool_ids: Optional[List[str]] = None,
        skill_ids: Optional[List[str]] = None,
        legacy_tools: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        from core.agent.types import AgentDefinition, AgentTool

        agent = AgentDefinition(
            id="_preview",
            name="preview",
            tool_ids=list(tool_ids or []),
            skill_ids=list(skill_ids or []),
            tools=[AgentTool(name=t) for t in (legacy_tools or [])],
        )
        return await CapabilityResolver.expand(agent)
