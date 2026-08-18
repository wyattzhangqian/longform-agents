"""Skill 包导入 — JSON manifest / SKILL.md（非 MCP 专用）"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict

from core.skills.tool_spec_parser import (
    extract_tool_block_from_md,
    finalize_import_manifest,
    parse_tool_block,
)
from core.skills.kind import ensure_manifest_kind
from core.skills.prompt_snippet import (
    PROMPT_HEADERS,
    USAGE_HEADERS,
    WHEN_TO_USE_HEADERS,
    compose_prompt_snippet,
)


def import_from_json(raw: str | dict) -> dict:
    """解析 Skill JSON 包 → create 可用的 draft dict"""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise ValueError("导入内容为空")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 格式无效: {e}") from e
    else:
        data = raw

    if not isinstance(data, dict):
        raise ValueError("Skill 包必须是 JSON 对象")

    if "skills" in data and isinstance(data["skills"], list):
        if not data["skills"]:
            raise ValueError("skills 数组为空")
        data = data["skills"][0]
    elif "skill" in data and isinstance(data["skill"], dict):
        data = data["skill"]

    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("缺少 name 字段")

    manifest = data.get("manifest") or {}
    if not isinstance(manifest, dict):
        raise ValueError("manifest 必须是对象")

    skill_id = (data.get("id") or "").strip()
    if not skill_id:
        slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name.lower()).strip("_") or "skill"
        skill_id = f"skill_import_{slug}_{int(time.time() * 1000) % 100000}"

    source = data.get("source") or "imported"
    if source not in ("custom", "imported", "mcp"):
        source = "imported"

    raw_manifest = {
        "version": manifest.get("version") or "1.0.0",
        "tools": list(manifest.get("tools") or []),
        "tool_definitions": list(manifest.get("tool_definitions") or []),
        "mcp": manifest.get("mcp"),
        "prompt_snippet": manifest.get("prompt_snippet") or "",
        "input_schema": manifest.get("input_schema"),
    }
    raw_manifest = finalize_import_manifest(raw_manifest, skill_name=name)
    raw_manifest = ensure_manifest_kind(raw_manifest, source=source)

    return {
        "id": skill_id,
        "name": name,
        "description": data.get("description") or "",
        "source": source,
        "manifest": raw_manifest,
        "mcp_config": data.get("mcp_config") or {},
    }


def _strip_yaml_frontmatter(content: str) -> tuple[str, dict]:
    """解析可选 YAML frontmatter（--- ... ---）"""
    meta: dict = {}
    text = content.strip()
    if not text.startswith("---"):
        return text, meta
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text, meta
    block = parts[1]
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip().strip('"').strip("'")
        if key in ("name", "description", "id"):
            meta[key] = val
    return parts[2].strip(), meta


def import_from_skill_md(content: str) -> dict:
    """解析 SKILL.md 风格文本 → draft dict"""
    content = (content or "").strip()
    if not content:
        raise ValueError("SKILL.md 内容为空")

    body, frontmatter = _strip_yaml_frontmatter(content)
    tool_block, body = extract_tool_block_from_md(body.splitlines())
    tool_definitions: list[dict] = []
    if tool_block:
        parsed = parse_tool_block(tool_block)
        if parsed:
            tool_definitions.append(parsed)

    lines = body.splitlines()
    name = (frontmatter.get("name") or "").strip()
    description_lines: list[str] = []
    if frontmatter.get("description"):
        description_lines.append(str(frontmatter["description"]).strip())
    tools: list[str] = []
    prompt_lines: list[str] = []
    when_to_use_lines: list[str] = []
    usage_lines: list[str] = []
    section = "header"

    tool_section_headers = (
        "## tools", "## 工具", "## 关联工具",
    )

    for line in lines:
        if line.startswith("# ") and not name:
            name = line[2:].strip()
            section = "desc"
            continue
        lower = line.strip().lower()
        if lower in tool_section_headers:
            section = "tools"
            continue
        if lower in PROMPT_HEADERS:
            section = "prompt"
            continue
        if lower in WHEN_TO_USE_HEADERS:
            section = "when"
            continue
        if lower in USAGE_HEADERS:
            section = "usage"
            continue

        if section == "desc" and line.strip() and not line.startswith("#"):
            description_lines.append(line.strip())
        elif section == "tools":
            m = re.match(r"^[-*]\s*(\S+)", line.strip())
            if m:
                tools.append(m.group(1))
            elif line.strip() and not line.startswith("#"):
                parts = line.strip().split()
                if parts:
                    tools.append(parts[0])
        elif section == "prompt":
            prompt_lines.append(line)
        elif section == "when":
            when_to_use_lines.append(line)
        elif section == "usage":
            usage_lines.append(line)

    if not name:
        raise ValueError("SKILL.md 需要 YAML frontmatter 的 name，或以 # 技能名称 开头")

    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name.lower()).strip("_") or "skill"
    skill_id = (frontmatter.get("id") or "").strip() or f"skill_md_{slug}_{int(time.time() * 1000) % 100000}"

    prompt_snippet = compose_prompt_snippet(
        name=name,
        description_lines=description_lines,
        prompt_lines=prompt_lines,
        when_to_use_lines=when_to_use_lines,
        usage_lines=usage_lines,
    )

    manifest = finalize_import_manifest(
        {
            "version": "1.0.0",
            "tools": tools,
            "tool_definitions": tool_definitions,
            "prompt_snippet": prompt_snippet,
        },
        full_content=content,
        skill_name=name,
    )
    manifest = ensure_manifest_kind(manifest, source="imported")

    return {
        "id": skill_id,
        "name": name,
        "description": " ".join(description_lines[:3]) or f"从 SKILL.md 导入: {name}",
        "source": "imported",
        "manifest": manifest,
        "mcp_config": {},
    }


def parse_skill_import(content: str, fmt: str = "json") -> dict:
    fmt = (fmt or "json").lower().strip()
    if fmt in ("json", "skill.json"):
        return import_from_json(content)
    if fmt in ("skill_md", "md", "skill.md"):
        return import_from_skill_md(content)
    raise ValueError(f"不支持的导入格式: {fmt}，可用 json | skill_md")
