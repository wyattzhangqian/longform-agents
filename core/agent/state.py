"""Agent 工作流状态模型

取代裸 accumulated dict，提供类型化、可序列化、可 checkpoint 的状态管理。

核心模型:
- WorkflowState: 全局工作流状态（包含所有业务数据 + 循环控制）
- Observation: Agent 观察记录（工具结果、推理、事件）
- AgentOutput: 单个 Agent 的执行输出
- RoundTableState: 圆桌讨论专用状态
"""

from __future__ import annotations
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel, Field


# ============================================================
# Observation — 观察记录
# ============================================================

class Observation(BaseModel):
    """Agent 在循环中产生的观察记录

    来源可以是：工具调用结果、LLM 推理摘要、上游 Agent 输出、系统事件
    """
    source: str = ""                          # 来源标识（agent_id 或 tool_name）
    content: str = ""                         # 观察内容
    kind: Literal["tool_result", "reasoning", "agent_output", "system", "question_answer", "ask_peer"] = "system"
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ============================================================
# AgentOutput — 单 Agent 输出
# ============================================================

class AgentOutput(BaseModel):
    """单个 Agent 在一次执行中的输出记录"""
    agent_id: str = ""
    agent_name: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)  # Agent 产出的结构化数据
    iterations: int = 0                                    # 内部循环用了多少轮
    termination_reason: str = ""                           # "completed" | "max_iterations" | "error"
    raw_text: str = ""                                     # LLM 原始输出文本（调试用）
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None


# ============================================================
# RoundTableState — 圆桌讨论状态
# ============================================================

class RoundTableState(BaseModel):
    """圆桌讨论专用状态"""
    topic: str = ""
    current_round: int = 0
    max_rounds: int = 3
    consensus_threshold: float = 0.7
    consensus_reached: bool = False
    consensus_confidence: float = 0.0
    consensus_conclusion: str = ""
    round_history: List[Dict[str, Any]] = Field(default_factory=list)  # 每轮发言记录
    escalated: bool = False
    decision_card_id: Optional[int] = None
    status: Literal["idle", "discussing", "consensus_reached", "escalated"] = "idle"


# ============================================================
# WorkflowState — 工作流全局状态
# ============================================================

class WorkflowState(BaseModel):
    """工作流全局状态（Platform-Core v2: 纯净基座，无业务字段）

    取代原来的 accumulated: dict + 业务字段，提供类型安全的跨 Agent 数据传递。

    所有业务数据通过 agent_outputs[agent_id].result 流转，
    上游 Agent 产出 → set_agent_result() → 下游 Agent 通过 get_agent_result() 读取。

    用法：
        state = WorkflowState(task="分析用户数据并生成报告")
        state.set_agent_result("analyzer", {"analysis": "...", "data_points": 1000}, iterations=2)

        # Agent 读取上游数据
        prev = state.get_agent_result("analyzer")  # Optional[AgentOutput]
        analysis = prev.result.get("analysis") if prev else None
    """

    # ---- 任务 & 控制 ----
    task: str = ""                            # 当前任务描述
    project_id: str = ""                      # 所属项目 ID
    session_id: str = ""                      # 所属会话 ID (Session v2)
    workflow_mode: Literal["sequential", "parallel", "roundtable", "adaptive", "dag", "auto"] = "auto"

    # 循环控制（Agent 内部循环用，也用于引擎级迭代）
    global_iteration: int = 0                 # 全局迭代计数器
    max_global_iterations: int = 10           # 全局最大迭代
    termination_reason: str = ""              # "completed" | "error" | "paused" | "max_global_iterations"

    # ---- Agent 输出记录（所有业务数据流转的唯一通道）----
    agent_outputs: Dict[str, AgentOutput] = Field(default_factory=dict)

    # ---- 观察记录 ----
    observations: List[Observation] = Field(default_factory=list)

    # ---- 圆桌讨论 ----
    roundtable_state: Optional[RoundTableState] = None

    # ---- 配置快照 ----
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)

    # ---- checkpoint 版本（乐观锁）----
    checkpoint_version: int = 0

    # ---- 时间戳 ----
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    # ==================== Agent 输出管理 ====================

    def set_agent_result(
        self,
        agent_id: str,
        result: Dict[str, Any],
        iterations: int = 1,
        termination_reason: str = "completed",
        raw_text: str = "",
        agent_name: str = "",
        error: Optional[str] = None,
    ):
        """记录一个 Agent 的执行输出"""
        now = datetime.now().isoformat()
        existing = self.agent_outputs.get(agent_id)
        started_at = existing.started_at if existing else now

        self.agent_outputs[agent_id] = AgentOutput(
            agent_id=agent_id,
            agent_name=agent_name or agent_id,
            result=result,
            iterations=iterations,
            termination_reason=termination_reason,
            raw_text=raw_text,
            started_at=started_at,
            finished_at=now,
            error=error,
        )
        self.updated_at = now

    def get_agent_result(self, agent_id: str) -> Optional[AgentOutput]:
        """获取指定 Agent 的输出"""
        return self.agent_outputs.get(agent_id)

    def has_agent_completed(self, agent_id: str) -> bool:
        """检查 Agent 是否已完成"""
        output = self.agent_outputs.get(agent_id)
        return output is not None and output.termination_reason == "completed"

    # ==================== 观察管理 ====================

    def add_observation(
        self,
        source: str,
        content: str,
        kind: Literal["tool_result", "reasoning", "agent_output", "system", "question_answer", "ask_peer"] = "system",
        metadata: Dict[str, Any] = None,
    ):
        """添加一条观察记录"""
        self.observations.append(Observation(
            source=source,
            content=content[:500],  # 截断过长内容
            kind=kind,
            metadata=metadata or {},
        ))
        self.updated_at = datetime.now().isoformat()

        # 限制观察记录数量，保留最近 50 条
        if len(self.observations) > 50:
            self.observations = self.observations[-50:]

    def get_recent_observations(self, n: int = 5, kind: str = None) -> List[Observation]:
        """获取最近 N 条观察记录，可按 kind 过滤"""
        filtered = [o for o in self.observations if kind is None or o.kind == kind]
        return filtered[-n:]

    # ==================== 终止判定 ====================

    def is_done(self) -> bool:
        """全局终止判定"""
        if self.termination_reason:
            return True
        if self.global_iteration >= self.max_global_iterations:
            self.termination_reason = "max_global_iterations"
            return True
        return False

    def mark_done(self, reason: str = "completed"):
        """标记工作流完成"""
        self.termination_reason = reason
        self.updated_at = datetime.now().isoformat()

    def mark_error(self, error_msg: str):
        """标记工作流出错"""
        self.termination_reason = "error"
        self.add_observation("system", error_msg, kind="system")
        self.updated_at = datetime.now().isoformat()

    # ==================== 圆桌讨论 ====================

    def init_roundtable(self, topic: str, max_rounds: int = 3, threshold: float = 0.7):
        """初始化圆桌讨论状态"""
        self.roundtable_state = RoundTableState(
            topic=topic,
            max_rounds=max_rounds,
            consensus_threshold=threshold,
            status="discussing",
        )

    def record_round_opinion(self, round_num: int, agent_id: str, content: str):
        """记录圆桌讨论中的一轮发言"""
        if self.roundtable_state:
            self.roundtable_state.current_round = round_num
            self.roundtable_state.round_history.append({
                "round": round_num,
                "agent_id": agent_id,
                "content": content,
                "timestamp": datetime.now().isoformat(),
            })

    def set_consensus(self, reached: bool, confidence: float, conclusion: str = ""):
        """设置圆桌讨论共识结果"""
        if self.roundtable_state:
            self.roundtable_state.consensus_reached = reached
            self.roundtable_state.consensus_confidence = confidence
            self.roundtable_state.consensus_conclusion = conclusion
            self.roundtable_state.status = "consensus_reached" if reached else "escalated"

    # ==================== 向后兼容 ====================

    def to_accumulated(self) -> dict:
        """转换为旧版 accumulated dict 格式（向后兼容）

        Platform-Core v2: 所有数据来自 agent_outputs，不再有独立业务字段。
        """
        result: dict = {}

        # 将 agent_outputs 展平到 accumulated
        for agent_id, output in self.agent_outputs.items():
            result[agent_id] = output.result

        # 圆桌讨论结果
        if self.roundtable_state and self.roundtable_state.consensus_reached:
            result["roundtable_conclusion"] = self.roundtable_state.consensus_conclusion
            result["roundtable_rounds"] = self.roundtable_state.current_round

        return result

    @classmethod
    def from_accumulated(cls, accumulated: dict, project_id: str = "") -> "WorkflowState":
        """从旧的 accumulated dict 恢复 WorkflowState（迁移用）

        Platform-Core v2: 所有数据存入 agent_outputs，不再有独立业务字段。
        """
        state = cls(project_id=project_id)

        for agent_id, data in accumulated.items():
            if isinstance(data, dict):
                state.set_agent_result(agent_id, data)
            else:
                state.set_agent_result(agent_id, {"value": str(data)})

        return state

    # ==================== 序列化 ====================

    def dump_json(self) -> str:
        """序列化为 JSON 字符串（用于 checkpoint 持久化）"""
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def load_json(cls, json_str: str) -> "WorkflowState":
        """从 JSON 字符串反序列化"""
        return cls.model_validate_json(json_str)
