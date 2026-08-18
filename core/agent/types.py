"""Agent 平台 - 核心 Pydantic 类型定义

定义 Agent 的 6 层完整结构、消息类型、编排配置等。
"""

from __future__ import annotations
from typing import Optional, List, Dict, Any, Literal, ClassVar
from datetime import datetime

from pydantic import BaseModel, Field

from core.models.types import AgentModelProfile, ModelBinding, PLATFORM_DEFAULT


# ============================================================
# Agent 6 层定义
# ============================================================

class MemoryConfig(BaseModel):
    """Agent 记忆配置"""
    episodic: dict = Field(default_factory=lambda: {"enabled": True, "max_entries": 100})
    semantic: dict = Field(default_factory=lambda: {"enabled": True, "topics": []})
    procedural: dict = Field(default_factory=lambda: {"enabled": True})


class AgentTool(BaseModel):
    """Agent 工具定义"""
    name: str
    description: str = ""
    endpoint: str = ""  # 工具调用端点


class InteractionConfig(BaseModel):
    """Agent 交互配置"""
    style: str = "collaborative"  # collaborative | authoritative | creative | managerial
    transparency: str = "high"    # high | medium | low
    protocol: str = "conversation_bus"


class AgentRuntimeInfo(BaseModel):
    """Agent 运行时部署信息（P3: RemoteAgent 扩展）"""
    deployment: Literal["local", "remote"] = "local"
    endpoint: str = ""              # remote: HTTP execute URL
    health_endpoint: str = ""       # remote: 健康检查 URL（默认同 endpoint/health）
    timeout_seconds: int = Field(default=120, ge=5, le=600)
    auth_header: str = ""           # 可选 Bearer token（仅存 DB，不回显 API）


class PerformanceMetrics(BaseModel):
    """Agent 性能指标（可扩展，应用层可添加领域特定指标）"""
    base: Dict[str, float] = Field(default_factory=lambda: {
        "accuracy": 0.5,
        "speed": 0.5,
        "user_rating": 3.0,
    })
    # 应用层通过 DomainAdapter 添加领域特定指标
    domain: Dict[str, float] = Field(default_factory=dict)


class EvolutionEvent(BaseModel):
    """Agent 进化事件"""
    version: str
    changes: str  # 变更描述
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class AgentDefinition(BaseModel):
    """Agent 完整定义（6层 + Platform-Core 契约扩展）"""
    # 身份层
    id: str
    name: str
    type: Literal["domain", "custom", "template", "remote"] = "custom"
    status: Literal["active", "inactive", "training", "archived"] = "active"
    emoji: str = "🤖"
    role: str = ""

    # 能力层
    capabilities: List[str] = Field(default_factory=list)
    version: str = "1.0.0"
    creator: str = "user"
    model: str = "deepseek-v4-flash"
    specialized_knowledge: List[str] = Field(default_factory=list)
    reasoning_engine: str = "chain-of-thought"  # @deprecated: 声明但运行时不消费，保留向后兼容
    temperature: Optional[float] = Field(default=0.7, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=4096, ge=1, le=32768)
    # 多模态模型配置（与 tool_ids / skill_ids 并列；model/temperature/max_tokens 为兼容旧 API）
    model_profile: AgentModelProfile = Field(default_factory=AgentModelProfile)

    # 契约层（Platform-Core v2）
    input_schema: Optional[dict] = None     # JSON Schema: 该 Agent 期望的输入结构
    output_schema: Optional[dict] = None    # JSON Schema: 该 Agent 承诺的输出结构
    tool_namespaces: List[str] = Field(default_factory=list)  # 需要访问的工具命名空间

    # 记忆层
    memory_config: MemoryConfig = Field(default_factory=MemoryConfig)

    # 工具层 — tool_ids 与 skill_ids 平级挂载
    tools: List[AgentTool] = Field(
        default_factory=list,
        deprecated=True,
        description="Legacy. 请用 tool_ids 或 skill_ids 代替。将在 v5.0 移除。",
    )
    tool_ids: List[str] = Field(default_factory=list)    # ToolCatalog 直接挂载
    skill_ids: List[str] = Field(default_factory=list)   # 指令/知识技能包

    # P2: API 序列化时排除的字段（减少响应体积）
    _API_EXCLUDE_FIELDS: ClassVar[frozenset] = frozenset({
        "reasoning_engine",
        "performance_metrics",
        "specialized_knowledge",
    })

    # 交互层
    interaction_config: InteractionConfig = Field(default_factory=InteractionConfig)
    runtime_info: AgentRuntimeInfo = Field(default_factory=AgentRuntimeInfo)
    system_prompt: str = ""
    peers: List[str] = Field(default_factory=list)  # 协作白名单

    # 进化数据
    evolution_history: List[EvolutionEvent] = Field(default_factory=list)  # @deprecated: 运行时不消费，仅 API 展示
    performance_metrics: PerformanceMetrics = Field(default_factory=PerformanceMetrics)  # @deprecated: 运行时不更新

    # 扩展字段（策略提示等，SelfOptimizer 等模块使用）
    extra: Dict[str, Any] = Field(default_factory=dict, description="扩展字段（策略提示等）")

    # 元数据
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ============================================================
# 消息类型定义
# ============================================================

class MessageType:
    """7 种核心消息类型 + 扩展"""
    TASK = "task"
    HANDOFF = "handoff"
    REVIEW_REQUEST = "review_request"
    QUESTION = "question"
    ANSWER = "answer"
    ESCALATE = "escalate"
    DECIDE = "decide"
    STATUS = "status"
    THINKING = "thinking"
    ALERT = "alert"
    CHAT = "chat"


class ConversationMessage(BaseModel):
    """对话消息"""
    id: Optional[int] = None
    project_id: str
    phase: str = ""
    sender_type: Literal["agent", "user", "system"] = "system"
    sender_id: str = ""
    receiver_id: str = ""
    message_type: str = MessageType.CHAT
    content: str
    parent_id: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    consensus_reached: bool = False
    created_at: Optional[str] = None


# ============================================================
# 决策卡
# ============================================================

class DecisionOption(BaseModel):
    """决策选项"""
    id: str
    label: str
    agent_id: str = ""
    agent_view: str = ""
    confidence: float = 0.5


class DecisionCard(BaseModel):
    """决策卡（Agent 分歧时升级给用户）"""
    id: Optional[int] = None
    project_id: str
    conversation_id: Optional[int] = None
    phase: str = ""
    question: str
    options: List[DecisionOption] = Field(default_factory=list)
    escalated_by: str = ""
    chosen_option: Optional[str] = None
    decided_by: Optional[Literal["agent", "user"]] = None
    resolved_at: Optional[str] = None
    created_at: Optional[str] = None


# ============================================================
# 项目
# ============================================================

class ProjectConfig(BaseModel):
    """项目配置（纯通用：不含任何业务字段，业务配置统一移入 extra）"""
    task_description: str = ""       # 任务描述（通用，替代 idea）
    domain_id: str = ""              # 领域ID（如 platform:web_novel），规划时自动识别
    review_mode: bool = False        # 是否启用审核模式
    temperature: Optional[float] = None
    extra: Dict[str, Any] = Field(default_factory=dict)  # 业务字段全放这里

    def get(self, key: str, default: Any = None) -> Any:
        """从 extra 读取业务配置"""
        return self.extra.get(key, default)

    def model_dump(self, **kwargs) -> Dict[str, Any]:
        """展开 extra 到顶层（向后兼容旧 API）"""
        data = super().model_dump(**kwargs)
        extra = data.pop("extra", {})
        data.update(extra)
        return data


class ProjectAgent(BaseModel):
    """项目中的 Agent"""
    id: Optional[int] = None
    project_id: str = ""
    agent_id: str
    join_order: int = 0
    role: str = ""
    status: str = "active"
    competition_group: str = ""


class Project(BaseModel):
    """项目（通用类型，不预设具体领域）"""
    id: str
    name: str
    type: str = "custom"
    status: Literal["idle", "created", "queued", "running", "paused", "completed", "partial", "failed", "archived"] = "idle"
    emoji: str = "📦"
    mode: Literal["sequential", "parallel", "roundtable", "adaptive", "dag", "auto"] = "auto"
    config: ProjectConfig = Field(default_factory=ProjectConfig)
    template_id: Optional[str] = None
    agents: List[ProjectAgent] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ============================================================
# 模板
# ============================================================

class PresetTemplate(BaseModel):
    """预设模板（Agent 组合方案，Platform-Core: project_type → tags 自由标签）"""
    id: str
    name: str
    emoji: str = "📋"
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    agent_ids: List[str] = Field(default_factory=list)
    workflow_config: Dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    created_at: Optional[str] = None


# ============================================================
# 编排配置
# ============================================================

class OrchestrationConfig(BaseModel):
    """编排运行参数"""
    pattern: Literal["sequential", "roundtable", "parallel"] = "sequential"
    review_mode: bool = False
    enable_parallel: bool = False
    enable_consensus: bool = False
    enable_evolution: bool = True
    trace_enabled: bool = True


# ============================================================
# 系统设置
# ============================================================

class SystemSettings(BaseModel):
    """系统全局设置（Platform-Core: 仅 LLM 平台级配置，工具模型配置在 config.py）"""
    llm_model: str = "deepseek-v4-flash"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_temperature: float = 0.7
    llm_max_token: int = 4096
    cache_enabled: bool = True
