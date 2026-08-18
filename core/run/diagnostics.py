"""Run 降级诊断 — 所有"非致命"降级必须经此记录，不允私下静默。

设计动机（2026-08-09 P0）：
  平台存在大量 except:pass / "非致命" 降级，降级动作不记录到 run 状态，
  用户看到 run COMPLETED 但内容实际残缺，无法判断是否可信。
  本模块为所有降级提供统一登记入口，最终透传给用户（SSE run_degraded /
  API diagnostics / 前端警告）。

使用约束：
  - Python 3.9：禁止 `X | None`，用 `Optional[X]`。
  - loguru 不做 %s 格式化，日志必须用 {} 或 f-string。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from dataclasses import dataclass, field


class Severity(str, Enum):
    """降级严重度 — 决定是否让用户知道、是否标记 run degraded。"""

    CRITICAL = "critical"      # 内容一致性直接受损（骨架丢失/记忆失效/产物未落盘）
    WARNING = "warning"        # 功能降级但可继续（知识未注入/RAG 不可用）
    INFO = "info"              # 辅助功能失效（进化日志/氛围追踪）


@dataclass
class DiagnosticEntry:
    severity: Severity
    component: str             # 如 "skeleton_planner", "quality_gate", "materialize"
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    recovery_action: str = ""  # 系统采取了什么降级动作
    # P0-3 扩充：结构化关联字段（run/phase/node 溯源 + 错误码 + 是否走了兜底）
    run_id: str = ""
    phase_id: str = ""
    node_id: str = ""
    code: str = ""             # 结构化错误码，如 "quality_gate_timeout"
    fallback_used: str = ""    # 降级动作名，如 "fallback_skeleton" / "retry" / "skip"
    source: str = ""           # 来源模块


class RunDiagnostics:
    """挂载在 RunContext 上，所有降级经 record() 记录。

    is_degraded = 任一 CRITICAL 或 WARNING → run 结果标记 degraded=True。
    """

    def __init__(self) -> None:
        self.entries: List[DiagnosticEntry] = []

    def record(
        self,
        severity: Severity,
        component: str,
        message: str,
        recovery_action: str = "",
        *,
        run_id: str = "",
        phase_id: str = "",
        node_id: str = "",
        code: str = "",
        fallback_used: str = "",
        source: str = "",
    ) -> DiagnosticEntry:
        entry = DiagnosticEntry(
            severity=severity,
            component=component,
            message=message,
            recovery_action=recovery_action,
            run_id=run_id,
            phase_id=phase_id,
            node_id=node_id,
            code=code,
            fallback_used=fallback_used,
            source=source,
        )
        self.entries.append(entry)
        return entry

    @property
    def has_critical(self) -> bool:
        return any(e.severity == Severity.CRITICAL for e in self.entries)

    @property
    def is_degraded(self) -> bool:
        return any(e.severity in (Severity.CRITICAL, Severity.WARNING) for e in self.entries)

    def summary(self) -> Dict[str, Any]:
        return {
            "is_degraded": self.is_degraded,
            "has_critical": self.has_critical,
            "critical_count": sum(1 for e in self.entries if e.severity == Severity.CRITICAL),
            "warning_count": sum(1 for e in self.entries if e.severity == Severity.WARNING),
            "info_count": sum(1 for e in self.entries if e.severity == Severity.INFO),
            "fallback_count": sum(1 for e in self.entries if e.fallback_used),
            "entries": [
                {
                    "severity": e.severity.value,
                    "component": e.component,
                    "message": e.message,
                    "recovery_action": e.recovery_action,
                    "code": e.code,
                    "fallback_used": e.fallback_used,
                    "run_id": e.run_id,
                    "phase_id": e.phase_id,
                    "node_id": e.node_id,
                    "source": e.source,
                    "timestamp": e.timestamp,
                }
                for e in self.entries
            ],
        }
