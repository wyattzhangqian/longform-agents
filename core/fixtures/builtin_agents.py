"""内置通用 Agent 定义（平台兜底，与领域无关）。

原 core.adapters.generic.GenericDomainAdapter 提供，插件注册体系废弃后
（2026-07-31 架构统一）迁移至 fixtures——它们本质就是平台种子数据。
"""

from __future__ import annotations

from typing import Any, Dict, List

BUILTIN_GENERIC_AGENTS: List[Dict[str, Any]] = [
    {
        "id": "fixture_agent_researcher",
        "name": "调研员 Alex",
        "type": "custom",
        "role": "调研员",
        "emoji": "🔍",
        "capabilities": ["调研", "信息检索", "分析"],
        "system_prompt": "你是专业调研员。给定任务主题，做结构化的深度调研：先厘清背景，再找核心事实，最后给出有依据的分析结论。回复用要点格式，每个要点标注信息置信度。",
        "model": "deepseek-v4-flash",
        "temperature": 0.4,
        "max_tokens": 16384,
        # thinking:disabled（2026-08-12）：长文/分析任务若开思维链，思维链与正文共享
        # max_tokens，预算被吃光 → 产出空/截断。通用兜底 Agent 一律关思考保产出。
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.4,
                "max_tokens": 16384,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["file_write"],
        "tool_namespaces": ["platform"],
    },
    {
        "id": "fixture_agent_writer",
        "name": "撰稿人 Blake",
        "type": "custom",
        "role": "撰稿人",
        "emoji": "✍️",
        "capabilities": ["写作", "文档", "表达"],
        "system_prompt": "你是技术撰稿人。根据调研素材，产出结构清晰、论证严谨的文档。要求：先给大纲（三级标题），再逐节展开，每节有明确的小结句。",
        "model": "deepseek-v4-flash",
        "temperature": 0.6,
        "max_tokens": 16384,
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.6,
                "max_tokens": 16384,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["file_write"],
        "tool_namespaces": ["platform"],
    },
    {
        "id": "fixture_agent_reviewer",
        "name": "审阅者 Casey",
        "type": "custom",
        "role": "审阅者",
        "emoji": "👁️",
        "capabilities": ["审阅", "校验", "反馈"],
        "system_prompt": "你是严谨的审阅者。对提交的文档，从准确性、完整性、逻辑性、可读性四个维度评分（每项 0-25），指出具体修改建议。如果总分 < 70，明确说'需要重写'并列出最关键的 3 个问题。",
        "model": "deepseek-v4-flash",
        "temperature": 0.2,
        "max_tokens": 16384,
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.2,
                "max_tokens": 16384,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["file_write"],
        "tool_namespaces": ["platform"],
    },
    {
        "id": "fixture_agent_general",
        "name": "通用助手 Dana",
        "type": "custom",
        "role": "通用助手",
        "emoji": "🤖",
        "capabilities": ["问答", "辅助"],
        "system_prompt": "你是通用 AI 助手，回答用户的问题，提供有用的信息和建议。",
        "model": "deepseek-v4-flash",
        "temperature": 0.5,
        "max_tokens": 16384,
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.5,
                "max_tokens": 16384,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["file_write"],
        "tool_namespaces": ["platform"],
    },
]
