"""DomainPrior — 领域先验读取与标准化 (Phase 1)

只做领域先验的**纯查询 + 标准化**，供 Planner 生成计划时注入领域最佳实践
（蓝图 §三-6「利用先验」：LLM 不是从零发明计划，而是「领域骨架 → 裁剪 → 补充 → 实例化」）。

不执行 Agent、不改变 GraphRuntime、不写具体领域分支（领域判断仍由
`DomainRegistry` / 领域数据承担，本模块只读不写）。
"""

from __future__ import annotations
import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PhasePrior(BaseModel):
    """领域推荐阶段的先验（来自 phase_definitions）"""
    id: str
    label: str = ""
    description: str = ""
    agent_role: str = ""                        # 该阶段推荐的角色（策划/撰稿/审核…）
    artifact_files: List[str] = Field(default_factory=list)  # 该阶段的期望产物


class DomainPrior(BaseModel):
    """领域先验的结构化表示"""
    domain_id: str
    name: str = ""
    phases: List[PhasePrior] = Field(default_factory=list)
    typical_agents: int = 0
    typical_mode: str = ""
    example_prompt: str = ""

    def to_prompt_context(self) -> str:
        """转成给 LLM 的结构化先验文本（紧凑，供 decompose/planner prompt 注入）。"""
        if not self.phases:
            return ""
        lines = [f"领域「{self.name}」({self.domain_id}) 的标准阶段骨架："]
        for i, p in enumerate(self.phases, 1):
            role = f"（角色：{p.agent_role}）" if p.agent_role else ""
            art = f"；产物：{', '.join(p.artifact_files)}" if p.artifact_files else ""
            lines.append(f"{i}. {p.label}{role}：{p.description}{art}")
        if self.typical_mode:
            lines.append(f"典型执行模式：{self.typical_mode}")
        if self.example_prompt:
            lines.append(f"示例任务：{self.example_prompt}")
        return "\n".join(lines)


def _parse_json_field(raw: Any) -> Any:
    """phase_definitions/metadata 可能是 JSON str 或已解析对象。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {} if raw.startswith("{") else []
    return raw or ({} if isinstance(raw, dict) else [])


def normalize_domain_prior(domain: Dict[str, Any]) -> DomainPrior:
    """从 quality_domains 行 dict 标准化为 DomainPrior（同步，供单测直接调）。"""
    phase_defs = _parse_json_field(domain.get("phase_definitions"))
    metadata = _parse_json_field(domain.get("metadata")) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    phases = []
    for pd in phase_defs if isinstance(phase_defs, list) else []:
        if not isinstance(pd, dict):
            continue
        phases.append(PhasePrior(
            id=pd.get("id", ""),
            label=pd.get("label", pd.get("id", "")),
            description=pd.get("description", ""),
            agent_role=pd.get("agent_role", ""),
            artifact_files=list(pd.get("artifact_files") or []),
        ))

    return DomainPrior(
        domain_id=domain.get("id", ""),
        name=domain.get("name", ""),
        phases=phases,
        typical_agents=int(metadata.get("typical_agents") or 0),
        typical_mode=str(metadata.get("typical_mode") or ""),
        example_prompt=str(metadata.get("example_prompt") or ""),
    )


async def load_domain_prior(domain_id: str) -> Optional[DomainPrior]:
    """读领域先验并标准化。领域不存在 / 读取失败返回 None（调用方降级通用策略）。"""
    if not domain_id:
        return None
    try:
        from core.gateway.domain_registry import DomainRegistry
        domain = await DomainRegistry.get_domain(domain_id)
        if not domain:
            return None
        return normalize_domain_prior(domain)
    except Exception:
        return None
