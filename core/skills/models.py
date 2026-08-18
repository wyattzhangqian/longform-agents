"""Skill 平台 — Pydantic 模型"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SkillManifest(BaseModel):
    """Skill manifest（存 DB JSON）"""

    version: str = "1.0.0"
    # instruction=仅 Prompt · executable=已绑定可 invoke 的 Tool · mcp=MCP 代理工具
    kind: Literal["instruction", "executable", "mcp"] = "instruction"
    tools: List[str] = Field(default_factory=list)
    # 导入时物化为 ToolRegistry 中的可执行能力（对用户隐藏 Tool 层）
    tool_definitions: List[Dict[str, Any]] = Field(default_factory=list)
    catalog: Optional[Dict[str, Any]] = None  # 外部技能库元数据（如 SkillHub）
    mcp: Optional[Dict[str, Any]] = None
    prompt_snippet: str = ""
    input_schema: Optional[dict] = None
    # Phase 0：工具级默认策略（可选，注册到 ToolRegistry 时合并）
    default_permission_mode: Optional[str] = None  # allow | ask | deny
    default_risk_level: Optional[str] = None       # low | medium | high
    default_timeout_seconds: Optional[int] = None


class SkillDefinition(BaseModel):
    """Skill 完整定义"""

    id: str
    name: str
    description: str = ""
    source: Literal["platform", "custom", "mcp", "imported", "catalog"] = "custom"
    status: Literal["active", "archived"] = "active"
    manifest: SkillManifest = Field(default_factory=SkillManifest)
    mcp_config: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MCPServerDefinition(BaseModel):
    """MCP Server 连接配置"""

    id: str
    name: str
    transport: Literal["stdio", "sse"] = "sse"
    url: str = ""
    command: str = ""
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    auth_header: str = ""
    enabled: bool = True
    health_status: str = "unknown"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
