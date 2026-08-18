"""平台内置通用 Agent 种子定义

启动时幂等注册到 agents 表，确保全新部署在 auto_plan 模式下不选到空 Agent。

4 个内置 Agent：
  - builtin_researcher  调研员（信息收集、数据分析）
  - builtin_writer      撰稿人（内容撰写、文案创作）
  - builtin_reviewer    审核员（质量审核、逻辑校验）
  - builtin_general     通用助手（灵活执行各类任务）

幂等：使用 AgentRegistry.register() + INSERT OR IGNORE，重复启动不重复插入。

字段对齐 agents 表 schema（db/schema.sql），与 GenericDomainAdapter 内置定义一致。
"""

from __future__ import annotations
from typing import List, Dict, Any

# ══════════════════════════════════════════════════════════════
# 内置 Agent 定义
# ══════════════════════════════════════════════════════════════

BUILTIN_AGENTS: List[Dict[str, Any]] = [
    {
        "id": "builtin_researcher",
        "name": "研究员",
        "description": "负责信息收集、资料检索、数据分析，输出结构化调研结果",
        "type": "custom",
        "status": "active",
        "emoji": "🔍",
        "role": "researcher",
        "capabilities": ["research", "web_search", "analysis", "data_collection"],
        "system_prompt": (
            "你是专业研究员，擅长信息收集和深度分析。"
            "产出应包含事实、数据来源和分析结论，有据可依。"
        ),
        "model": "deepseek-v4-flash",
        # max_tokens 16384 + thinking:disabled（2026-08-12）：reasoning 模型 4096 会被思维链
        # 吃光 → 产出为空；关闭 thinking 让预算全给正文，长输出稳定。
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.3,
                "max_tokens": 16384,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["web_search", "file_read", "file_write"],
        "skill_ids": [],
        "temperature": 0.3,
        "max_tokens": 16384,
    },
    {
        "id": "builtin_writer",
        "name": "撰稿人",
        "description": "负责内容撰写、文案创作、报告生成，输出逻辑清晰、表达流畅的内容",
        "type": "custom",
        "status": "active",
        "emoji": "✍️",
        "role": "writer",
        "capabilities": ["writing", "content_creation", "summarization", "documentation"],
        "system_prompt": (
            "你是专业撰稿人，擅长结构化写作和创意表达。"
            "输出内容应逻辑清晰、表达流畅、可读性强。"
            "【强制要求】完成正文写作后，必须调用 file_write 工具将完整正文写入指定文件路径"
            "（文件路径见协作协议/任务中的产出文件清单，不得只返回文本而不落盘）。"
        ),
        "model": "deepseek-v4-flash",
        # max_tokens 32000 + thinking:disabled（2026-08-09）：写作 agent 若开思维链，
        # 思维链与正文共享 max_tokens，预算被吃光 → 正文空/短（实测）。关闭 thinking
        # 让整个预算全给正文（≈24000 汉字），章节长且稳定。
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.7,
                "max_tokens": 32000,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["file_read", "file_write"],
        "skill_ids": [],
        "temperature": 0.7,
        "max_tokens": 32000,
    },
    {
        "id": "builtin_reviewer",
        "name": "审核员",
        "description": "负责质量审核、逻辑校验、风格一致性检查，输出结构化评审意见",
        "type": "custom",
        "status": "active",
        "emoji": "📝",
        "role": "reviewer",
        "capabilities": ["review", "quality_check", "editing", "optimization"],
        "system_prompt": (
            "你是严格的审核员。你的职责是发现问题、提出改进建议。"
            "给出结构化评审意见：问题列表、严重等级、修改建议。"
            "要具体指出问题位置和修复方式，不泛泛而谈。"
        ),
        "model": "deepseek-v4-flash",
        # max_tokens 32000 + thinking:disabled（2026-08-12）：审核 agent 要读整篇章节后综合
        # 评审，预算不足会被思维链吃光 → 审核意见 0 输出 → 强制检查标 FAILED（run partial）。
        # 关闭 thinking 让预算全给评审意见；file_write 落盘评审结果。
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.2,
                "max_tokens": 32000,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["file_read", "file_write"],
        "skill_ids": [],
        "temperature": 0.2,
        "max_tokens": 32000,
    },
    {
        "id": "builtin_general",
        "name": "通用助手",
        "description": "通用任务执行者，适合不确定归属的任务，灵活调整策略",
        "type": "custom",
        "status": "active",
        "emoji": "🤖",
        "role": "assistant",
        "capabilities": ["general", "planning", "execution", "coordination"],
        "system_prompt": (
            "你是全能 AI 助手，能处理各类任务。"
            "收到任务后根据性质灵活调整策略。"
            "输出简洁、准确、可执行。"
        ),
        "model": "deepseek-v4-flash",
        # max_tokens 16384 + thinking:disabled（2026-08-12）：通用兜底 agent 处理任意任务，
        # 4096 会被 reasoning 思维链吃光 → 长输出截断/空产出；关 thinking 保产出。
        "model_profile": {
            "llm": {
                "model_id": "deepseek-v4-flash",
                "temperature": 0.5,
                "max_tokens": 16384,
                "extra": {"thinking": "disabled"},
            },
            "image": None, "video": None, "music": None, "judge": None,
        },
        "tool_ids": ["web_search", "file_read", "file_write"],
        "skill_ids": [],
        "temperature": 0.5,
        "max_tokens": 16384,
    },
]


# ══════════════════════════════════════════════════════════════
# 幂等注册
# ══════════════════════════════════════════════════════════════

async def ensure_builtin_agents(db=None) -> int:
    """启动时确保内置 Agent 存在（幂等：已存在则跳过）。

    通过 AgentRegistry.register() 注册 —— 与 raw INSERT 不同，
    走完整 AgentDefinition 序列化管道（model_profile → AgentModelProfile 等）。

    返回实际新增的 Agent 数量（首次启动 = 4，后续启动 = 0）。
    """
    from core.agent.registry import get_registry
    from core.logging import get_logger

    _logger = get_logger("seed_agents")
    registry = get_registry()

    registered = 0
    for agent_def in BUILTIN_AGENTS:
        # 幂等检查：已存在则跳过
        try:
            existing = await registry.get(agent_def["id"])
            if existing is not None:
                continue
        except Exception:
            pass

        try:
            await registry.register(agent_def)
            registered += 1
            _logger.info(
                "内置 Agent 注册: id=%s name=%s role=%s",
                agent_def["id"],
                agent_def["name"],
                agent_def.get("role", ""),
            )
        except Exception as e:
            _logger.warning(
                "内置 Agent 注册跳过 id=%s: %s", agent_def["id"], e,
            )

    if registered:
        _logger.info(
            "内置 Agent 种子完成: %d 新增 / %d 总计",
            registered, len(BUILTIN_AGENTS),
        )
    else:
        _logger.info(
            "内置 Agent 已全部就绪: %d 个（无新增）",
            len(BUILTIN_AGENTS),
        )

    return registered


def get_builtin_agent_ids() -> List[str]:
    """返回内置 Agent 的 id 列表（供 planner 等模块使用白名单过滤）"""
    return [a["id"] for a in BUILTIN_AGENTS]
