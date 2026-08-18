"""阶段策略与质量网关构建 — 从模板 workflow_config / QualityProfile 读取

架构说明（2026-07-31 统一）：领域质量规则由 DbQualityGateway 主路径
（core/gateway/db_quality_gateway.py，quality_rules 表驱动）承担；
本模块仅负责阶段策略与 QualityProfile 自带规则（pack_rules）的网关构建。
原 DomainAdapter/DomainBundle 数据源已随插件注册体系一并废弃。
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional

from core.gateway import QualityGateway
from core.gateway.rules import QualityRule as GatewayQualityRule


def get_phase_on_complete(
    workflow_config: Optional[Dict[str, Any]],
    agent_id: str,
    default: str = "continue",
) -> str:
    """读取阶段完成策略: continue | review | retry"""
    cfg = workflow_config or {}

    for phase in cfg.get("phases", []):
        pid = phase.get("agent_id") or phase.get("id")
        if pid == agent_id:
            return str(phase.get("on_complete", default))

    policies = cfg.get("phase_policies", {})
    policy = policies.get(agent_id)
    if isinstance(policy, dict):
        return str(policy.get("on_complete", default))
    if isinstance(policy, str):
        return policy

    return str(cfg.get("default_on_complete", default))


def build_quality_gateway_for_profile(
    quality_profile: Optional[Dict[str, Any]] = None,
    project_type: Optional[str] = None,
) -> QualityGateway:
    """按 Phase QualityProfile 构建网关 — 仅 pack_rules（D5）。

    领域规则不走这里：runtime 质量门在有 domain_id 时走 DbQualityGateway
    （quality_rules 表），本函数只装配 profile 显式携带的规则。
    """
    profile = quality_profile or {}
    gateway = QualityGateway()

    pack_rules = profile.get("pack_rules") or profile.get("rules") or []
    rules: List[GatewayQualityRule] = [
        GatewayQualityRule(
            rule_id=str(r.get("rule_id") or f"pack_{i}"),
            name=str(r.get("name") or r.get("rule_id") or f"rule_{i}"),
            description=str(r.get("description") or ""),
            check_type=str(r.get("check_type") or "generic"),
            severity=str(r.get("severity") or "warning"),
            domain_id=str(r.get("domain_id") or ""),
            config=dict(r.get("config") or {}),
        )
        for i, r in enumerate(pack_rules)
        if isinstance(r, dict)
    ]
    if rules:
        gateway.add_rules(rules)
        gateway.set_pipeline(list({r.check_type for r in rules}))
    return gateway
