"""Agent Team Builder — 动态组队与角色协商 (AInative v3)

让 Agent 不再被静态模板固定，而是根据任务描述自主协商组队。

核心流程：
    1. 任务分析 → 拆解所需角色和能力
    2. 角色发布 → 向候选 Agent 广播角色需求
    3. Agent 自主投标 → 各 Agent 根据自身能力提交参与意愿
    4. 角色协商 → 多个 Agent 竞争同一角色时协商分配
    5. 团队组建 → 输出 TeamComposition

这是 AInative 区别于传统工作流引擎的关键能力之一。
"""

from __future__ import annotations
from typing import Optional, List, Dict, Any, Set
from datetime import datetime
from pydantic import BaseModel, Field
from core.agent.types import AgentDefinition
from core.agent.base import BaseAgent
from core.logging import get_logger

_logger = get_logger("team_builder")


# ============================================================
# 数据模型
# ============================================================

class RequiredRole(BaseModel):
    """任务需要的角色定义"""
    role_id: str
    name: str = ""
    description: str = ""                              # 角色职责描述
    required_capabilities: List[str] = Field(default_factory=list)  # 必需能力
    preferred_capabilities: List[str] = Field(default_factory=list) # 优先能力
    min_confidence: float = 0.3                        # 最低自信度


class AgentBid(BaseModel):
    """Agent 对某个角色的投标"""
    agent_id: str
    agent_name: str = ""
    role_id: str
    confidence: float = 0.0                            # 自信程度 0-1
    reasoning: str = ""                                # 为什么认为自己适合
    matched_capabilities: List[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class TeamAssignment(BaseModel):
    """角色分配结果"""
    role: RequiredRole
    agent_id: str
    agent_name: str = ""
    confidence: float = 0.0
    assigned_by: str = "auto"                          # auto | user | orchestrator


class TeamComposition(BaseModel):
    """团队组建结果"""
    team_id: str
    task_description: str
    assignments: List[TeamAssignment] = Field(default_factory=list)
    unassigned_roles: List[RequiredRole] = Field(default_factory=list)  # 未找到合适 Agent 的角色
    total_agents: int = 0
    avg_confidence: float = 0.0
    formed_at: str = Field(default_factory=lambda: datetime.now().isoformat())


# ============================================================
# AgentTeamBuilder — 动态组队引擎
# ============================================================

class AgentTeamBuilder:
    """动态组队引擎

    将任务需求与 Agent 能力池匹配，通过投标-协商机制自主组建团队。

    用法：
        builder = AgentTeamBuilder(available_agents)
        composition = await builder.form_team(
            task="对用户行为数据进行聚类分析和可视化",
            required_capabilities=["data_analysis", "visualization"],
        )
    """

    def __init__(self, available_agents: Dict[str, AgentDefinition]):
        self.agents = available_agents
        self.bids: Dict[str, List[AgentBid]] = {}      # role_id → bids

    async def form_team(
        self,
        task: str,
        required_capabilities: List[str] = None,
        roles: List[RequiredRole] = None,
        mode: str = "best_fit",                        # best_fit | all_willing | ranked
    ) -> TeamComposition:
        """组建团队

        Args:
            task: 任务描述
            required_capabilities: 任务需要的全局能力（自动拆解为角色）
            roles: 预定义角色（可选，不传则自动从 capabilities 推导）
            mode: 组建模式
                - best_fit: 每个角色选最合适的 Agent
                - all_willing: 所有有意愿的 Agent 都加入
                - ranked: 按匹配度排序分配

        Returns:
            TeamComposition 团队组建结果
        """
        import uuid

        # 1. 分析角色需求
        if roles is None:
            roles = self._derive_roles(task, required_capabilities or [])

        _logger.info("组队: task=%s, roles=%s, mode=%s",
                      task[:50], [r.role_id for r in roles], mode)

        # 2. 开放投标
        self.bids.clear()
        for role in roles:
            self.bids[role.role_id] = await self._collect_bids(role)

        # 3. 角色分配
        assignments, unassigned = self._assign_roles(roles, mode)

        # 4. 构建结果
        team_id = f"team_{uuid.uuid4().hex[:8]}"
        all_confidence = [a.confidence for a in assignments] if assignments else [0]

        composition = TeamComposition(
            team_id=team_id,
            task_description=task,
            assignments=assignments,
            unassigned_roles=unassigned,
            total_agents=len(set(a.agent_id for a in assignments)),
            avg_confidence=sum(all_confidence) / len(all_confidence) if all_confidence else 0,
        )

        _logger.info("团队组建完成: team=%s, agents=%d, avg_confidence=%.2f, unassigned=%d",
                      team_id, composition.total_agents,
                      composition.avg_confidence, len(unassigned))

        return composition

    # ==================== 内部方法 ====================

    async def _collect_bids(self, role: RequiredRole) -> List[AgentBid]:
        """收集所有 Agent 对某个角色的投标"""
        bids = []

        for agent_id, agent_def in self.agents.items():
            # 计算能力匹配度
            capabilities = set(agent_def.capabilities)
            required = set(role.required_capabilities)
            preferred = set(role.preferred_capabilities)

            matched_required = capabilities & required
            match_rate = len(matched_required) / len(required) if required else 0.5

            # 低于最低自信度则跳过
            if match_rate < role.min_confidence:
                continue

            # 生成投标
            confidence = match_rate
            if capabilities & preferred:
                confidence = min(1.0, confidence + 0.1)

            bid = AgentBid(
                agent_id=agent_id,
                agent_name=agent_def.name,
                role_id=role.role_id,
                confidence=confidence,
                reasoning=self._generate_bid_reasoning(agent_def, role, match_rate),
                matched_capabilities=list(matched_required),
            )
            bids.append(bid)

        from core.discovery.capability import CapabilityDiscovery
        ranked = CapabilityDiscovery(list(self.agents.values())).discover(
            required_capabilities=list(role.required_capabilities),
            limit=max(len(bids), 20),
        )
        rank_map = {r["id"]: r["score"] for r in ranked}
        for bid in bids:
            if bid.agent_id in rank_map:
                bid.confidence = min(1.0, (bid.confidence + rank_map[bid.agent_id]) / 2)

        bids.sort(key=lambda b: b.confidence, reverse=True)
        return bids

    def _assign_roles(
        self,
        roles: List[RequiredRole],
        mode: str,
    ) -> tuple[List[TeamAssignment], List[RequiredRole]]:
        """分配角色给 Agent"""
        assignments = []
        assigned_agents: Set[str] = set()

        for role in roles:
            role_bids = self.bids.get(role.role_id, [])

            if not role_bids:
                continue  # 无人投标，留在 unassigned

            if mode == "best_fit":
                # 选最合适的（且未分配）
                for bid in role_bids:
                    if bid.agent_id not in assigned_agents:
                        assignments.append(TeamAssignment(
                            role=role,
                            agent_id=bid.agent_id,
                            agent_name=bid.agent_name,
                            confidence=bid.confidence,
                        ))
                        assigned_agents.add(bid.agent_id)
                        break

            elif mode == "all_willing":
                # 所有投标者都加入
                for bid in role_bids:
                    assignments.append(TeamAssignment(
                        role=role,
                        agent_id=bid.agent_id,
                        agent_name=bid.agent_name,
                        confidence=bid.confidence,
                    ))
                    assigned_agents.add(bid.agent_id)

            elif mode == "ranked":
                # 按匹配度排序，每人最多一个角色
                for bid in role_bids:
                    if bid.agent_id not in assigned_agents:
                        assignments.append(TeamAssignment(
                            role=role,
                            agent_id=bid.agent_id,
                            agent_name=bid.agent_name,
                            confidence=bid.confidence,
                        ))
                        assigned_agents.add(bid.agent_id)
                        break

        # 未分配的 role
        assigned_role_ids = {a.role.role_id for a in assignments}
        unassigned = [r for r in roles if r.role_id not in assigned_role_ids]

        return assignments, unassigned

    @staticmethod
    def _derive_roles(task: str, capabilities: List[str]) -> List[RequiredRole]:
        """从任务描述和能力需求推导所需角色

        基础推导：每个能力一个角色。应用层可通过 LLM 做更智能的推导。
        """
        roles = []
        for i, cap in enumerate(capabilities):
            roles.append(RequiredRole(
                role_id=f"role_{cap}",
                name=cap.replace("_", " ").title(),
                description=f"负责 {cap} 相关任务",
                required_capabilities=[cap],
            ))
        # 至少有一个协调者角色
        if not roles:
            roles.append(RequiredRole(
                role_id="coordinator",
                name="Coordinator",
                description="任务协调与结果整合",
                required_capabilities=["coordination", "synthesis"],
            ))
        return roles

    @staticmethod
    def _generate_bid_reasoning(
        agent_def: AgentDefinition,
        role: RequiredRole,
        match_rate: float,
    ) -> str:
        """生成投标理由"""
        if match_rate >= 0.8:
            return f"能力高度匹配 ({len(agent_def.capabilities)} 项能力覆盖角色需求)"
        elif match_rate >= 0.5:
            return f"能力部分匹配，可胜任角色核心职责"
        else:
            return f"具备基础能力，愿意尝试"


