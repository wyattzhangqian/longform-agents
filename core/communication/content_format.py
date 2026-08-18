"""从 Agent 结果 / 产物文件中提取可展示的 Markdown 或纯文本"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional


_DISPLAY_KEYS = ("content", "result", "summary", "markdown", "text", "output", "body")
_SKIP_AS_PRIMARY = frozenset({"tool_use", "tool_calls", "needs_tool", "tool_name", "tool_args"})
_FENCE_JSON_RE = re.compile(r"^```(?:json)?\s*\n([\s\S]*?)\n```\s*$", re.IGNORECASE)
_FENCE_BLOCKS_RE = re.compile(r"```(?:json)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_FENCE_MD_RE = re.compile(r"```(?:markdown|md)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_FILE_WRITE_LOOSE_RE = re.compile(
    r'"tool_name"\s*:\s*"file_write"[\s\S]*?"path"\s*:\s*"([^"]+)"[\s\S]*?"content"\s*:\s*"([\s\S]*?)"\s*\n\s*\}',
    re.MULTILINE,
)
_VAULT_SUFFIX_RE = re.compile(r"_v\d+$")

# 成对的 XML 工具调用标记（含其参数内容），落盘产物前剥离
# 标签名兼容两种写法：read_file/file_read、write_file/file_write
_XML_TOOL_PAIR_RE = re.compile(
    r"<(read_file|file_read|write_file|file_write|tool_call|tool_use|function_calls|invoke|ask_peer)\b[^>]*>[\s\S]*?</\1>",
    re.IGNORECASE,
)
# 未闭合的 XML 工具调用标记（agent 输出被截断时残留），从该标记起截断
_XML_TOOL_OPEN_RE = re.compile(
    r"<(read_file|file_read|write_file|file_write|tool_call|tool_use|function_calls|invoke|ask_peer)\b[^>]*>",
    re.IGNORECASE,
)
# markdown 标题行（# ~ ######，至多3个前导空格）
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE)

# 正文末尾的对话式/自我评价式元评论标记（agent 在交付正文后追加的互动或设计说明；
# 均选用几乎不可能出现在叙事正文里的特征词，降低误伤）
_TRAILING_DIALOGUE_MARKERS = (
    "请告诉我", "请随时告诉我", "我可以继续", "如果你", "若你", "需要调整",
    "随时调整", "随时告诉", "随时告知", "期待你的反馈", "欢迎提出", "如需",
    "如果需要", "希望这个", "希望这份", "以上是", "以上便是", "以上即为",
    "供你参考", "供您参考", "你可以提出", "你可以告诉",
    # 自我评价 / 设计说明区块（✅ 清单、任务总结、创作阐述等，不会出现在叙事正文）
    "格式规范", "设计说明", "设计思路", "创作说明", "写作说明", "本章设定",
    "符合网文", "结构完整", "爽点驱动", "符合阅读习惯", "符合.*阅读习惯",
    "✅", "任务完成情况", "人设承接", "悬念设置", "伏笔设置", "留下钩子",
    "承接：", "完成了“", "完成了\"", "爽点，且", "特质。", "为核心概念",
)


# 正文前/中混入的"设定承接"引用块起始（agent 协作自检行为产物）
_META_QUOTE_START_RE = re.compile(
    r"^\s*>\s*(?:\*\*)?(设定承接|设定说明|创作说明|写作说明|本章设定|风格要求|风格设定|"
    r"质量自检|自检清单|一致性检查|承接检查|承接说明|设定引用)",
)
# 正文末尾的"编号自我评价"行（如 "3. **风格**：…"、"4. **境界体系**：…"）
_META_NUMBERED_RE = re.compile(
    r"^\s*\d+\.\s*\*\*(风格|境界|设定|结构|检查|承接|伏笔|节奏|人设|一致性|质量|"
    r"世界观|人物|叙事|画面|镜头|台词|配音|转场|时长|自检)",
)
_HR_LINE_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")


def _strip_meta_blocks(text: str) -> str:
    """剥离混入正文中的 agent 元评论块（设定承接引用块、编号自我评价列表）。

    这些块来自创作类 agent 被提示词要求的"设定承接/质量自检"行为，非交付正文。
    逐行扫描：命中元评论起始行后进入跳过态，跳过其延续行（引用行/列表行），
    遇空行或正文行结束跳过态。最后清理孤立的水平分隔线与多余空行。
    """
    lines = text.split("\n")
    out: list = []
    skipping = False
    for ln in lines:
        if _META_QUOTE_START_RE.match(ln) or _META_NUMBERED_RE.match(ln):
            skipping = True
            continue
        if skipping:
            s = ln.strip()
            # 元评论块的延续行：引用行、列表项；空行则结束该块
            if s.startswith(">") or s.startswith("- ") or s.startswith("* "):
                continue
            if s == "":
                skipping = False
                continue
            skipping = False  # 正文行：结束跳过
        out.append(ln)
    cleaned = "\n".join(out)
    # 清理因剥离而孤立的水平分隔线（前后都是空行的 ---）与 3+ 连续空行
    cleaned = re.sub(r"\n{2,}\s*(?:-{3,}|\*{3,}|_{3,})\s*\n{2,}", "\n\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _is_meta_commentary(tail: str) -> bool:
    """判断末尾段落是否为 agent 元评论（对话互动或自我评价），而非叙事正文。"""
    if not tail or tail.lstrip().startswith("#"):
        return False
    hits = sum(1 for m in _TRAILING_DIALOGUE_MARKERS if m in tail)
    if hits == 0:
        return False
    # 列表/说明区块常以 "- " 或 "关于" 起始，元评论特征更强
    meta_start = tail.lstrip().startswith(("- ", "* ", "关于", "注", "说明", "以上"))
    return hits >= 2 or meta_start or len(tail) < 400


def _strip_trailing_dialogue(text: str) -> str:
    """剥离正文末尾的对话式/自我评价式元评论尾巴。

    从后往前按空行分段扫描，剥离元评论段落，遇到疑似叙事的正文段落即停
    （保守，避免误删正文结尾）。
    """
    paras = text.split("\n\n")
    while len(paras) > 1:
        tail = paras[-1].strip()
        if not tail:
            paras.pop()
            continue
        if _is_meta_commentary(tail):
            paras.pop()
        else:
            break
    return "\n\n".join(paras).strip()

# Agent 过程/思考用语 — 出现在交付物正文中则视为污染
_AGENT_PROCESS_MARKERS = (
    "web_search",
    "file_write",
    "needs_tool",
    "ask_peer",
    "剧本师已就位",
    "第一步",
    "第二步",
    "工具执行",
    "立即执行",
    "现在开始搜索",
    "我的流程",
    "已完成搜索",
    "产出文件：",
    "我将直接写入",
    "我将直接产出",
)


def _parse_fenced_json(text: str) -> Any | None:
    t = (text or "").strip()
    m = _FENCE_JSON_RE.match(t)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if t.startswith("```"):
        lines = t.split("\n")
        if len(lines) >= 2:
            body = "\n".join(lines[1:])
            if body.rstrip().endswith("```"):
                body = body.rstrip()[:-3].strip()
            try:
                return json.loads(body)
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
    return None


def _tool_args_dict(tool: dict) -> dict:
    args = tool.get("tool_args") or tool.get("arguments") or {}
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return args if isinstance(args, dict) else {}


def extract_fenced_markdown_bodies(text: str) -> List[str]:
    """从 ```markdown / ```md 代码块提取正文。"""
    if not text:
        return []
    return [m.group(1).strip() for m in _FENCE_MD_RE.finditer(text) if m.group(1).strip()]


def contains_agent_process_preamble(text: str, *, window: int = 1200) -> bool:
    """正文开头是否混入了 Agent 思考/工具说明（非交付文档）。"""
    head = (text or "")[:window]
    if not head.strip():
        return True
    hits = sum(1 for m in _AGENT_PROCESS_MARKERS if m in head)
    if hits >= 2:
        return True
    if hits >= 1 and not head.lstrip().startswith("#"):
        return True
    return False


def is_clean_deliverable_body(text: str, filename: str) -> bool:
    """是否为可交付的最终文档（非过程文本）。"""
    body = (text or "").strip()
    if not body:
        return False
    if is_ephemeral_agent_output(body):
        return False
    if contains_agent_process_preamble(body):
        return False
    name = Path(filename or "").name
    if name.endswith(".json"):
        try:
            json.loads(body)
            return True
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
    if name.endswith(".md"):
        return body.lstrip().startswith("#") and (
            "\n## " in body or "\n### " in body or "\n- " in body
        )
    return True


def _split_markdown_sections(text: str) -> List[str]:
    """按一级标题 `# ` 切分文档块。"""
    lines = (text or "").split("\n")
    sections: List[str] = []
    current: List[str] = []
    for line in lines:
        if line.lstrip().startswith("# ") and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())
    return [s for s in sections if s.strip()]


def extract_deliverable_body(text: str, filename: str) -> str:
    """从 Agent 混合输出中提取指定交付文件的干净正文（不含思考过程）。"""
    body = (text or "").strip()
    if not body:
        return ""
    name = Path(filename or "").name
    extracted = extract_deliverable_for_path(body, name)
    if extracted.strip() and is_clean_deliverable_body(extracted, name):
        return extracted.strip()
    for block in reversed(extract_fenced_markdown_bodies(body)):
        if block.lstrip().startswith("#") and is_clean_deliverable_body(block, name):
            return block.strip()
    if name.endswith(".md"):
        candidates = _split_markdown_sections(body)
        for section in reversed(candidates):
            if not section.lstrip().startswith("#"):
                continue
            if contains_agent_process_preamble(section, window=400):
                continue
            if "\n## " in section or "\n### " in section or len(section) > 400:
                if is_clean_deliverable_body(section, name):
                    return section.strip()
        for i, line in enumerate(body.split("\n")):
            if line.lstrip().startswith("# ") and len(line.strip()) > 2:
                tail = "\n".join(body.split("\n")[i:]).strip()
                if is_clean_deliverable_body(tail, name):
                    return tail
    if name.endswith(".json"):
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            candidate = body[start:end + 1]
            try:
                json.loads(candidate)
                return candidate.strip()
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    return ""


def refine_agent_deliverable(text: str, filename: str = "") -> str:
    """从 Agent 流式混合输出中提炼干净的最终交付正文（工作台通用）。

    Agent 直接口头输出时，产物常混入思考/独白/工具调用标记，如：
        "好的，收到任务！我先读取方案… <file_read>{...}</file_read> 现在为你创作。
         ### 第一章：千年的梦  林尘猛然睁开眼。…"
    本函数剥离这些过程性内容，只保留交付正文（上例中 "### 第一章…" 起）。

    策略（逐级回退，保证通用且不空转）：
      1) 复用 extract_deliverable_body：处理 file_write / fenced-markdown 情况
      2) 剥离成对 XML 工具标记；遇未闭合标记（输出截断）则自该处截断
      3) 从首个 markdown 标题行(# ~ ######)起截取正文 —— 交付文档几乎都以标题开头，
         而 agent 的思考/口语前缀极少以标题开头
    均无法提炼时返回 ""（由调用方决定兜底，通常是保留原文）。
    """
    body = (text or "").strip()
    if not body:
        return ""
    name = Path(filename or "output.md").name

    # —— 定位正文候选（两条路径，取第一条成功的）——
    candidate = ""
    # 路径1：file_write 载荷 / fenced markdown 块 / 一级标题切分
    extracted = extract_deliverable_body(body, name)
    if extracted.strip():
        candidate = extracted.strip()
    else:
        # 路径2：剥离 XML 工具标记后，从首个 markdown 标题行截取
        cleaned = _XML_TOOL_PAIR_RE.sub("\n", body)
        cleaned = _XML_TOOL_OPEN_RE.split(cleaned)[0] if _XML_TOOL_OPEN_RE.search(cleaned) and not _HEADING_LINE_RE.search(cleaned) else cleaned
        cleaned = cleaned.strip()
        m = _HEADING_LINE_RE.search(cleaned)
        if m:
            candidate = cleaned[m.start():].strip() if m.start() > 0 else cleaned

    if not candidate:
        return ""

    # —— 统一清理：剥离 agent 元评论块（设定承接引用块、编号自我评价）与对话式尾巴 ——
    candidate = _strip_meta_blocks(candidate)
    candidate = _strip_trailing_dialogue(candidate)
    candidate = candidate.strip()
    if not candidate:
        return ""

    # 校验（放宽：标题开头或较长内容即视为正文）
    if is_clean_deliverable_body(candidate, name) or len(candidate) > 200:
        return candidate
    return ""


_REFINE_SYSTEM_PROMPT = (
    "你是精准的内容提炼器。从 AI 助手的原始输出中提取最终交付正文，"
    "只输出纯正文，不多一字，不加任何解释或标记。"
)

_REFINE_USER_TEMPLATE = (
    "下面是某个 AI 助手的原始输出，混杂了它的思考过程、设定说明、自我评价和与用户的对话。\n\n"
    "请提取出**最终交付正文**（如小说正文、文档内容本身），要求：\n"
    "- 只保留交付内容本身（章节/文档标题+正文）\n"
    "- 去除所有思考独白、工具调用、设定承接/说明、自我评价/检查清单、与用户的对话互动\n"
    "- 正文原样保留，不改写、不总结、不补充\n"
    "- 直接输出纯正文，不加任何解释或额外标记\n\n"
    "原始输出：\n\"\"\"\n{body}\n\"\"\""
)


async def refine_with_llm(text: str, filename: str, llm_client, max_input_chars: int = 12000) -> str:
    """用 LLM 把 Agent 混合输出提炼为纯交付正文（最可靠，语义理解边界）。

    规则提炼无法覆盖 LLM 输出的多样性（思考/设定说明/自我评价/对话形式层出不穷），
    LLM 提炼能准确区分"正文 vs 过程"。返回 "" 表示失败（调用方用规则结果兜底）。
    """
    body = (text or "").strip()
    if not body:
        return ""
    snippet = body[:max_input_chars]
    prompt = _REFINE_USER_TEMPLATE.format(body=snippet)
    try:
        r = await llm_client.chat(prompt, system_prompt=_REFINE_SYSTEM_PROMPT)
    except Exception:
        return ""
    if not r or r.lstrip().startswith("[LLM"):
        return ""
    return r.strip()


def needs_llm_refine(text: str) -> bool:
    """启发式判断规则提炼结果是否仍含 agent 污染（需升级 LLM 精提炼）。

    宁可多升级（多用一次 LLM）也不放过污染。覆盖：过程前缀、设定承接引用块、
    编号自我评价、末尾对话式/自我评价式元评论。
    """
    t = (text or "").strip()
    if not t:
        return True
    if contains_agent_process_preamble(t):
        return True
    if _META_NUMBERED_RE.search(t) or _META_QUOTE_START_RE.search(t):
        return True
    tail = t[-400:]
    if any(m in tail for m in _TRAILING_DIALOGUE_MARKERS):
        return True
    return False


# ── XML 格式工具调用解析（工作台通用，兼容底层 agent 的多样输出）──
# 注册表工具名（动词在后）+ 常见别名（动词在前），统一归一到注册表
_TOOL_NAME_ALIASES = {
    "read_file": "file_read",
    "write_file": "file_write",
    "search_web": "web_search",
    "exec_shell": "shell_exec",
    "run_shell": "shell_exec",
    "query_knowledge": "knowledge_query",
    "query_asset": "asset_query",
    "query_assets": "asset_query",
}
_KNOWN_TOOL_NAMES = (
    "file_read", "file_write", "web_search", "shell_exec", "generate_image",
    "generate_video", "generate_music", "asset_query", "llm_call",
    "knowledge_query", "read_url", "ask_peer",
    "read_file", "write_file", "search_web", "exec_shell", "run_shell",
    "query_knowledge", "query_asset", "query_assets",
)
_XML_TOOL_CALL_RE = re.compile(
    r"<(" + "|".join(_KNOWN_TOOL_NAMES) + r")\b([^>]*)>([\s\S]*?)</\1>",
    re.IGNORECASE,
)
# 自闭合形式：<read_file path="a.md" />
_XML_TOOL_SELF_CLOSING_RE = re.compile(
    r"<(" + "|".join(_KNOWN_TOOL_NAMES) + r")\b([^>]*?)/>",
    re.IGNORECASE,
)
# <invoke name="tool"> 形式：DeepSeek DSML 等模型的原生工具调用格式
#   <invoke name="file_read"><…parameter name="path">x</…parameter></invoke>
_INVOKE_BLOCK_RE = re.compile(r'<invoke\s+name="(\w+)"[^>]*>([\s\S]*?)</invoke>', re.IGNORECASE)
_INVOKE_PARAM_RE = re.compile(r'parameter\s+name="(\w+)"[^>]*>([\s\S]*?)</[^>]*?parameter\s*>', re.IGNORECASE)
_XML_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_XML_SUBTAG_RE = re.compile(r"<(\w+)>([\s\S]*?)</\1>")


def normalize_tool_name(name: str) -> str:
    """工具名归一化到注册表（动词在前写法 → 注册表动词在后写法）。"""
    n = (name or "").strip()
    return _TOOL_NAME_ALIASES.get(n.lower(), n)


def _parse_xml_tool_args(inner: str, attrs: str) -> dict:
    """解析 XML 工具调用的参数：属性 / 子标签 / JSON / 纯文本。"""
    args = dict(_XML_ATTR_RE.findall(attrs or ""))
    inner = (inner or "").strip()
    if not inner:
        return args
    # 子标签形式：<path>x</path><content>y</content>
    subtags = _XML_SUBTAG_RE.findall(inner)
    if subtags:
        for k, v in subtags:
            args[k] = v.strip()
        return args
    # JSON 形式：<read_file>{"path": "x"}</read_file>
    if inner.startswith("{"):
        try:
            obj = json.loads(inner)
            if isinstance(obj, dict):
                args.update(obj)
                return args
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # 纯文本：file_read/file_write 视为 path/content
    if "path" not in args:
        args["path"] = inner
    return args


def _iter_xml_tool_intents(text: str):
    """从 Agent 原文解析 XML 格式工具调用（<read_file>…</read_file> 等）。"""
    if not text:
        return
    for m in _XML_TOOL_CALL_RE.finditer(text):
        yield {
            "needs_tool": True,
            "tool_name": normalize_tool_name(m.group(1)),
            "tool_args": _parse_xml_tool_args(m.group(3), m.group(2)),
        }
    # 自闭合形式（属性参数）
    for m in _XML_TOOL_SELF_CLOSING_RE.finditer(text):
        yield {
            "needs_tool": True,
            "tool_name": normalize_tool_name(m.group(1)),
            "tool_args": dict(_XML_ATTR_RE.findall(m.group(2) or "")),
        }
    # <invoke name="tool"> 形式（DeepSeek DSML 等模型原生工具调用格式）
    for m in _INVOKE_BLOCK_RE.finditer(text):
        inner = m.group(2)
        args = {k: v.strip() for k, v in _INVOKE_PARAM_RE.findall(inner)}
        yield {
            "needs_tool": True,
            "tool_name": normalize_tool_name(m.group(1)),
            "tool_args": args,
        }


def _intent_from_tool_dict(tool: dict) -> dict | None:
    """将 tool_use / tool_calls 条目规范为 needs_tool 意图。"""
    if not isinstance(tool, dict):
        return None
    if tool.get("needs_tool"):
        return tool
    name = str(tool.get("tool_name") or tool.get("name") or "").strip()
    args = _tool_args_dict(tool)
    fn = tool.get("function")
    if isinstance(fn, dict):
        name = name or str(fn.get("name") or "").strip()
        if not args:
            raw = fn.get("arguments")
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        args = parsed
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            elif isinstance(raw, dict):
                args = raw
    if not name:
        return None
    return {"needs_tool": True, "tool_name": normalize_tool_name(name), "tool_args": args}


def iter_tool_intents_from_text(text: str):
    """从 Agent 原文中按顺序解析 tool call 意图（needs_tool / tool_use / tool_calls）。"""
    if not text:
        return

    def _yield_from_obj(obj: dict):
        if not isinstance(obj, dict):
            return
        if obj.get("needs_tool"):
            if obj.get("tool_name"):
                obj = {**obj, "tool_name": normalize_tool_name(obj["tool_name"])}
            yield obj
            return
        direct = _intent_from_tool_dict(obj)
        if direct:
            yield direct
            return
        for key in ("tool_use", "tool_calls"):
            tools = obj.get(key)
            if not isinstance(tools, list):
                continue
            for tool in tools:
                intent = _intent_from_tool_dict(tool)
                if intent:
                    yield intent

    found_fenced = False
    for block in _FENCE_BLOCKS_RE.findall(text):
        block = block.strip()
        if not block:
            continue
        try:
            obj = json.loads(block)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for intent in _yield_from_obj(obj):
            found_fenced = True
            yield intent

    # 兼容整段单一 JSON（无代码块且 fenced 无意图时）
    if not found_fenced and "{" in text:
        start = text.find("{")
        end = text.rfind("}") + 1
        if end > start:
            try:
                obj = json.loads(text[start:end])
            except (json.JSONDecodeError, TypeError, ValueError):
                obj = None
            if obj is not None:
                yield from _yield_from_obj(obj)

    # XML 格式工具调用（<read_file>…</read_file> 等）：底层 agent 可能输出此格式，
    # 作为补充统一解析（fenced JSON 优先，此处兜底，保证 agent 输出总能被理解）
    yield from _iter_xml_tool_intents(text)


def extract_file_write_payloads(text: str) -> List[tuple[str, str]]:
    """从 Agent 输出中提取 file_write 的 (path, content) 列表。"""
    if not text:
        return []
    found: List[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(path: str, content: str) -> None:
        p = (path or "").strip().replace("\\", "/").lstrip("/")
        if not p or content is None:
            return
        body = _unescape_literal_escapes(str(content).strip())
        if not body:
            return
        key = (Path(p).name, body[:120])
        if key in seen:
            return
        seen.add(key)
        found.append((p, body))

    for block in _FENCE_BLOCKS_RE.findall(text):
        try:
            obj = json.loads(block.strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            for m in _FILE_WRITE_LOOSE_RE.finditer(block):
                _add(m.group(1), m.group(2))
            continue
        if not isinstance(obj, dict):
            continue
        tool_name = str(obj.get("tool_name") or obj.get("name") or "")
        if tool_name == "file_write":
            args = _tool_args_dict(obj)
            _add(str(args.get("path") or ""), str(args.get("content") or ""))
        for tool in obj.get("tool_use") or obj.get("tool_calls") or []:
            if not isinstance(tool, dict):
                continue
            if str(tool.get("tool_name") or tool.get("name") or "") != "file_write":
                continue
            args = _tool_args_dict(tool)
            _add(str(args.get("path") or ""), str(args.get("content") or ""))

    for m in _FILE_WRITE_LOOSE_RE.finditer(text):
        _add(m.group(1), m.group(2))

    return found


def extract_deliverable_for_path(text: str, filename: str) -> str:
    """从 Agent 混合输出中提取指定交付文件的 file_write 正文。"""
    target = Path(filename or "").name
    if not target:
        return ""
    for path, content in reversed(extract_file_write_payloads(text)):
        if Path(path).name == target and content.strip():
            return content.strip()
    return ""


def _markdown_from_tool_use(tools: Any) -> str:
    """从 tool_use 列表中提取 file_write 的 Markdown 正文。"""
    if not isinstance(tools, list):
        return ""
    parts: List[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("tool_name") or tool.get("name") or "")
        if name != "file_write":
            continue
        args = _tool_args_dict(tool)
        path = str(args.get("path") or "output")
        content = args.get("content")
        if content is None:
            continue
        body = _unescape_literal_escapes(str(content).strip())
        if not body:
            continue
        parts.append(f"## {path}\n\n{body}")
    return "\n\n---\n\n".join(parts)


def _extract_tool_use_payload(value: Any) -> str:
    if isinstance(value, dict):
        tools = value.get("tool_use") or value.get("tool_calls")
        md = _markdown_from_tool_use(tools)
        if md.strip():
            return md
    if isinstance(value, str):
        fenced = _parse_fenced_json(value)
        if fenced is not None:
            return _extract_tool_use_payload(fenced)
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return ""
        return _extract_tool_use_payload(parsed)
    return ""


def _unescape_literal_escapes(text: str) -> str:
    """LLM / JSON 里常见的字面量 \\n → 换行"""
    if "\\n" not in text:
        return text
    if text.count("\n") >= text.count("\\n") / 2:
        return text
    return text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')


def extract_display_content(value: Any, *, max_len: int = 0) -> str:
    """递归解析 JSON 包装，返回适合前端 Markdown 渲染的正文。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        if value.get("tool_use") or value.get("tool_calls"):
            md = _extract_tool_use_payload(value)
            if md.strip():
                return _truncate(_unescape_literal_escapes(md), max_len)
            for key in _DISPLAY_KEYS:
                if key in value and value[key] is not None:
                    inner = extract_display_content(value[key], max_len=0)
                    if inner.strip() and "tool_use" not in inner[:80]:
                        return _truncate(inner, max_len)
            raw = value.get("raw")
            if isinstance(raw, str) and raw.strip():
                inner = extract_display_content(raw, max_len=0)
                if inner.strip():
                    return _truncate(_unescape_literal_escapes(inner), max_len)
            return ""
        # Vault 落盘：{ agent_id, role, result, raw }
        for key in ("result", "raw", *_DISPLAY_KEYS):
            if key not in value or value[key] is None:
                continue
            inner = extract_display_content(value[key], max_len=0)
            if inner.strip():
                return _truncate(_unescape_literal_escapes(inner), max_len)
        return ""
    if not isinstance(value, str):
        value = str(value)
    raw_text = value.strip()
    if not raw_text:
        return ""
    tool_md = _extract_tool_use_payload(raw_text)
    if tool_md.strip():
        return _truncate(_unescape_literal_escapes(tool_md), max_len)
    text = _unescape_literal_escapes(raw_text)
    fenced = _parse_fenced_json(text)
    if fenced is None:
        fenced = _parse_fenced_json(raw_text)
    if fenced is not None:
        inner = extract_display_content(fenced, max_len=0)
        if inner.strip():
            return _truncate(inner, max_len)
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
            inner = extract_display_content(parsed, max_len=0)
            if inner.strip():
                text = inner
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return _truncate(text, max_len)


def _truncate(text: str, max_len: int) -> str:
    if max_len and len(text) > max_len:
        return text[:max_len]
    return text


def is_markdown_path(path: str) -> bool:
    p = (path or "").lower()
    return p.endswith(".md") or p.endswith(".markdown") or p.endswith(".mdx")


def is_deliverable_file_content(text: str, filename: str) -> bool:
    """磁盘上的期望交付文件是否已是可读交付物（非工具中间态）。"""
    body = (text or "").strip()
    if not body:
        return False
    name = Path(filename or "").name
    if name.endswith(".json"):
        try:
            json.loads(body)
            return True
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
    if is_ephemeral_agent_output(body):
        return False
    if "needs_tool" in body[:600] and "file_write" not in body[:600]:
        return False
    cleaned = extract_deliverable_body(body, name)
    if cleaned:
        return True
    return is_clean_deliverable_body(body, name)


def is_ephemeral_agent_output(value: Any, text: str = "") -> bool:
    """对话/工具中间态 — 不应作为项目产物落盘或在「产物」Tab 展示。"""
    if isinstance(value, dict):
        if value.get("needs_tool"):
            return True
        tool_name = str(value.get("tool_name") or "")
        if tool_name in {"ask_peer", "handoff", "status", "acknowledge"}:
            return True
        tools = value.get("tool_use") or value.get("tool_calls")
        if tools:
            if _markdown_from_tool_use(tools).strip():
                return False
            return True
        display = extract_display_content(value)
        if display.strip():
            return False
        return True
    body = (text or "").strip()
    if not body and isinstance(value, str):
        body = value.strip()
    if not body:
        return True
    if body.startswith("{") and body.endswith("}"):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return is_ephemeral_agent_output(parsed)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    fenced = _parse_fenced_json(body)
    if isinstance(fenced, dict):
        return is_ephemeral_agent_output(fenced)
    if looks_like_markdown(body):
        return False
    return False


def looks_like_markdown(text: str) -> bool:
    t = (text or "").lstrip()[:800]
    if not t:
        return False
    return (
        t.startswith("#")
        or "\n## " in t
        or "\n- " in t
        or "**" in t
        or "```" in t
    )
