"""SSE 事件模型 — Graph Engine 运行时事件

定义 Pydantic 模型用于 SSE 事件序列化。
事件通过 GraphRuntime._emit() → EventBus.broadcast() 推送到前端。
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel


class RouterDecisionEvent(BaseModel):
    """router_decision SSE 事件 — 自适应路由决策

    在节点执行完成后、下一个节点开始前发出。
    前端可据此显示路由决策原因和重试状态。
    """
    event_type: Literal["router_decision"] = "router_decision"
    node_id: str
    node_name: str
    decision: str            # continue / retry / ask_human / skip
    reasoning: str = ""
    feedback: Optional[str] = None
    retry_count: int = 0
    total_iterations: int = 0


class ReplayStartedEvent(BaseModel):
    """replay_started SSE 事件 — Checkpoint 回放启动

    从指定 phase 回溯重放时发出，前端可据此显示回放进度。
    """
    event_type: Literal["replay_started"] = "replay_started"
    run_id: str
    from_phase_id: str
    phases_to_rerun: List[str] = []


class QualityCheckEvent(BaseModel):
    """quality_check SSE 事件 — 结构化质量检查报告

    在 QUALITY_GATE 节点执行后发出，含完整 violations 数组（含 suggestion 字段）。
    向后兼容：保留 passed/violations_count/message/severity 字段。
    """
    event_type: Literal["quality_check"] = "quality_check"
    node_id: str = ""
    node_name: str = ""
    phase_id: str = ""
    agent_id: str = ""
    passed: bool = True
    score: float = 0.0
    error_count: int = 0
    warning_count: int = 0
    violations: List[dict] = []           # Violation.model_dump() 列表
    overall_suggestion: Optional[str] = None
    # 向后兼容字段
    violations_count: int = 0
    message: str = ""
    severity: str = "info"
