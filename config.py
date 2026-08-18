"""Agent 平台 — 通用核心层配置

仅为 core/ 提供运行时配置。业务相关模型（图片/视频等）由应用层通过 DomainAdapter 提供。
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import logging
import os
from pathlib import Path
from typing import Dict, Optional

_logger = logging.getLogger(__name__)

# ============================================================
# 平台版本号（单一来源）
# ============================================================

VERSION = "0.1.0"

# ============================================================
# 路径常量
# ============================================================

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)
# DATABASE_PATH 可覆盖（测试隔离 / 容器挂载）
DB_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "agent_platform.db")))

# ============================================================
# 平台基础模型 — LLM（所有 Agent 共享）
# 通过 LLM_MODELS_CONFIG 环境变量配置（JSON），不设置则使用默认值
# 格式: {"model-id": {"provider":"...","label":"...","description":"...","base_url":"..."}}
# ============================================================

def _parse_models_config(env_name: str, defaults: dict) -> dict:
    """从环境变量读取模型配置（JSON），未设置则使用默认值"""
    raw = os.getenv(env_name, "").strip()
    if raw:
        import json
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            import warnings
            warnings.warn(f"{env_name} JSON 格式错误，回退默认值: {e}")
    return defaults

_LLM_DEFAULTS: Dict[str, Dict[str, str]] = {
    "deepseek-v4-flash": {
        "provider": "deepseek",
        "label": "DeepSeek V4 Flash",
        "description": "快速通用对话，适合大多数 Agent 任务",
        "base_url": "https://api.deepseek.com",
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "label": "DeepSeek V4 Pro",
        "description": "强推理与复杂分析，适合 Judge / 规划",
        "base_url": "https://api.deepseek.com",
        "fallback": "deepseek-v4-flash",  # pro 不可用时降级到 flash
    },
}

AVAILABLE_LLM_MODELS = _parse_models_config("LLM_MODELS_CONFIG", _LLM_DEFAULTS)

# 旧别名（2026-07-24 退役）→ 官方 V4 model id
LEGACY_LLM_MODEL_ALIASES: Dict[str, str] = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}

DEFAULT_LLM_MODEL = os.getenv("LLM_MODEL", next(iter(AVAILABLE_LLM_MODELS)) if AVAILABLE_LLM_MODELS else "deepseek-v4-flash")
DEFAULT_LLM_JUDGE_MODEL = os.getenv("LLM_JUDGE_MODEL",
    list(AVAILABLE_LLM_MODELS.keys())[1] if len(AVAILABLE_LLM_MODELS) > 1 else DEFAULT_LLM_MODEL)

# 推理模型（有思维链输出）——用于规划、领域识别、评审等需要深度思考的场景
REASONING_MODEL = os.getenv("REASONING_MODEL", "deepseek-v4-pro")
# 哪些场景默认使用推理模型
REASONING_ENABLED_SCOPES = {"planner", "judge"}


def get_model_for_scope(scope: str) -> str:
    """根据使用场景返回推荐模型——推理场景用推理模型，其他用基座模型"""
    if scope in REASONING_ENABLED_SCOPES:
        return REASONING_MODEL
    return DEFAULT_LLM_MODEL

# ============================================================
# 多模态生成模型 catalog（生图 / 生视频）
# 通过 IMAGE_MODELS_CONFIG / VIDEO_MODELS_CONFIG 环境变量配置（JSON）
# 格式: {"model-id": {"provider":"...","label":"...","base_url":"...","api_key_env":"..."}}
# ============================================================

VOLCENGINE_API_KEY = os.getenv("VOLCENGINE_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

_IMAGE_DEFAULTS: Dict[str, Dict[str, str]] = {
    "doubao-seedream-4-5-251128": {
        "provider": "volcengine",
        "label": "Doubao Seedream 4.5",
        "description": "火山引擎生图模型，适合分镜/角色参考图",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key_env": "VOLCENGINE_API_KEY",
    },
    "dall-e-3": {
        "provider": "openai",
        "label": "DALL-E 3",
        "description": "OpenAI 兼容生图（需配置 OPENAI_API_KEY）",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
}

AVAILABLE_IMAGE_MODELS = _parse_models_config("IMAGE_MODELS_CONFIG", _IMAGE_DEFAULTS)

_VIDEO_DEFAULTS: Dict[str, Dict[str, str]] = {
    "doubao-seedance-1-5-pro-251215": {
        "provider": "volcengine",
        "label": "Doubao Seedance 1.5 Pro",
        "description": "火山引擎图生视频 / 文生视频",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key_env": "VOLCENGINE_API_KEY",
    },
}

AVAILABLE_VIDEO_MODELS = _parse_models_config("VIDEO_MODELS_CONFIG", _VIDEO_DEFAULTS)

_MUSIC_DEFAULTS: Dict[str, Dict[str, str]] = {
    "suno-v4": {
        "provider": "apibox",
        "label": "Suno V4 (API Box)",
        "description": "AI 歌曲生成（人声+伴奏），国内直连，微信支付。注册: api.box/api-key",
        "base_url": "https://apibox.erweima.ai",
        "api_key_env": "SUNO_API_KEY",
    },
}

AVAILABLE_MUSIC_MODELS = _parse_models_config("MUSIC_MODELS_CONFIG", _MUSIC_DEFAULTS)

DEFAULT_IMAGE_MODEL = os.getenv("IMAGE_MODEL", next(iter(AVAILABLE_IMAGE_MODELS)) if AVAILABLE_IMAGE_MODELS else "")
DEFAULT_VIDEO_MODEL = os.getenv("VIDEO_MODEL", next(iter(AVAILABLE_VIDEO_MODELS)) if AVAILABLE_VIDEO_MODELS else "")
DEFAULT_MUSIC_MODEL = os.getenv("MUSIC_MODEL", next(iter(AVAILABLE_MUSIC_MODELS)) if AVAILABLE_MUSIC_MODELS else "")
SUNO_API_KEY = os.getenv("SUNO_API_KEY", "") or os.getenv("MUSIC_API_KEY", "")

# ── API Key 引用映射 — Agent 引用名称 → 实际 Key ──
# Agent model_profile.extra.api_key_ref 存名称（如 "VOLCENGINE"），不存明文。
# 平台管理员在 .env 中统一管理 Key。延迟求值以兼容定义顺序。


# 模块级 Key 缓存，由 reload_model_defaults() 刷新
_db_key_cache: Dict[str, str] = {}


# 凭证引用名白名单：只允许显式登记的引用名读取对应环境变量/DB Key。
# 未登记一律拒绝——禁止恶意 agent 配置通过 api_key_ref 读取服务器任意 {REF}_API_KEY
# 环境变量（2026-08-18 安全修复，配合 router.py 的 custom_base_url 校验堵死密钥外泄链）。
_API_KEY_REF_WHITELIST = {
    "VOLCENGINE": "VOLCENGINE_API_KEY",
    "OPENAI": "OPENAI_API_KEY",
    "SUNO": "SUNO_API_KEY",
    "DEEPSEEK": "DEEPSEEK_API_KEY",
}


def get_api_key_for_ref(ref: str) -> str:
    """根据引用名返回 API Key。仅允许白名单内的引用名（env var > DB system_settings）。"""
    env_name = _API_KEY_REF_WHITELIST.get(ref.upper())
    if env_name is None:
        _logger.warning("api_key_ref 白名单外引用被拒绝: %s", ref)
        return ""
    env_val = os.getenv(env_name, "")
    if env_val:
        return env_val

    # DB 兜底（由 Settings UI 写入）
    return _db_key_cache.get(ref.upper(), "")


async def get_api_key_for_ref_async(ref: str) -> str:
    """#4 统一凭证库：优先查 credentials 表，fallback get_api_key_for_ref

    调用方在 async 上下文中应使用此方法替代同步版。
    """
    # 1. credentials 表
    try:
        from core.storage.repositories.credential_repo import CredentialRepository
        repo = CredentialRepository()
        cred = await repo.get_by_provider(ref.upper())
        if cred:
            key = await repo.get_decrypted_key(cred["id"])
            if key:
                return key
    except Exception:
        pass
    # 2. env + DB system_settings
    return get_api_key_for_ref(ref)


def normalize_llm_model(model: Optional[str]) -> str:
    """解析并归一化 LLM model id（兼容旧别名）。

    健壮性（2026-08-08）：模型不在可用列表且非别名时 fallback 到默认模型，
    否则 Agent 配了不存在的模型（如 claude-sonnet-4-6）会让 LLM 调用 400、
    直接导致 phase/run 失败。
    """
    if not model:
        return DEFAULT_LLM_MODEL
    if model in AVAILABLE_LLM_MODELS:
        return model
    if model in LEGACY_LLM_MODEL_ALIASES:
        return LEGACY_LLM_MODEL_ALIASES[model]
    if model != DEFAULT_LLM_MODEL:
        import logging
        logging.getLogger("config").warning(
            "未知 LLM 模型 '%s'，fallback 到默认 %s", model, DEFAULT_LLM_MODEL,
        )
    return DEFAULT_LLM_MODEL

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_ANTHROPIC_BASE_URL = os.getenv("DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

LLM_CONFIG = {
    "provider": os.getenv("LLM_PROVIDER", "deepseek"),
    "model": os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
    "api_key": DEEPSEEK_API_KEY,
    "base_url": DEEPSEEK_BASE_URL,
    "temperature": float(os.getenv("LLM_TEMPERATURE", "0.7")),
    "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "16384")),
}

# LLM-as-Judge 专用配置
LLM_JUDGE_MODEL = os.getenv("LLM_JUDGE_MODEL", DEFAULT_LLM_JUDGE_MODEL)
LLM_JUDGE_TEMPERATURE = float(os.getenv("LLM_JUDGE_TEMPERATURE", "0.3"))

# ============================================================
# LLM 容错（P0 — 超时 / 重试 / 熔断）
# ============================================================

LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.0"))
LLM_CIRCUIT_THRESHOLD = int(os.getenv("LLM_CIRCUIT_THRESHOLD", "5"))   # 连续失败 N 次熔断
LLM_CIRCUIT_COOLDOWN = float(os.getenv("LLM_CIRCUIT_COOLDOWN", "60"))  # 熔断冷却秒数

# 协作成本预算（每项目运行；0 = 不限制）
RUN_TOKEN_BUDGET = int(os.getenv("RUN_TOKEN_BUDGET", "0"))          # 单次运行最大 token 数
RUN_LLM_CALL_BUDGET = int(os.getenv("RUN_LLM_CALL_BUDGET", "0"))    # 单次运行最大 LLM 调用次数

# ============================================================
# 认证配置
# ============================================================

API_KEY = os.getenv("API_KEY", "")
API_KEY_ENABLED = os.getenv("API_KEY_ENABLED", "false").lower() == "true"

# 用户体系（JWT 登录）。本地默认关闭；生产/docker-compose 通过 .env 设 AUTH_ENABLED=true
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() == "true"
AUTH_SECRET = os.getenv("AUTH_SECRET", "")  # 为空时启动自动生成并持久化到 data/.auth_secret
AUTH_TOKEN_TTL_HOURS = int(os.getenv("AUTH_TOKEN_TTL_HOURS", "24"))
AUTH_ADMIN_USERNAME = os.getenv("AUTH_ADMIN_USERNAME", "admin")
AUTH_ADMIN_PASSWORD = os.getenv("AUTH_ADMIN_PASSWORD", "")  # 首次启动 bootstrap 管理员

# CORS：逗号分隔的允许来源；"*" 仅开发环境（此时强制关闭 credentials）
CORS_ORIGINS = [
    o.strip() for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,https://ai-comic-workflow-d9db094a82bf37-1314141361.tcloudbaseapp.com",
    ).split(",") if o.strip()
]

# ============================================================
# PostgreSQL 配置
# ============================================================

POSTGRES_ENABLED = os.getenv("POSTGRES_ENABLED", "false").lower() == "true"

POSTGRES_CONFIG = {
    "enabled": POSTGRES_ENABLED,
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
    "database": os.getenv("POSTGRES_DATABASE", "agent_platform"),
    "min_size": int(os.getenv("POSTGRES_POOL_MIN", "2")),
    "max_size": int(os.getenv("POSTGRES_POOL_MAX", "10")),
    "command_timeout": int(os.getenv("POSTGRES_TIMEOUT", "30")),
}

# ============================================================
# Redis + Celery 配置（异步任务基础设施）
# ============================================================

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

if REDIS_PASSWORD:
    REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
else:
    REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

CELERY_ENABLED = os.getenv("CELERY_ENABLED", "false").lower() == "true"

CELERY_CONFIG = {
    "enabled": CELERY_ENABLED,
    "soft_time_limit": int(os.getenv("CELERY_SOFT_TIMEOUT", "1800")),
    "time_limit": int(os.getenv("CELERY_TIME_LIMIT", "3600")),
    "result_expires": int(os.getenv("CELERY_RESULT_EXPIRES", "86400")),
}

# ============================================================
# SSE 跨实例（Redis Pub/Sub，P3）
# ============================================================

SSE_REDIS_ENABLED = os.getenv("SSE_REDIS_ENABLED", "false").lower() == "true"
INSTANCE_ID = os.getenv("INSTANCE_ID", "")

# ============================================================
# 可观测性 & 项目鉴权
# ============================================================

PROMETHEUS_ENABLED = os.getenv("PROMETHEUS_ENABLED", "true").lower() == "true"
PROJECT_ACCESS_ENABLED = os.getenv("PROJECT_ACCESS_ENABLED", "false").lower() == "true"

# ============================================================
# Web 搜索（web_search 工具）
# ============================================================

WEB_SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "auto")  # auto | tavily | serpapi | duckduckgo
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")

# ============================================================
# Agent 元数据选项 — 供 /api/config 端点暴露给前端
# ============================================================

AGENT_OPTIONS = {
    "agent_types": [
        {"value": "custom", "label": "自定义", "description": "用户手动创建的独立 Agent"},
        {"value": "domain", "label": "领域专家", "description": "规划器按任务领域组建的专家 Agent"},
        {"value": "template", "label": "模板实例", "description": "从 Agent 蓝图实例化的 Agent"},
        {"value": "remote", "label": "远程 Agent", "description": "通过 HTTP endpoint 执行的外部 Agent"},
    ],
    "statuses": [
        {"value": "active", "label": "运行中"},
        {"value": "inactive", "label": "已停用"},
        {"value": "training", "label": "训练中"},
    ],
    "reasoning_engines": [
        {"value": "chain-of-thought", "label": "Chain-of-Thought", "description": "逐步推理，适合复杂分析"},
        {"value": "react", "label": "ReAct", "description": "推理+行动循环，适合工具调用"},
        {"value": "langgraph", "label": "LangGraph", "description": "图结构编排，适合多步骤工作流"},
        {"value": "custom", "label": "自定义", "description": "用户自定义推理策略"},
    ],
    "interaction_styles": [
        {"value": "collaborative", "label": "协作型", "description": "与用户和其他 Agent 紧密配合"},
        {"value": "authoritative", "label": "权威型", "description": "以明确判断指导决策"},
        {"value": "creative", "label": "创意型", "description": "鼓励发散思维和创意输出"},
        {"value": "managerial", "label": "管理型", "description": "协调任务分配和执行"},
    ],
    "transparency_levels": [
        {"value": "high", "label": "完全公开", "description": "所有推理过程对外可见"},
        {"value": "medium", "label": "关键决策", "description": "仅关键决策节点透明"},
        {"value": "low", "label": "黑盒模式", "description": "仅输出结果，隐藏内部过程"},
    ],
    "memory_types": [
        {
            "key": "episodic",
            "label": "情景记忆",
            "description": "记录对话历史和上下文，支持按轮次回顾",
            "extra_field": {"name": "max_entries", "label": "最大记忆条数", "type": "number", "min": 10, "max": 1000, "default": 100},
        },
        {
            "key": "semantic",
            "label": "语义记忆",
            "description": "提取并组织领域知识主题，形成知识图谱",
            "extra_field": {"name": "topics", "label": "关注主题", "type": "chip_list", "default": []},
        },
        {
            "key": "procedural",
            "label": "程序记忆",
            "description": "记录任务执行路径和行为模式，优化后续决策",
            "extra_field": None,
        },
    ],
    "temperature_range": {"min": 0, "max": 2, "step": 0.1, "default": 0.7},
    "max_tokens_range": {"min": 256, "max": 32768, "step": 256, "default": 4096},
}

SERVER_HOST = os.getenv("HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", "8110"))


def auth_disabled_warning() -> Optional[str]:
    """两套鉴权开关全关时返回告警文案；否则返回 None。

    默认部署 AUTH_ENABLED / API_KEY_ENABLED 均为 false，此时 ApiKeyMiddleware
    直接放行所有请求（见 core/auth.py），任何能访问端口的人都可调用 /api/run、
    读取产物。对外发布前必须开启其一。启动时由 main.py 打印此告警。
    """
    if AUTH_ENABLED or API_KEY_ENABLED:
        return None
    return (
        "安全告警：AUTH_ENABLED 与 API_KEY_ENABLED 均为 false，服务当前【无鉴权】"
        "暴露所有 API（含 /api/run 与产物下载）。对外发布前请在 .env 设置 "
        "AUTH_ENABLED=true（并配置 AUTH_ADMIN_PASSWORD）或 API_KEY_ENABLED=true（并配置 API_KEY）。"
    )
