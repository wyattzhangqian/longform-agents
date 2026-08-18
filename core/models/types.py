"""模型层类型 — Agent 绑定与平台 ModelSpec"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

Modality = Literal["llm", "image", "video", "judge", "music"]
PLATFORM_DEFAULT = "platform_default"


class ModelBinding(BaseModel):
    """某 modality 的模型绑定（Agent 级）"""

    model_id: str = PLATFORM_DEFAULT
    provider: str = ""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class AgentModelProfile(BaseModel):
    """Agent 多模态模型配置 — 与 Tool / Skill 并列挂载"""

    llm: ModelBinding = Field(default_factory=ModelBinding)
    image: Optional[ModelBinding] = None
    video: Optional[ModelBinding] = None
    music: Optional[ModelBinding] = None
    judge: Optional[ModelBinding] = None

    @classmethod
    def from_legacy(
        cls,
        model: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> "AgentModelProfile":
        """从旧 agents.model / temperature / max_tokens 字段构造"""
        llm = ModelBinding(
            model_id=model or PLATFORM_DEFAULT,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return cls(llm=llm)

    def sync_legacy_fields(self) -> dict:
        """写回旧列：model / temperature / max_tokens（兼容未迁移客户端）"""
        mid = self.llm.model_id
        return {
            "model": mid if mid != PLATFORM_DEFAULT else "",
            "temperature": self.llm.temperature,
            "max_tokens": self.llm.max_tokens,
        }


class ModelSpec(BaseModel):
    """平台注册表中的一条模型规格"""

    id: str
    modality: Modality
    provider: str
    label: str = ""
    description: str = ""
    base_url: str = ""
    default_temperature: float = 0.7
    default_max_tokens: int = 4096
    api_key_env: str = ""  # 环境变量名，运行时解析
    fallback: str = ""  # 降级模型 ID（空=不降级）
