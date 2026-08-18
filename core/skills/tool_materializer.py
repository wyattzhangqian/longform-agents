"""Skill 导入物化 — 将 manifest.tool_definitions 注册为可执行 Tool

对用户：Skill 是唯一能力层；导入即注册，无需单独配置 Tool。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from core.agent.tools import ToolDefinition, ToolRegistry

logger = logging.getLogger(__name__)

_BUILTIN_TOOL_NAMES = frozenset({"file_read", "file_write", "web_search"})


def skill_tool_namespace(skill_id: str) -> str:
    return f"skill/{skill_id}"


def has_custom_tools(manifest: Any) -> bool:
    if manifest is None:
        return False
    defs = manifest.get("tool_definitions") if isinstance(manifest, dict) else getattr(manifest, "tool_definitions", None)
    return bool(defs)


def _manifest_dict(manifest: Any) -> dict:
    if manifest is None:
        return {}
    if isinstance(manifest, dict):
        return manifest
    if hasattr(manifest, "model_dump"):
        return manifest.model_dump()
    return {}


def _default_parameters(props: Optional[dict] = None, required: Optional[list] = None) -> dict:
    props = props or {}
    required = required or list(props.keys())
    return {"type": "object", "properties": props, "required": required}


def _build_http_handler(defn: dict) -> Callable:
    method = (defn.get("method") or "GET").upper()
    url_template = defn.get("url_template") or defn.get("url") or ""
    headers = dict(defn.get("headers") or {})
    max_chars = int(defn.get("max_response_chars") or 12000)

    async def handler(**kwargs) -> str:
        import aiohttp

        if not url_template:
            return "[错误] http_request 缺少 url_template"
        try:
            target = url_template.format(**kwargs)
        except KeyError as e:
            return f"[错误] URL 模板缺少参数: {e}"

        timeout_sec = int(defn.get("timeout_seconds") or 60)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_sec)
            ) as session:
                async with session.request(method, target, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        return f"[错误] HTTP {resp.status}: {text[:500]}"
                    if len(text) > max_chars:
                        return text[:max_chars] + f"\n... (截断，共 {len(text)} 字符)"
                    return text
        except Exception as e:
            return f"[错误] HTTP 请求失败: {e}"

    return handler


def _build_alias_handler(builtin_name: str, registry: ToolRegistry) -> Optional[Callable]:
    tool = registry.get(builtin_name)
    if not tool:
        return None
    return tool.handler


def _build_llm_prompt_handler(defn: dict) -> Callable:
    """P1: 构建 LLM prompt 工具的 handler 闭包（Skill 可自定义 AI 工具）"""
    prompt_template = defn.get("prompt_template") or ""
    system_prompt = defn.get("system_prompt") or ""
    model_override = defn.get("model") or ""
    temperature = defn.get("temperature")
    max_tokens = defn.get("max_tokens")
    max_input_chars = int(defn.get("max_input_chars") or 20000)

    if not prompt_template:
        raise ValueError("llm_prompt 类型的 tool_definition 需要 prompt_template 字段")

    async def handler(**kwargs) -> str:
        from core.models.router import resolve_llm_config
        from tools.llm_client import LLMClient
        from core.gateway.budget import check_budget

        budget_err = check_budget()
        if budget_err:
            return f"[错误] 预算已耗尽: {budget_err}"

        user_args = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        for key, val in user_args.items():
            if isinstance(val, str) and len(val) > max_input_chars:
                user_args[key] = val[:max_input_chars] + "... (截断)"

        try:
            rendered = prompt_template.format(**user_args)
        except KeyError as e:
            return f"[错误] prompt 模板缺少参数: {e}"

        cfg = resolve_llm_config()
        if model_override:
            cfg = {**cfg, "model": model_override}
        if temperature is not None:
            cfg = {**cfg, "temperature": temperature}
        if max_tokens is not None:
            cfg = {**cfg, "max_tokens": max_tokens}

        try:
            client = LLMClient(cfg)
            result = await client.chat(rendered, system_prompt=system_prompt or None)
            return result or "(空响应)"
        except Exception as e:
            return f"[错误] LLM prompt 执行失败: {e}"

    return handler


def build_tool_definition(defn: dict, registry: ToolRegistry) -> ToolDefinition:
    name = (defn.get("name") or "").strip()
    if not name:
        raise ValueError("tool_definitions 项缺少 name")

    tool_type = (defn.get("type") or "http_request").lower()
    description = defn.get("description") or f"Skill 工具: {name}"
    parameters = defn.get("parameters")
    if not parameters or not isinstance(parameters, dict):
        parameters = _default_parameters(defn.get("param_properties"))

    if tool_type == "alias":
        alias_of = defn.get("alias_of") or defn.get("builtin")
        handler = _build_alias_handler(alias_of, registry)
        if not handler:
            raise ValueError(f"alias 目标不存在: {alias_of}")
    elif tool_type in ("http_request", "http", "http_get"):
        handler = _build_http_handler(defn)
    elif tool_type in ("llm_prompt", "llm"):          # ← P1 新增
        handler = _build_llm_prompt_handler(defn)      # ← P1 新增
    else:
        raise ValueError(f"不支持的 tool type: {tool_type}")

    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        category="skill",
        permission_mode=defn.get("permission_mode") or "allow",
        risk_level=defn.get("risk_level") or "low",
        timeout_seconds=int(defn.get("timeout_seconds") or 120),
    )


def register_skill_tools(skill_id: str, manifest: Any) -> List[str]:
    """将 Skill manifest 中的 tool_definitions 注册到 ToolRegistry（按 skill 命名空间隔离）"""
    md = _manifest_dict(manifest)
    defs = md.get("tool_definitions") or []
    if not defs:
        return []

    registry = ToolRegistry.get_instance()
    ns = skill_tool_namespace(skill_id)
    registered: List[str] = []

    for raw in defs:
        if not isinstance(raw, dict):
            continue
        try:
            tool = build_tool_definition(raw, registry)
            registry.register(tool, namespace=ns)
            registered.append(tool.name)
            logger.info("Skill %s 注册工具 %s (ns=%s)", skill_id, tool.name, ns)
        except ValueError as e:
            logger.warning("Skill %s 工具注册跳过: %s", skill_id, e)

    return registered


def unregister_skill_tools(skill_id: str) -> None:
    registry = ToolRegistry.get_instance()
    ns = skill_tool_namespace(skill_id)
    bucket = registry._namespaces.pop(ns, None)
    if bucket:
        logger.info("Skill %s 已卸载 %d 个工具 (ns=%s)", skill_id, len(bucket), ns)


async def reload_all_skill_tools() -> int:
    """启动时从 DB 恢复所有 active Skill 的自定义工具"""
    from core.skills.registry import get_skill_registry

    registry = get_skill_registry()
    skills = await registry.list(status="active")
    count = 0
    for skill in skills:
        if has_custom_tools(skill.manifest):
            unregister_skill_tools(skill.id)
            names = register_skill_tools(skill.id, skill.manifest)
            count += len(names)
    return count


def merge_manifest_tools(manifest: dict) -> dict:
    """确保 manifest.tools 与 tool_definitions 一致（对用户只暴露 Skill 层）"""
    manifest = dict(manifest or {})
    defs = manifest.get("tool_definitions") or []
    tool_names = [d.get("name") for d in defs if isinstance(d, dict) and d.get("name")]
    existing = list(manifest.get("tools") or [])

    if tool_names:
        merged = list(dict.fromkeys(tool_names + [t for t in existing if t not in tool_names]))
        manifest["tools"] = merged
    elif not existing:
        manifest["tools"] = []

    return manifest
