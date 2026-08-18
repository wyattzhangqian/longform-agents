"""Asset Vault 模型 — 资产版本控制 + 锁机制"""

from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


class AssetVersion(BaseModel):
    """资产的版本记录"""
    version_id: str
    asset_id: str
    version_num: int = 1
    content_hash: str = ""              # 内容哈希（SHA256）
    file_path: str = ""                  # 存储路径
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = ""                 # 创建者 agent_id
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class AssetLock(BaseModel):
    """资产锁（乐观锁）"""
    asset_id: str
    locked_by: str = ""                  # 锁定者 agent_id
    locked_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    expires_at: Optional[str] = None     # 锁过期时间
    reason: str = ""                     # 锁定原因
