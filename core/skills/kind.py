"""Skill 类型 — instruction / executable / mcp（用户只感知 Skill，Tool 为内部实现）"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

SkillKind = Literal["instruction", "executable", "mcp"]

_BUILTIN_TOOLS = frozenset({"file_read", "file_write", "web_search"})


def _manifest_dict(manifest: Any) -> dict:
    if manifest is None:
        return {}
    if isinstance(manifest, dict):
        return manifest
    if hasattr(manifest, "model_dump"):
        return manifest.model_dump()
    return {}


def has_executable_tools(manifest: Any) -> bool:
    md = _manifest_dict(manifest)
    if md.get("tool_definitions"):
        return True
    tools = md.get("tools") or []
    return bool(tools)


def infer_skill_kind(
    manifest: Any,
    *,
    source: str = "custom",
    mcp_config: Optional[dict] = None,
) -> SkillKind:
    """根据 manifest / 来源推断技能类型"""
    md = _manifest_dict(manifest)
    mcp_cfg = mcp_config or {}
    mcp_block = md.get("mcp") or {}

    if source == "mcp" or mcp_cfg.get("server_id") or mcp_block.get("server_id"):
        return "mcp"

    if has_executable_tools(md):
        return "executable"

    return "instruction"


def ensure_manifest_kind(
    manifest: Any,
    *,
    source: str = "custom",
    mcp_config: Optional[dict] = None,
) -> dict:
    """写入 manifest.kind（缺失或不可信时重算）"""
    md = dict(_manifest_dict(manifest))
    current = md.get("kind")
    if current in ("instruction", "executable", "mcp"):
        # 说明型但已有 tools 时仍以可执行为准
        if current == "instruction" and has_executable_tools(md):
            md["kind"] = "executable"
        elif current == "executable" and not has_executable_tools(md) and source != "platform":
            md["kind"] = infer_skill_kind(md, source=source, mcp_config=mcp_config)
        return md
    md["kind"] = infer_skill_kind(md, source=source, mcp_config=mcp_config)
    return md


def kind_label(kind: str) -> str:
    return {
        "instruction": "说明型",
        "executable": "可执行",
        "mcp": "MCP",
    }.get(kind, "说明型")


async def repair_all_skill_kinds() -> int:
    """为历史技能补全 manifest.kind"""
    from core.skills.registry import get_skill_registry

    registry = get_skill_registry()
    skills = await registry.list()
    fixed = 0
    for skill in skills:
        md = skill.manifest.model_dump()
        normalized = ensure_manifest_kind(
            md,
            source=skill.source,
            mcp_config=skill.mcp_config,
        )
        if normalized.get("kind") == md.get("kind"):
            continue
        await registry.update(skill.id, {"manifest": normalized})
        fixed += 1
    return fixed
