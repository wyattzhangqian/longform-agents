"""CapabilityDiscovery — 按能力/标签/兼容性发现与排序 Agent"""

from __future__ import annotations
from typing import Any, Dict, List, Optional, Set

from core.agent.types import AgentDefinition
from core.logging import get_logger

_logger = get_logger("discovery")


class CapabilityDiscovery:
    """能力发现服务 — 替代散落的 tag 字符串匹配"""

    def __init__(self, agents: List[AgentDefinition]):
        self._agents = [a for a in agents if a.status == "active"]

    def discover(
        self,
        required_capabilities: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        search: Optional[str] = None,
        exclude_ids: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """发现候选 Agent，按匹配分排序"""
        required = set(required_capabilities or [])
        tag_set = set(tags or [])
        excluded = set(exclude_ids or [])
        search_l = (search or "").lower()

        results: List[Dict[str, Any]] = []
        for agent in self._agents:
            if agent.id in excluded:
                continue
            score, reasons = self._score_agent(agent, required, tag_set, search_l)
            if score <= 0 and (required or tag_set or search_l):
                continue
            if score <= 0:
                score = 0.1
            results.append({
                "id": agent.id,
                "name": agent.name,
                "role": agent.role,
                "emoji": agent.emoji,
                "type": agent.type,
                "capabilities": agent.capabilities,
                "score": round(score, 3),
                "reasons": reasons,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def best_for_capabilities(
        self,
        required_capabilities: List[str],
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional[str]:
        """为能力集合选最佳单个 Agent"""
        hits = self.discover(
            required_capabilities=required_capabilities,
            exclude_ids=exclude_ids,
            limit=1,
        )
        return hits[0]["id"] if hits else None

    def rank_for_team(
        self,
        required_capabilities: List[str],
        team_size: int = 5,
        exclude_ids: Optional[List[str]] = None,
    ) -> List[str]:
        """按能力覆盖度贪心组队"""
        required = list(dict.fromkeys(required_capabilities))
        picked: List[str] = []
        covered: Set[str] = set()
        excluded = set(exclude_ids or [])

        while len(picked) < team_size and len(covered) < len(required):
            best_id, best_gain = None, 0.0
            for agent in self._agents:
                if agent.id in excluded or agent.id in picked:
                    continue
                caps = set(agent.capabilities)
                gain = len((set(required) - covered) & caps)
                if gain > best_gain:
                    best_gain = gain
                    best_id = agent.id
                elif gain == best_gain and best_id:
                    s1 = self._score_agent(agent, set(required) - covered, set(), "")[0]
                    prev = next(a for a in self._agents if a.id == best_id)
                    s0 = self._score_agent(prev, set(required) - covered, set(), "")[0]
                    if s1 > s0:
                        best_id = agent.id
            if not best_id or best_gain == 0:
                break
            picked.append(best_id)
            excluded.add(best_id)
            agent = next(a for a in self._agents if a.id == best_id)
            covered |= set(agent.capabilities) & set(required)

        if len(picked) < team_size:
            for agent in self._agents:
                if agent.id not in picked and agent.id not in (exclude_ids or []):
                    picked.append(agent.id)
                if len(picked) >= team_size:
                    break
        return picked[:team_size]

    def check_pair_compatible(self, agent: AgentDefinition, target: AgentDefinition) -> tuple[bool, str]:
        """双 Agent 兼容性（peers 白名单 + 能力交集）"""
        if agent.peers and target.id not in agent.peers:
            return False, f"{agent.name} 的 peers 不包含 {target.name}"
        shared = set(agent.capabilities) & set(target.capabilities)
        if not shared and agent.capabilities and target.capabilities:
            return False, "无共同能力标签"
        return True, "兼容"

    def _score_agent(
        self,
        agent: AgentDefinition,
        required: Set[str],
        tags: Set[str],
        search_l: str,
    ) -> tuple[float, List[str]]:
        score = 0.0
        reasons: List[str] = []
        caps = set(agent.capabilities)

        if required:
            matched = caps & required
            if matched:
                rate = len(matched) / len(required)
                score += rate * 0.7
                reasons.append(f"能力匹配 {len(matched)}/{len(required)}")
            else:
                # 能力标签未精确匹配时，搜索 system_prompt / name / role 中的关键词
                # 这让 LLM 分解出的能力标签（如 "analysis"）能匹配到 system_prompt 中
                # 提到"分析"的 agent，即使 capability 列表中没有 "analysis"
                prompt_l = (agent.system_prompt or "").lower()
                name_role_l = f"{agent.name} {agent.role}".lower()
                sem_hits = 0
                for req in required:
                    req_l = req.lower()
                    if req_l in prompt_l or req_l in name_role_l:
                        sem_hits += 1
                if sem_hits > 0:
                    rate = sem_hits / len(required)
                    score += rate * 0.4  # 语义匹配权重低于精确匹配
                    reasons.append(f"语义匹配 {sem_hits}/{len(required)}（prompt/名称）")
                else:
                    return 0.0, []

        if tags:
            agent_tags = set(agent.type.split()) | caps | {agent.type}
            tag_hit = tags & agent_tags
            if tag_hit:
                score += 0.2 * len(tag_hit)
                reasons.append(f"标签 {', '.join(tag_hit)}")

        if search_l:
            hay = f"{agent.name} {agent.role} {' '.join(caps)} {(agent.system_prompt or '')[:200]}".lower()
            if search_l in hay:
                score += 0.15
                reasons.append("关键词命中")

        metrics = agent.performance_metrics
        if metrics:
            base = metrics.base if hasattr(metrics, "base") else {}
            if isinstance(base, dict) and base.get("success_rate"):
                score += min(0.15, float(base["success_rate"]) * 0.15)
                reasons.append("历史成功率加权")

        if agent.specialized_knowledge:
            score += min(0.05, len(agent.specialized_knowledge) * 0.01)

        return score, reasons


async def discover_agents(
    required_capabilities: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    search: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """便捷函数：从 Registry 发现 Agent"""
    from core.agent.registry import get_registry

    registry = get_registry()
    agents = await registry.list({"status": "active"})
    return CapabilityDiscovery(agents).discover(
        required_capabilities=required_capabilities,
        tags=tags,
        search=search,
        limit=limit,
    )
