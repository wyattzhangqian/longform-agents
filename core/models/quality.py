"""质量检查数据模型 — QualityViolation / QualityCheckReport

统一 Pydantic 模型，消除 core/gateway/db_quality_gateway.py 中的重复定义。
自 v3.2 起为唯一正式定义。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """违规严重级别"""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class QualityViolation(BaseModel):
    """单条质量违规"""
    rule_id: str
    rule_name: str
    severity: str = "warning"             # error | warning | info
    message: str = ""
    fix_hint: str = ""
    auto_fix_capable: bool = False
    context: str = ""                      # 问题位置
    domain_id: str = ""
    passed: bool = False                   # 该条检查是否通过

    def model_dump(self, **kwargs) -> Dict[str, Any]:
        """序列化为 dict，保证与旧 to_dict() 兼容"""
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "message": self.message,
            "fix_hint": self.fix_hint,
            "auto_fix_capable": self.auto_fix_capable,
            "context": self.context,
            "domain_id": self.domain_id,
            "passed": self.passed,
        }


class QualityCheckReport(BaseModel):
    """一次完整质量检查的报告"""
    phase_id: str = ""
    agent_id: str = ""
    domain_id: str = ""
    total_rules: int = 0
    passed: int = 0                       # 通过的规则数
    warnings: int = 0
    errors: int = 0
    violations: List[QualityViolation] = Field(default_factory=list)
    results: List[QualityViolation] = Field(default_factory=list)  # 所有结果（含通过和未通过）
    overall_passed: bool = True
    checked_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    # 向后兼容别名
    @property
    def passed_count(self) -> int:
        return self.passed

    @property
    def warning_count(self) -> int:
        return self.warnings

    @property
    def error_count(self) -> int:
        return self.errors

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict（SSE 事件用）"""
        return {
            "total_rules": self.total_rules,
            "passed": self.passed,
            "warnings": self.warnings,
            "errors": self.errors,
            "overall_passed": self.overall_passed,
            "violations": [v.model_dump() for v in self.violations],
            "phase_id": self.phase_id,
            "agent_id": self.agent_id,
            "domain_id": self.domain_id,
        }


# ==================== 向后兼容导出 ====================
# 旧名称映射，避免破坏现有 import
Violation = QualityViolation
QualityCheckResult = QualityViolation  # db_quality_gateway 旧名称
