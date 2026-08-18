"""Prometheus 指标 — 工作流 / 阶段 / SSE"""

from __future__ import annotations
from typing import Optional

from config import PROMETHEUS_ENABLED

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain"


class PlatformMetrics:
    """平台指标封装 — PROMETHEUS_ENABLED=false 时 no-op"""

    def __init__(self):
        self.enabled = PROMETHEUS_ENABLED and _AVAILABLE
        if not self.enabled:
            return

        self.workflow_runs_total = Counter(
            "agent_platform_workflow_runs_total",
            "工作流执行次数",
            ["status", "mode"],
        )
        self.workflow_active = Gauge(
            "agent_platform_workflow_active",
            "当前运行中的工作流数",
        )
        self.phase_duration_seconds = Histogram(
            "agent_platform_phase_duration_seconds",
            "Agent 阶段耗时（秒）",
            ["phase", "mode"],
            buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
        )
        self.workflow_events_total = Counter(
            "agent_platform_workflow_events_total",
            "工作流事件计数",
            ["event_type"],
        )
        self.sse_clients = Gauge(
            "agent_platform_sse_clients",
            "SSE 连接数",
        )
        self.checkpoint_saves_total = Counter(
            "agent_platform_checkpoint_saves_total",
            "Checkpoint 保存次数",
            ["result"],
        )

    def workflow_started(self, mode: str = "sequential") -> None:
        if not self.enabled:
            return
        self.workflow_active.inc()

    def workflow_finished(self, status: str, mode: str = "sequential") -> None:
        if not self.enabled:
            return
        self.workflow_runs_total.labels(status=status, mode=mode).inc()
        self.workflow_active.dec()

    def record_phase(self, phase: str, duration: float, mode: str = "sequential") -> None:
        if not self.enabled:
            return
        self.phase_duration_seconds.labels(phase=phase, mode=mode).observe(duration)

    def record_event(self, event_type: str) -> None:
        if not self.enabled:
            return
        self.workflow_events_total.labels(event_type=event_type).inc()

    def set_sse_clients(self, count: int) -> None:
        if not self.enabled:
            return
        self.sse_clients.set(count)

    def checkpoint_saved(self, ok: bool = True) -> None:
        if not self.enabled:
            return
        self.checkpoint_saves_total.labels(result="ok" if ok else "conflict").inc()

    def export(self) -> tuple[bytes, str]:
        if not self.enabled:
            return b"# Prometheus metrics disabled\n", "text/plain"
        return generate_latest(), CONTENT_TYPE_LATEST


metrics = PlatformMetrics()
