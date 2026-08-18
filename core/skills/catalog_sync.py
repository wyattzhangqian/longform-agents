"""从 SkillHub 同步技能到平台技能库"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from core.skills.importer import parse_skill_import
from core.skills.kind import ensure_manifest_kind
from core.skills.prompt_snippet import build_catalog_prompt_snippet
from core.skills.registry import get_skill_registry
from core.skills.skillhub_client import SkillHubClient, SkillHubEntry

logger = logging.getLogger(__name__)


def skillhub_skill_id(slug: str) -> str:
    safe = slug.strip().lower().replace(" ", "-")
    return f"skillhub_{safe}"


def _executable_tools(manifest: dict) -> List[str]:
    tools = list(manifest.get("tools") or [])
    defs = manifest.get("tool_definitions") or []
    for d in defs:
        if isinstance(d, dict) and d.get("name"):
            tools.append(d["name"])
    return list(dict.fromkeys(tools))


async def import_skillhub_entry(
    entry: SkillHubEntry,
    *,
    skill_md: Optional[str] = None,
    client: Optional[SkillHubClient] = None,
) -> Dict[str, Any]:
    """将单个 SkillHub 条目导入平台 DB"""
    client = client or SkillHubClient()
    registry = get_skill_registry()
    skill_id = skillhub_skill_id(entry.slug)

    if skill_md is None:
        skill_md = await client.extract_skill_md(entry.slug)

    draft = parse_skill_import(skill_md, "skill_md")
    manifest = dict(draft.get("manifest") or {})
    manifest["catalog"] = client.catalog_meta(entry)
    manifest["version"] = entry.version or manifest.get("version") or "1.0.0"

    desc = (entry.description_zh or entry.description or draft.get("description") or "").strip()
    manifest["prompt_snippet"] = build_catalog_prompt_snippet(
        entry.name or draft.get("name") or entry.slug,
        desc,
    )
    manifest = ensure_manifest_kind(manifest, source="catalog")
    if len(desc) > 500:
        desc = desc[:497] + "..."

    payload = {
        "name": entry.name or draft.get("name") or entry.slug,
        "description": desc,
        "source": "catalog",
        "status": "active",
        "manifest": manifest,
        "mcp_config": draft.get("mcp_config") or {},
    }

    existing = await registry.get(skill_id)
    if existing:
        skill = await registry.update(skill_id, payload)
        action = "updated"
    else:
        payload["id"] = skill_id
        skill = await registry.create(payload)
        action = "created"

    executable = _executable_tools(manifest)
    return {
        "skill_id": skill_id,
        "slug": entry.slug,
        "name": skill.name if skill else entry.name,
        "action": action,
        "downloads": entry.downloads,
        "executable_tools": executable,
        "executable": bool(executable),
    }


async def repair_catalog_prompt_snippets() -> int:
    """修复已导入 SkillHub 技能的 prompt_snippet（去掉误注入的代码块）"""
    from core.skills.prompt_snippet import build_catalog_prompt_snippet, is_code_heavy

    registry = get_skill_registry()
    skills = await registry.list(source="catalog")
    fixed = 0
    for skill in skills:
        ps = (skill.manifest.prompt_snippet or "").strip()
        if ps and not is_code_heavy(ps) and ps.startswith("## 技能："):
            continue
        desc = skill.description or ""
        new_ps = build_catalog_prompt_snippet(skill.name, desc)
        if new_ps == ps:
            continue
        manifest = skill.manifest.model_dump()
        manifest["prompt_snippet"] = new_ps
        await registry.update(skill.id, {"manifest": manifest})
        fixed += 1
    return fixed


async def sync_skillhub_catalog(
    *,
    limit: int = 100,
    sort_by: str = "downloads",
    delay_seconds: float = 0.15,
    on_progress: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """拉取 SkillHub Top-N 并导入平台技能库"""
    client = SkillHubClient()
    entries = await client.fetch_top_skills(limit=limit, sort_by=sort_by)

    results: List[Dict[str, Any]] = []
    ok = 0
    failed = 0
    executable_count = 0

    for i, entry in enumerate(entries, 1):
        try:
            row = await import_skillhub_entry(entry, client=client)
            results.append({**row, "ok": True})
            ok += 1
            if row.get("executable"):
                executable_count += 1
            if on_progress:
                on_progress(i, len(entries), row)
        except Exception as e:
            logger.warning("SkillHub 导入失败 %s: %s", entry.slug, e)
            results.append({
                "slug": entry.slug,
                "name": entry.name,
                "ok": False,
                "error": str(e),
                "downloads": entry.downloads,
            })
            failed += 1
        if delay_seconds > 0 and i < len(entries):
            await asyncio.sleep(delay_seconds)

    repaired = await repair_catalog_prompt_snippets()

    return {
        "provider": "skillhub",
        "sort_by": sort_by,
        "requested": limit,
        "fetched": len(entries),
        "imported": ok,
        "failed": failed,
        "executable": executable_count,
        "prompt_snippets_repaired": repaired,
        "results": results,
    }
