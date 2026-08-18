"""从 SKILL.md / JSON 解析可执行 tool_definitions（导入管线）"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


def parse_tool_block(text: str) -> Optional[dict]:
    """解析 ```tool / ```json 代码块或 key: value 段落"""
    text = (text or "").strip()
    if not text:
        return None

    fence = re.search(r"```(?:tool|json|yaml)?\s*\n([\s\S]*?)```", text, re.I)
    if fence:
        block = fence.group(1).strip()
        try:
            data = json.loads(block)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

    if text.startswith("{"):
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

    return _parse_kv_tool_spec(text)


def _parse_kv_tool_spec(text: str) -> Optional[dict]:
    spec: dict = {"param_properties": {}, "param_required": []}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue

        if key.startswith("param."):
            pname = key[6:]
            ptype = "string"
            required = False
            if " " in val:
                parts = val.split()
                ptype = parts[0]
                required = "required" in parts[1:]
            else:
                ptype = val or "string"
            spec["param_properties"][pname] = {
                "type": ptype,
                "description": f"参数 {pname}",
            }
            if required or val.endswith("required"):
                spec["param_required"].append(pname)
        else:
            spec[key] = val

    if not spec.get("name"):
        return None

    if spec.get("param_properties") and not spec.get("parameters"):
        props = spec.pop("param_properties")
        req = spec.pop("param_required", None) or list(props.keys())
        spec["parameters"] = {
            "type": "object",
            "properties": props,
            "required": req,
        }
    spec.pop("param_properties", None)
    spec.pop("param_required", None)
    return spec


def infer_jina_tool_definition(skill_name: str = "") -> dict:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (skill_name or "jina").lower()).strip("_")
    tool_name = "jina_fetch" if "jina" in slug else f"{slug}_fetch"
    return {
        "name": tool_name,
        "description": "通过 Jina Reader 抓取网页正文（Markdown）",
        "type": "http_request",
        "method": "GET",
        "url_template": "https://r.jina.ai/{url}",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的完整 URL（含 https://）",
                }
            },
            "required": ["url"],
        },
        "permission_mode": "allow",
        "risk_level": "low",
        "timeout_seconds": 90,
    }


def content_suggests_jina(content: str) -> bool:
    lower = (content or "").lower()
    return "r.jina.ai" in lower or "jina reader" in lower or "jina.ai" in lower


def finalize_import_manifest(manifest: dict, full_content: str = "", skill_name: str = "") -> dict:
    """合并 tools / tool_definitions，必要时推断 Jina 等可执行能力"""
    manifest = dict(manifest or {})
    tool_defs: List[dict] = list(manifest.get("tool_definitions") or [])
    legacy_tools = list(manifest.get("tools") or [])

    if not tool_defs and content_suggests_jina(full_content):
        tool_defs = [infer_jina_tool_definition(skill_name)]

    if tool_defs:
        manifest["tool_definitions"] = tool_defs
        names = [d["name"] for d in tool_defs if d.get("name")]
        manifest["tools"] = list(dict.fromkeys(names + legacy_tools))
    else:
        manifest["tools"] = legacy_tools

    return manifest


def extract_tool_block_from_md(lines: list[str]) -> tuple[Optional[str], str]:
    """从 SKILL.md 提取 ## Tool / ## 工具定义 段落"""
    section_headers = (
        "## tool", "## 工具", "## 工具定义", "## 执行", "## executor",
    )
    section = None
    block_lines: list[str] = []
    rest: list[str] = []

    for line in lines:
        lower = line.strip().lower()
        if lower in section_headers:
            section = "tool_def"
            continue
        if section == "tool_def":
            if line.startswith("## ") and lower not in section_headers:
                section = None
                rest.append(line)
                continue
            block_lines.append(line)
        else:
            rest.append(line)

    block = "\n".join(block_lines).strip()
    return (block if block else None), "\n".join(rest)
