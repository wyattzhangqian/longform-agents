"""Skill prompt_snippet 清洗 — 避免把代码示例注入 Agent 上下文"""

from __future__ import annotations

import re

MAX_PROMPT_SNIPPET_CHARS = 1200

# 优先作为「行为说明」的章节（英文 SkillHub 包常见）
WHEN_TO_USE_HEADERS = frozenset({
    "## when to use", "## 何时使用", "## 适用场景", "## 使用场景",
})

# 次选；常含代码示例，需过滤
USAGE_HEADERS = frozenset({
    "## 使用", "## usage", "## 用法",
})

PROMPT_HEADERS = frozenset({
    "## prompt", "## 提示", "## prompt_snippet", "## 技能说明",
})


def strip_code_fences(text: str) -> str:
    text = re.sub(r"```[\w]*\n[\s\S]*?```", "", text)
    text = re.sub(r"`[^`\n]+`", "", text)
    return text.strip()


def is_code_heavy(text: str) -> bool:
    if not text.strip():
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    code_like = 0
    for ln in lines:
        if ln.startswith("```"):
            return True
        if re.match(r"^(from|import|def |class |async def |# |mem\.|await )", ln):
            code_like += 1
        if re.match(r"^[a-z_]+\s*=", ln) and "http" not in ln:
            code_like += 1
    return code_like >= max(2, len(lines) // 3)


def normalize_prompt_snippet(text: str, *, max_len: int = MAX_PROMPT_SNIPPET_CHARS) -> str:
    cleaned = strip_code_fences(text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return ""
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def build_catalog_prompt_snippet(name: str, description: str) -> str:
    """SkillHub 同步：用 API 描述作注入片段，不用 SKILL.md 里的代码块"""
    desc = normalize_prompt_snippet(description or "", max_len=900)
    title = (name or "技能").strip()
    if desc:
        return f"## 技能：{title}\n{desc}"
    return f"## 技能：{title}\n在合适的场景下按该技能说明协助用户（详见技能描述）。"


def compose_prompt_snippet(
    *,
    name: str,
    description_lines: list[str],
    prompt_lines: list[str],
    when_to_use_lines: list[str],
    usage_lines: list[str],
) -> str:
    """从 SKILL.md 各章节合成安全的 prompt_snippet"""
    for candidate in (
        "\n".join(prompt_lines),
        "\n".join(when_to_use_lines),
        "\n".join(usage_lines),
        "\n".join(description_lines[-4:]),
    ):
        normalized = normalize_prompt_snippet(candidate)
        if normalized and not is_code_heavy(normalized):
            title = (name or "技能").strip()
            if not normalized.startswith("##"):
                return f"## 技能：{title}\n{normalized}"
            return normalized

    desc = normalize_prompt_snippet("\n".join(description_lines[:3]))
    title = (name or "技能").strip()
    if desc:
        return f"## 技能：{title}\n{desc}"
    return ""
