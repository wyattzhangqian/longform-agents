"""PermissionGate — 工具执行前权限决策（Phase 0）

规则层：allow / ask / deny，与阶段级 Review/Decision（人工层）配合。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent.tools import ToolDefinition


class PermissionMode(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionDecision:
    mode: PermissionMode
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.mode == PermissionMode.ALLOW


class PermissionGate:
    """工具调用权限门闩（deny > ask > allow）"""

    DEFAULT_TIMEOUT_SECONDS = 120

    @staticmethod
    def resolve_mode(tool: "ToolDefinition") -> PermissionMode:
        explicit = getattr(tool, "permission_mode", None) or ""
        if explicit in ("allow", "ask", "deny"):
            return PermissionMode(explicit)
        if getattr(tool, "requires_approval", False):
            return PermissionMode.ASK
        risk = (getattr(tool, "risk_level", None) or "low").lower()
        if risk in ("high", "critical"):
            return PermissionMode.ASK
        return PermissionMode.ALLOW

    @classmethod
    def check(cls, tool: "ToolDefinition", tool_name: str) -> PermissionDecision:
        mode = cls.resolve_mode(tool)
        if mode == PermissionMode.DENY:
            return PermissionDecision(
                mode=PermissionMode.DENY,
                reason=f"工具 {tool_name} 已被策略禁止执行",
            )
        if mode == PermissionMode.ASK:
            return PermissionDecision(
                mode=PermissionMode.ASK,
                reason=f"工具 {tool_name} 需要人工审批后方可执行",
            )
        return PermissionDecision(mode=PermissionMode.ALLOW)

    @staticmethod
    def effective_timeout_seconds(tool: "ToolDefinition") -> int:
        raw = getattr(tool, "timeout_seconds", None)
        if raw is None:
            return PermissionGate.DEFAULT_TIMEOUT_SECONDS
        try:
            return max(1, min(int(raw), 600))
        except (TypeError, ValueError):
            return PermissionGate.DEFAULT_TIMEOUT_SECONDS
