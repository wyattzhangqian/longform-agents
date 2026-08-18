"""Tool 执行上下文与审计载荷 — Phase 0 运行时契约"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass
class ToolExecutionContext:
    """单次工具调用上下文"""

    project_id: str = ""
    agent_id: str = ""
    trace_id: str = ""
    run_id: str = ""  # P0-3：产物归属校验用（登记产物时写入 run_id）
    emit: Optional[Callable[[str, dict], None]] = None

    def audit(self, event: str, payload: Dict[str, Any]) -> None:
        if not self.emit:
            return
        body = {
            "tool_call_id": payload.get("tool_call_id") or str(uuid.uuid4()),
            "project_id": self.project_id,
            "agent_id": self.agent_id,
            "trace_id": self.trace_id,
            "ts": time.time(),
            **payload,
        }
        self.emit(event, body)


def validate_tool_args(parameters: dict, tool_args: dict) -> Optional[str]:
    """轻量 JSON Schema 校验（required + properties 键）"""
    if not parameters or parameters.get("type") != "object":
        return None
    props = parameters.get("properties") or {}
    if not isinstance(tool_args, dict):
        return "tool_args 必须为对象"
    for key in parameters.get("required") or []:
        if key not in tool_args:
            return f"缺少必填参数: {key}"
    if props:
        for key in tool_args:
            if key not in props:
                return f"未知参数: {key}"
    return None
