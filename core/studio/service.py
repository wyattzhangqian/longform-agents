"""Agent Studio 服务 — Prompt 帮写、预览对话（带工具循环）"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

from config import DEEPSEEK_API_KEY
from core.logging import get_logger
from core.models.router import resolve_llm_config
from core.models.types import PLATFORM_DEFAULT
from core.studio.prompt_templates import PLATFORM_PROMPT_TEMPLATES, PROMPT_TEMPLATE_CATEGORIES
from tools.llm_client import LLMClient

_logger = get_logger("studio")

DEFAULT_MAX_TOOL_ITERATIONS = 5


def resolve_model(model: Optional[str]) -> str:
    cfg = resolve_llm_config(model=model)
    return cfg.get("model", "")


def llm_available() -> bool:
    cfg = resolve_llm_config()
    return bool(cfg.get("api_key") or DEEPSEEK_API_KEY)


def list_prompt_templates(category: Optional[str] = None) -> dict[str, Any]:
    templates: List[dict] = list(PLATFORM_PROMPT_TEMPLATES)
    categories = {c["id"]: c for c in PROMPT_TEMPLATE_CATEGORIES}
    if category and category != "all":
        templates = [t for t in templates if t.get("category") == category]
    return {
        "categories": [{"id": "all", "label": "全部"}, *categories.values()],
        "templates": templates,
        "total": len(templates),
    }


async def generate_prompt_assist(
    brief: str, role: str = "", model: Optional[str] = None,
) -> dict[str, Any]:
    if not llm_available():
        raise ValueError("未配置 LLM API Key，无法调用 LLM")
    cfg = resolve_llm_config(model=None if model == PLATFORM_DEFAULT else model)
    client = LLMClient(cfg)
    system = (
        "你是 Agent 人设（System Prompt）设计专家。"
        "根据用户描述生成结构化 Markdown System Prompt。"
        "只输出 JSON，格式："
        '{"system_prompt":"...","suggested_role":"...","suggested_capabilities":["..."]}'
    )
    user = f"角色名称：{role or '未指定'}\n职责描述：{brief or '通用助手'}"
    raw = await client.chat(user, system_prompt=system, temperature=0.7, max_tokens=2048)
    parsed = client._extract_json(raw)
    if parsed:
        try:
            data = json.loads(parsed)
            if isinstance(data, dict) and data.get("system_prompt"):
                return {
                    "system_prompt": str(data["system_prompt"]),
                    "suggested_role": str(data.get("suggested_role") or role or ""),
                    "suggested_capabilities": list(data.get("suggested_capabilities") or []),
                    "model": cfg.get("model"),
                }
        except json.JSONDecodeError:
            pass
    return {
        "system_prompt": raw.strip(), "suggested_role": role,
        "suggested_capabilities": [], "model": cfg.get("model"),
    }


async def _preview_system_with_skills(
    system_prompt: str, agent_name: str, skill_ids: Optional[list],
) -> str:
    base = system_prompt or f"你是 {agent_name}，专业、清晰、可执行地回复用户。"
    if not skill_ids:
        return base
    from core.skills.resolver import SkillResolver
    expanded = await SkillResolver.expand(skill_ids=skill_ids, include_instruction_tools=False)
    snippets = expanded.get("prompt_snippets") or []
    if snippets:
        return base + "\n\n" + "\n\n".join(snippets)
    return base


# ═══════════════════════════════════════════════════════
# 工具循环版预览对话
# ═══════════════════════════════════════════════════════

async def preview_agent_chat_with_tools(
    message: str,
    system_prompt: str = "",
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    agent_name: str = "Agent",
    skill_ids: Optional[list] = None,
    tool_ids: Optional[list] = None,
    use_tools: bool = False,
    history: Optional[List[Dict[str, str]]] = None,
    simulate_context: Optional[Dict[str, Any]] = None,
    agent_id: str = "",
) -> Dict[str, Any]:
    """Agent 预览对话（带工具循环 + 多轮记忆）。

    与 BaseAgent.execute() 一致的工具循环：
    LLM → 解析 needs_tool → 执行工具 → 结果回灌 → 最多 N 轮。
    """
    if not llm_available():
        raise ValueError("未配置 LLM API Key，无法调用 LLM")

    cfg = resolve_llm_config(model=None if model == PLATFORM_DEFAULT else model)
    cfg["temperature"] = temperature
    cfg["max_tokens"] = max_tokens

    sys_prompt = await _preview_system_with_skills(system_prompt, agent_name, skill_ids)

    # 解析可用工具
    tool_names: List[str] = []
    if use_tools:
        from core.capabilities.resolver import CapabilityResolver
        expanded = await CapabilityResolver.expand_ids(skill_ids=skill_ids, tool_ids=tool_ids)
        tool_names = expanded.get("tools") or []
        prompt_snippets = expanded.get("prompt_snippets") or []
        if prompt_snippets:
            sys_prompt += "\n\n" + "\n\n".join(prompt_snippets)

    # 构建 messages：系统提示 + 协作协议（Studio 模式：on_demand） + 历史 + 当前消息
    messages: List[Dict[str, str]] = []
    messages.append({"role": "system", "content": sys_prompt})

    # 工具策略 — Studio 预览：告诉 LLM 调用格式
    if tool_names:
        tool_list = ", ".join(tool_names)
        protocol = (
            f"## 协作协议\n"
            f"- 你已配备工具: {tool_list}\n"
            f"- 当用户的请求适合用工具完成时（如'画图'→generate_image，'生视频'→generate_video），"
            f"输出 JSON 调用工具: {{\"needs_tool\": true, \"tool_name\": \"工具名\", \"tool_args\": {{\"prompt\": \"描述\"}}}}\n"
            f"- 不要先用文字描述再调工具——直接调工具\n"
            f"- 工具执行完成后，根据结果简洁回复"
        )
        messages.append({"role": "system", "content": protocol})

    # [结合点] 在工具策略 message 之后、history 之前，插入模拟上下文
    if simulate_context:
        ctx_lines = ["## 协作协议（模拟）"]
        if simulate_context.get("phase_label"):
            ctx_lines.append(f"- 你正在执行阶段: {simulate_context['phase_label']}")
        if simulate_context.get("handoff_summary"):
            ctx_lines.append(f"- 上游交接摘要: {simulate_context['handoff_summary']}")
        if simulate_context.get("expected_outputs"):
            files = ", ".join(simulate_context["expected_outputs"])
            ctx_lines.append(f"- 请将产出写入文件: {files}")
        ctx_block = "\n".join(ctx_lines)
        messages.append({"role": "system", "content": ctx_block})

        if simulate_context.get("upstream_output"):
            messages.append({"role": "user", "content": f"## 上游产出\n\n{simulate_context['upstream_output']}"})

    for h in (history or []):
        role = h.get("role", "user")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": message})

    client = LLMClient(cfg)
    tool_results: List[Dict[str, str]] = []
    iterations = 0
    last_reply = ""

    while iterations < DEFAULT_MAX_TOOL_ITERATIONS:
        iterations += 1
        raw = await client.chat_messages(messages)

        # 检测 LLM 错误
        if raw.startswith("[LLM"):
            return {
                "reply": raw, "model": cfg.get("model"),
                "tool_calls": tool_results, "iterations": iterations,
            }

        # 解析 tool call 意图
        thought = _parse_tool_thought(raw)
        if not thought.get("needs_tool"):
            last_reply = raw
            break

        if not tool_names:
            last_reply = raw
            break

        tool_name = thought.get("tool_name", "")
        if tool_name not in tool_names:
            # LLM 请求了不在列表中的工具 → 告知并继续
            messages.append({"role": "assistant", "content": raw[:500]})
            messages.append({
                "role": "user",
                "content": f"工具 '{tool_name}' 不可用。可用工具: {', '.join(tool_names)}。请用可用工具完成任务。",
            })
            tool_results.append({"tool": tool_name, "result": f"[不可用] 可用: {', '.join(tool_names)}"})
            continue

        # 执行工具
        tool_args = thought.get("tool_args", {}) or {}
        tool_result = await _execute_preview_tool(tool_name, tool_args, agent_id=agent_id)
        tool_results.append({"tool": tool_name, "result": tool_result[:500]})

        # 结果回灌
        messages.append({"role": "assistant", "content": raw[:500]})
        messages.append({"role": "user", "content": f"[工具 {tool_name} 执行结果]\n{tool_result}"})

    return {
        "reply": last_reply or raw,
        "model": cfg.get("model"),
        "tool_calls": tool_results,
        "iterations": iterations,
    }


async def stream_preview_agent_chat_with_tools(
    message: str,
    system_prompt: str = "",
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    agent_name: str = "Agent",
    skill_ids: Optional[list] = None,
    tool_ids: Optional[list] = None,
    use_tools: bool = False,
    history: Optional[List[Dict[str, str]]] = None,
    agent_id: str = "",
) -> AsyncIterator[str]:
    """流式版 Agent 预览对话 — 首先运行工具循环，然后流式输出最终回复。

    yield 格式:
      - "__TOOL__:{json}" — 工具调用事件
      - 普通 token — LLM 回复文本
    """
    import json as _json

    if not llm_available():
        raise ValueError("未配置 LLM API Key")

    cfg = resolve_llm_config(model=None if model == PLATFORM_DEFAULT else model)
    cfg["temperature"] = temperature
    cfg["max_tokens"] = max_tokens

    sys_prompt = await _preview_system_with_skills(system_prompt, agent_name, skill_ids)

    tool_names: List[str] = []
    if use_tools:
        from core.capabilities.resolver import CapabilityResolver
        expanded = await CapabilityResolver.expand_ids(skill_ids=skill_ids, tool_ids=tool_ids)
        tool_names = expanded.get("tools") or []
        snippets = expanded.get("prompt_snippets") or []
        if snippets:
            sys_prompt += "\n\n" + "\n\n".join(snippets)

    messages: List[Dict[str, str]] = []
    messages.append({"role": "system", "content": sys_prompt})

    # 工具策略 — 告诉 LLM 调用格式
    if tool_names:
        tool_list = ", ".join(tool_names)
        protocol = (
            f"## 协作协议\n"
            f"- 你已配备工具: {tool_list}\n"
            f"- 当用户的请求适合用工具完成时（如'画图'→generate_image，'生视频'→generate_video），"
            f"输出 JSON 调用工具: {{\"needs_tool\": true, \"tool_name\": \"工具名\", \"tool_args\": {{\"prompt\": \"描述\"}}}}\n"
            f"- 不要先用文字描述再调工具——直接调工具\n"
            f"- 工具执行完成后，根据结果简洁回复"
        )
        messages.append({"role": "system", "content": protocol})

    for h in (history or []):
        role = h.get("role", "user")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": message})

    client = LLMClient(cfg)
    tool_results: List[Dict[str, str]] = []
    iterations = 0

    # Phase 1: 工具循环（非流式，快速完成）
    while tool_names and iterations < DEFAULT_MAX_TOOL_ITERATIONS:
        iterations += 1
        raw = await client.chat_messages(messages)
        if raw.startswith("[LLM"):
            yield f"__ERROR__:{raw}"
            return

        thought = _parse_tool_thought(raw)
        if not thought.get("needs_tool"):
            messages.append({"role": "assistant", "content": raw})
            break

        tool_name = thought.get("tool_name", "")
        if tool_name not in tool_names:
            messages.append({"role": "assistant", "content": raw[:500]})
            messages.append({
                "role": "user",
                "content": f"工具 '{tool_name}' 不可用。可用: {', '.join(tool_names)}",
            })
            continue

        tool_args = thought.get("tool_args", {}) or {}
        yield f"__TOOL_START__:{_json.dumps({'tool': tool_name, 'args': tool_args}, ensure_ascii=False)}\n"
        tool_result = await _execute_preview_tool(tool_name, tool_args, agent_id=agent_id)
        tool_results.append({"tool": tool_name, "result": tool_result[:500]})
        yield f"__TOOL_END__:{_json.dumps({'tool': tool_name, 'result': tool_result[:300]}, ensure_ascii=False)}\n"

        messages.append({"role": "assistant", "content": raw[:500]})
        messages.append({"role": "user", "content": f"[工具结果]\n{tool_result}"})

    # Phase 2: 流式输出最终回复
    async for delta in client.chat_messages_stream(messages):
        if not delta.startswith("__REASONING__:"):
            yield delta


# ═══════════════════════════════════════════════════════
# 旧接口（兼容）
# ═══════════════════════════════════════════════════════

async def preview_agent_chat(
    message: str, system_prompt: str = "", model: Optional[str] = None,
    temperature: float = 0.7, max_tokens: int = 2048, agent_name: str = "Agent",
    skill_ids: Optional[list] = None, tool_ids: Optional[list] = None,
    use_tools: bool = False, history: Optional[List[Dict[str, str]]] = None,
    agent_id: str = "",
) -> dict[str, Any]:
    """单 Agent 预览对话 — 委托给工具循环版"""
    return await preview_agent_chat_with_tools(
        message=message, system_prompt=system_prompt, model=model,
        temperature=temperature, max_tokens=max_tokens, agent_name=agent_name,
        skill_ids=skill_ids, tool_ids=tool_ids, use_tools=use_tools, history=history,
        agent_id=agent_id,
    )


async def stream_preview_agent_chat(
    message: str, system_prompt: str = "", model: Optional[str] = None,
    temperature: float = 0.7, max_tokens: int = 2048, agent_name: str = "Agent",
    skill_ids: Optional[list] = None, tool_ids: Optional[list] = None,
    use_tools: bool = False, history: Optional[List[Dict[str, str]]] = None,
    agent_id: str = "",
) -> AsyncIterator[str]:
    """流式版 — 委托给工具循环流式版"""
    async for delta in stream_preview_agent_chat_with_tools(
        message=message, system_prompt=system_prompt, model=model,
        temperature=temperature, max_tokens=max_tokens, agent_name=agent_name,
        skill_ids=skill_ids, tool_ids=tool_ids, use_tools=use_tools, history=history,
        agent_id=agent_id,
    ):
        yield delta


# ═══════════════════════════════════════════════════════
# 工具执行
# ═══════════════════════════════════════════════════════

def _parse_tool_thought(text: str) -> dict:
    """从 LLM 输出解析 tool call 意图"""
    from core.communication.content_format import iter_tool_intents_from_text
    intents = list(iter_tool_intents_from_text(text))
    if intents:
        return intents[0]
    from core.agent.tools import ToolExecutor
    parsed = ToolExecutor.parse_tool_call_from_text(text)
    if parsed:
        return {"needs_tool": True, **parsed}
    return {"needs_tool": False}


async def _execute_preview_tool(tool_name: str, tool_args: dict, agent_id: str = "") -> str:
    """在预览上下文中执行工具（不依赖 Agent 运行时）"""
    from core.agent.tools import ToolRegistry
    registry = ToolRegistry.get_instance()
    tool = registry.get(tool_name)
    if not tool:
        return f"[错误] 未知工具: {tool_name}"

    try:
        handler = tool.handler
        import asyncio
        # 注入 _agent_id 供 generate_image/video/music 读取 Agent model_profile
        if agent_id:
            tool_args["_agent_id"] = agent_id
        if asyncio.iscoroutinefunction(handler):
            result = await handler(**tool_args)
        else:
            result = handler(**tool_args)
        return str(result)
    except Exception as e:
        return f"[错误] 工具执行失败: {e}"
