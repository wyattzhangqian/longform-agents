"""跨 Run 记忆数据模型

MemoryType: 4 种记忆类型
MemoryEntry: 单条跨 run 记忆
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """跨 run 记忆类型"""
    USER_PREFERENCE = "user_preference"       # 用户偏好（如风格、格式）
    QUALITY_PATTERN = "quality_pattern"       # 质量模式（如常见违规修复）
    DOMAIN_KNOWLEDGE = "domain_knowledge"     # 领域知识
    WORKFLOW_INSIGHT = "workflow_insight"     # 工作流洞察


class MemoryEntry(BaseModel):
    """单条跨 run 记忆"""
    id: str
    agent_id: str
    memory_type: MemoryType = MemoryType.USER_PREFERENCE
    content: str = ""                          # 提取的记忆内容（自然语言）
    source_run_id: str = ""
    source_phase: Optional[str] = None
    confidence: float = 0.6                    # 0-1
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    last_accessed: str = Field(default_factory=lambda: datetime.now().isoformat())
    access_count: int = 0
    decay_score: float = 1.0                   # 衰减分数，< threshold 时 GC

    def to_dict(self) -> dict:
        """序列化为 DB 行（memory_type 存 string 值）"""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "memory_type": self.memory_type.value,
            "content": self.content,
            "source_run_id": self.source_run_id,
            "source_phase": self.source_phase or "",
            "confidence": self.confidence,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "decay_score": self.decay_score,
        }

    @classmethod
    def from_row(cls, row: dict) -> "MemoryEntry":
        """从 DB 行反序列化"""
        mt = row.get("memory_type", "user_preference")
        try:
            memory_type = MemoryType(mt)
        except ValueError:
            memory_type = MemoryType.USER_PREFERENCE
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            memory_type=memory_type,
            content=row.get("content", ""),
            source_run_id=row.get("source_run_id", ""),
            source_phase=row.get("source_phase") or None,
            confidence=row.get("confidence", 0.6),
            created_at=row.get("created_at", ""),
            last_accessed=row.get("last_accessed", ""),
            access_count=row.get("access_count", 0),
            decay_score=row.get("decay_score", 1.0),
        )
