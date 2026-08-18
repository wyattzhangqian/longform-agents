"""Quality Gateway — 质量门禁调度器

在编排生命周期的关键节点执行质量检查。
支持可配置的检查 pipeline，接入自动重做机制。

纯通用层：不预设任何业务检查规则。
领域规则由质量领域体系提供（quality_rules 表，经 DbQualityGateway 加载）。
"""

from __future__ import annotations
from typing import List, Dict, Any
from .rules import QualityRule, RuleEngine


class QualityGateway:
    """质量门禁 — 编排生命周期中的质量检查点

    用法：
        gateway = QualityGateway()
        gateway.add_rules(domain_adapter.get_quality_rules())

        # 阶段完成后检查
        violations = gateway.evaluate(agent_id, result)
        if not RuleEngine.passes(violations):
            # 触发重做或告警
            ...
    """

    def __init__(self):
        self._rules: List[QualityRule] = []
        self._pipeline: List[str] = ["generic"]  # 默认检查顺序（纯通用）
        self._history: List[Dict[str, Any]] = []

    def add_rules(self, rules: List[QualityRule]):
        """添加质量规则"""
        for rule in rules:
            if rule not in self._rules:
                self._rules.append(rule)

    def set_pipeline(self, stages: List[str]):
        """设置检查 pipeline 的检查类型顺序"""
        self._pipeline = stages

    def evaluate(
        self,
        agent_id: str,
        result: Any,
        check_types: List[str] = None,
    ) -> List[dict]:
        """对 Agent 产出执行质量检查"""
        if check_types is None and self._rules:
            violations = RuleEngine.check(self._rules, result)
            all_violations = list(violations)
        else:
            types = check_types or self._pipeline
            all_violations = []
            for check_type in types:
                type_rules = [r for r in self._rules if r.check_type == check_type]
                if type_rules:
                    violations = RuleEngine.check(type_rules, result)
                    all_violations.extend(violations)

        self._history.append({
            "agent_id": agent_id,
            "violations_count": len(all_violations),
            "passed": RuleEngine.passes(all_violations),
            "violations": all_violations,
        })

        return all_violations

    def get_history(self) -> List[Dict[str, Any]]:
        """获取检查历史"""
        return self._history

    def clear_history(self):
        """清空检查历史"""
        self._history.clear()

    def should_retry(self, violations: List[dict], max_retries: int = 3) -> bool:
        """判断是否应该自动重做"""
        error_count = sum(1 for v in violations if v["severity"] == "error")
        if error_count == 0:
            return False
        recent_errors = sum(
            1 for h in self._history[-max_retries:]
            if not h["passed"]
        )
        return recent_errors < max_retries
