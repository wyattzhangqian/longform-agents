"""RunContext — 单次 Run 的唯一状态真相 (D2)

合并原 WorkflowState + GraphState 业务语义；checkpoint 只持久化此结构。
"""

from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field, PrivateAttr

from core.agent.state import AgentOutput, Observation, RoundTableState, WorkflowState


class PeerReply(BaseModel):
    """同伴回复 — ask_peer 子图调用结果"""
    content: str = ""
    status: Literal["confirmed", "open", "timeout"] = "confirmed"
    constraints: List[str] = Field(default_factory=list)


class SubgraphResult(BaseModel):
    """子图执行结果 — execute_subgraph 返回"""
    success: bool = False
    output: str = ""
    error: str = ""


class QualityRecord(BaseModel):
    phase_id: str = ""
    agent_id: str = ""
    passed: bool = True
    score: float = 0.0
    violations: List[Dict[str, Any]] = Field(default_factory=list)
    checked_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class WorkspaceRef(BaseModel):
    """大产物引用 — 协作不传全文 (Anthropic artifact 模式)"""
    artifact_id: str = ""
    path: str = ""
    summary: str = ""
    agent_id: str = ""
    phase_id: str = ""


class CollaborationThreadEntry(BaseModel):
    protocol: Literal["handoff", "delegate", "broadcast", "status", "ack", "dialogue"] = "status"
    from_agent: str = ""
    to_agent: str = ""
    phase_id: str = ""
    summary: str = ""
    workspace_refs: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class NegotiationEntry(BaseModel):
    """协商台账条目 — 跨阶段累积的约束 / 未决问题（D6 扩展）

    shared_thread 问答、ask_peer 中途提问、人类插话的结论统一沉淀于此，
    供下游 prompt 注入与质量门禁校验。
    """
    id: str = ""
    kind: Literal["constraint", "open_question", "decision"] = "constraint"
    phase_id: str = ""
    from_agent: str = ""
    to_agent: str = ""
    text: str = ""
    status: Literal["open", "confirmed", "disputed", "satisfied", "violated", "resolved"] = "confirmed"
    source: Literal["shared_thread", "ask_peer", "human", "system"] = "shared_thread"
    round_num: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class HumanInterjection(BaseModel):
    """人类插话 — 注入进行中的协作线程，下一个执行的 Agent 消费"""
    id: str = ""
    content: str = ""
    target_agent: str = ""  # 空 = 任意下一个 Agent
    phase_id: str = ""
    consumed_by: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class RunControl(BaseModel):
    pause_reason: str = ""
    pending_human: bool = False
    pending_decision_id: Optional[int] = None
    termination_reason: str = ""
    pause_requested: bool = False       # 用户主动请求暂停
    cancel_requested: bool = False      # 用户主动请求取消


class RunContext(BaseModel):
    """Run 级唯一状态"""

    run_id: str = ""
    project_id: str = ""
    session_id: str = ""
    task: str = ""

    current_phase_id: str = ""
    completed_phase_ids: List[str] = Field(default_factory=list)
    phase_specs: List[Dict[str, Any]] = Field(default_factory=list)

    agent_outputs: Dict[str, AgentOutput] = Field(default_factory=dict)
    workspace_refs: List[WorkspaceRef] = Field(default_factory=list)
    collaboration_thread: List[CollaborationThreadEntry] = Field(default_factory=list)
    quality_records: List[QualityRecord] = Field(default_factory=list)
    observations: List[Observation] = Field(default_factory=list)
    roundtable_state: Optional[RoundTableState] = None
    negotiation_ledger: List[NegotiationEntry] = Field(default_factory=list)
    human_interjections: List[HumanInterjection] = Field(default_factory=list)

    control: RunControl = Field(default_factory=RunControl)
    graph: Dict[str, Any] = Field(default_factory=dict)  # current_node, completed_nodes, ...
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)

    # --- Phase 1 新增：长内容骨架支持 ---
    skeleton: Optional[Dict[str, Any]] = None  # Skeleton.model_dump() 结果
    current_unit: Optional[int] = None
    content_type: str = ""
    total_units: int = 0

    # --- Phase 2 新增：分层记忆 + 连续性追踪 ---
    layered_memory: Optional[Dict[str, Any]] = None   # LayeredMemory.to_dict() 结果
    continuity_state: Optional[List[Dict[str, Any]]] = None  # ContinuityTracker.to_state() 结果

    # --- Phase 3 新增：氛围追踪 + 角色 Profile ---
    atmosphere_state: Optional[Dict[str, Any]] = None    # AtmosphereTracker.to_state()
    character_profiles: Optional[List[Dict[str, Any]]] = None  # CharacterProfileStore.to_state()

    # --- 降级诊断（2026-08-09 P0）：非致命降级记录，最终透传用户 ---
    diagnostics: Optional[Dict[str, Any]] = None  # RunDiagnostics.summary() 的序列化形式

    # Replay revision contexts — phase_id → revision info (not persisted in checkpoint)
    _revision_contexts: Dict[str, Dict[str, Any]] = PrivateAttr(default_factory=dict)
    # ask_peer 递归深度计数器（防子图嵌套死锁）
    _peer_depth: int = PrivateAttr(default=0)
    # 并行节点写保护锁（防 set_agent_result 多步操作竞态）
    _write_lock: Any = PrivateAttr(default=None)
    # 在线降级累加器（不序列化；checkpoint 恢复时从 diagnostics 字段重建）
    _live_diagnostics: Any = PrivateAttr(default=None)

    checkpoint_version: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    @property
    def workflow_mode(self) -> str:
        return str(self.config_snapshot.get("mode", "sequential"))

    def get_recent_observations(self, n: int = 5) -> List[Observation]:
        if not self.observations:
            return []
        return list(self.observations[-n:])

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()

    def _get_live_diagnostics(self):
        """惰性初始化 RunDiagnostics；checkpoint 恢复时从 diagnostics 字段重建"""
        if self._live_diagnostics is None:
            from core.run.diagnostics import RunDiagnostics, Severity
            diag = RunDiagnostics()
            if self.diagnostics:
                for e in (self.diagnostics.get("entries") or []):
                    try:
                        sev = Severity(e.get("severity", "warning"))
                    except ValueError:
                        sev = Severity.WARNING
                    diag.record(
                        severity=sev,
                        component=e.get("component", ""),
                        message=e.get("message", ""),
                        recovery_action=e.get("recovery_action", ""),
                    )
            self._live_diagnostics = diag
        return self._live_diagnostics

    def record_degradation(
        self,
        severity,
        component: str,
        message: str,
        recovery_action: str = "",
        *,
        phase_id: str = "",
        node_id: str = "",
        code: str = "",
        fallback_used: str = "",
        source: str = "",
    ) -> None:
        """所有降级经此入口记录（不允私下 _logger.warning 后继续）。

        severity 接受 Severity 枚举或字符串（"critical"/"warning"/"info"）。
        run_id 自动从当前 RunContext 填充；phase_id/node_id/code/fallback_used 可选
        （由调用方在节点上下文传入）。
        记录后同步到可序列化字段 diagnostics（checkpoint/API 可读）。
        """
        from core.run.diagnostics import Severity
        if isinstance(severity, str):
            try:
                severity = Severity(severity)
            except ValueError:
                severity = Severity.WARNING
        diag = self._get_live_diagnostics()
        diag.record(
            severity, component, message, recovery_action,
            run_id=self.run_id,
            phase_id=phase_id,
            node_id=node_id,
            code=code,
            fallback_used=fallback_used,
            source=source,
        )
        self.diagnostics = diag.summary()
        self.touch()

    def _get_write_lock(self):
        """惰性初始化 asyncio.Lock（Pydantic v2 私有属性不可在 __init__ 中创建）"""
        if self._write_lock is None:
            import asyncio
            self._write_lock = asyncio.Lock()
        return self._write_lock

    async def set_agent_result_async(
        self,
        agent_id: str,
        result: Dict[str, Any],
        *,
        phase_id: str = "",
        iterations: int = 1,
        raw_text: str = "",
        agent_name: str = "",
    ) -> None:
        """async 版 — GraphRuntime 并行节点场景下应使用此方法"""
        lock = self._get_write_lock()
        async with lock:
            self.set_agent_result(
                agent_id, result,
                phase_id=phase_id, iterations=iterations,
                raw_text=raw_text, agent_name=agent_name,
            )

    def set_agent_result(
        self,
        agent_id: str,
        result: Dict[str, Any],
        *,
        phase_id: str = "",
        iterations: int = 1,
        raw_text: str = "",
        agent_name: str = "",
    ) -> None:
        """并行安全：通过 asyncio.Lock 保护多步写操作

        同步调用场景（非 async）退化为直接执行——asyncio.Lock 的同步语义
        在单线程 GIL 下仍保证不会被打断（async 上下文才真正 await）。
        """
        import asyncio
        lock = self._get_write_lock()
        now = datetime.now().isoformat()
        existing = self.agent_outputs.get(agent_id)
        self.agent_outputs[agent_id] = AgentOutput(
            agent_id=agent_id,
            agent_name=agent_name or agent_id,
            result=result,
            iterations=iterations,
            termination_reason="completed",
            raw_text=raw_text,
            started_at=existing.started_at if existing else now,
            finished_at=now,
        )
        if phase_id and phase_id not in self.completed_phase_ids:
            self.completed_phase_ids.append(phase_id)
        self.touch()

    def add_quality_record(
        self,
        phase_id: str,
        agent_id: str,
        violations: List[Dict[str, Any]],
        score: float = 0.0,
    ) -> None:
        passed = all(v.get("severity") != "error" for v in violations)
        self.quality_records.append(
            QualityRecord(
                phase_id=phase_id,
                agent_id=agent_id,
                passed=passed,
                score=score,
                violations=violations,
            )
        )
        self.touch()

    def add_collaboration_entry(self, entry: CollaborationThreadEntry) -> None:
        self.collaboration_thread.append(entry)
        self.touch()

    def add_observation(self, source: str, content: str, kind: str = "tool") -> None:
        self.observations.append(Observation(source=source, content=content, kind=kind))
        self.touch()

    def add_negotiation_entry(self, entry: NegotiationEntry) -> NegotiationEntry:
        if not entry.id:
            import uuid
            entry.id = f"neg_{uuid.uuid4().hex[:10]}"
        self.negotiation_ledger.append(entry)
        self.touch()
        return entry

    def confirmed_constraints(self, phase_id: str = "") -> List[NegotiationEntry]:
        """已确认且未被标记违反的约束（phase_id 为空 = 全部阶段）"""
        return [
            e for e in self.negotiation_ledger
            if e.kind == "constraint"
            and e.status in ("confirmed", "satisfied", "resolved")
            and (not phase_id or e.phase_id == phase_id)
        ]

    def open_negotiations(self) -> List[NegotiationEntry]:
        """未决问题 / 分歧（跨阶段累积）"""
        return [
            e for e in self.negotiation_ledger
            if e.status in ("open", "disputed")
        ]

    def pending_interjections(self, agent_id: str) -> List[HumanInterjection]:
        """该 Agent 尚未消费的人类插话"""
        return [
            ij for ij in self.human_interjections
            if agent_id not in ij.consumed_by
            and (not ij.target_agent or ij.target_agent == agent_id)
        ]

    def inject_phase_context(self, phase_id: str, revision_instructions: str) -> None:
        """为指定 phase 注入修改指令（replay revision mode）

        Agent 执行时会读取此上下文，在 prompt 中注入 previous_output 和修改指令。
        """
        prev_output = ""
        existing = self.agent_outputs.get(phase_id)
        if existing and existing.result:
            prev_output = str(existing.result)[:3000]

        self._revision_contexts[phase_id] = {
            "mode": "revision",
            "instructions": revision_instructions,
            "previous_output": prev_output,
        }

    def get_phase_context(self, phase_id: str) -> Optional[Dict[str, Any]]:
        """获取 phase 的 revision 上下文（如果存在）"""
        return self._revision_contexts.get(phase_id)

    def to_workflow_state(self) -> WorkflowState:
        """兼容旧 checkpoint 读取 — 新路径应直接使用 RunContext"""
        ws = WorkflowState(
            task=self.task,
            project_id=self.project_id,
            session_id=self.session_id,
            agent_outputs=dict(self.agent_outputs),
            observations=list(self.observations),
            roundtable_state=self.roundtable_state,
            config_snapshot={
                **self.config_snapshot,
                "_run_context": self.model_dump(),
                "current_phase_id": self.current_phase_id,
            },
            checkpoint_version=self.checkpoint_version,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
        mode = self.config_snapshot.get("mode", "sequential")
        if mode in ("sequential", "parallel", "roundtable"):
            ws.workflow_mode = mode
        if self.control.termination_reason:
            ws.termination_reason = self.control.termination_reason
        return ws

    @classmethod
    def from_workflow_state(cls, ws: WorkflowState) -> "RunContext":
        snap = ws.config_snapshot or {}
        if "_run_context" in snap:
            return cls.model_validate(snap["_run_context"])
        return cls(
            task=ws.task,
            project_id=ws.project_id,
            session_id=ws.session_id,
            agent_outputs=dict(ws.agent_outputs),
            observations=list(ws.observations),
            roundtable_state=ws.roundtable_state,
            config_snapshot=snap,
            checkpoint_version=ws.checkpoint_version,
            created_at=ws.created_at,
            updated_at=ws.updated_at,
            control=RunControl(termination_reason=ws.termination_reason or ""),
        )

    async def execute_subgraph(
        self,
        agent_id: str,
        task: str,
        timeout: int = 30,
    ) -> SubgraphResult:
        """在当前 RunContext 内执行同伴 Agent 的单次轻量调用（子图嵌套）

        时间到超时返回 SubgraphResult(success=False)，不抛异常。
        返回的 output 是 execute 返回的文本表示。
        递归深度通过 self._peer_depth 计数，超过 MAX_PEER_DEPTH 时拒绝执行以防死锁。
        """
        import asyncio

        MAX_PEER_DEPTH = 3
        if self._peer_depth >= MAX_PEER_DEPTH:
            return SubgraphResult(
                success=False,
                error=f"ask_peer 递归深度超限（{self._peer_depth}>={MAX_PEER_DEPTH}），已阻止以防死锁",
            )

        self._peer_depth += 1
        try:
            from core.agent.base import BaseAgent as _BaseAgent
            from core.agent.registry import get_registry as _get_registry

            registry = _get_registry()
            definition = await registry.get(agent_id)
            if not definition:
                return SubgraphResult(success=False, error=f"Agent {agent_id} not found")

            peer_agent = _BaseAgent(definition=definition)
            input_data = {
                "task": task,
                "phase_id": f"peer_query_{agent_id}",
                "run_context": self,
                "project_id": self.project_id,
                "context": {
                    "parent_run_id": self.run_id,
                    "is_peer_query": True,
                },
            }

            result = await asyncio.wait_for(
                peer_agent.execute(input_data=input_data),
                timeout=timeout,
            )

            output_text = ""
            if isinstance(result, dict):
                output_text = (
                    result.get("result")
                    or result.get("content")
                    or result.get("text")
                    or ""
                )
                if not output_text and result:
                    output_text = str(result)
            elif isinstance(result, str):
                output_text = result
            else:
                output_text = str(result)

            return SubgraphResult(success=True, output=output_text)

        except asyncio.TimeoutError:
            return SubgraphResult(success=False, error="timeout")
        except Exception as e:
            return SubgraphResult(success=False, error=str(e))
        finally:
            self._peer_depth -= 1

    def sync_from_graph_state(self, graph_values: Dict[str, Any], graph_meta: Dict[str, Any]) -> None:
        """Graph 执行后回写调度元数据"""
        self.graph = {**self.graph, **graph_meta}
        if graph_values.get("task"):
            self.task = str(graph_values["task"])
        self.touch()

    def capture_graph_resume(
        self,
        *,
        graph_def: Dict[str, Any],
        graph_state: Dict[str, Any],
        scheduler: Dict[str, Any],
        runtime_status: str,
        paused_node_id: str = "",
        graph_run_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """持久化 Graph 断点（供 review/resume 跨进程恢复）"""
        self.graph.update({
            "graph_def": graph_def,
            "graph_state": graph_state,
            "scheduler": scheduler,
            "runtime_status": runtime_status,
            "paused_node_id": paused_node_id,
            "graph_run_config": graph_run_config or {},
        })
        if runtime_status == "paused":
            self.control.pending_human = True
            self.control.pause_reason = "review"
        self.touch()
