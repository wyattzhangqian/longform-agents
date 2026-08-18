"""协作协议 — handoff / delegate / broadcast / status (D6)

Bus 只承载协议消息；路由决策由 CollaborationGraph 的 Router 节点负责。
"""

from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional
from pathlib import Path
from pydantic import BaseModel, Field

from core.communication.handoff import HandoffPayload, build_handoff_payload
from core.run.run_context import CollaborationThreadEntry, RunContext, WorkspaceRef
from core.logging import get_logger

_logger = get_logger("run.collaboration_protocol")

ProtocolType = Literal["handoff", "broadcast", "status", "ack", "dialogue"]


def workspace_ref_label(ref: WorkspaceRef) -> str:
    """Handoff / 产物 Tab 展示用的人类可读文件名。"""
    path = (ref.path or "").replace("\\", "/")
    if path:
        name = Path(path).name
        if name:
            return name
    summary = (ref.summary or "").strip()
    if summary and not summary.startswith("art_") and not summary.startswith("mat_"):
        return summary[:120]
    return ref.artifact_id or path or summary


class CollaborationMessage(BaseModel):
    protocol: ProtocolType
    from_agent: str
    to_agent: str = ""
    phase_id: str = ""
    summary: str = ""
    workspace_refs: List[WorkspaceRef] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CollaborationProtocol:
    """RunContext 与 ConversationBus 之间的协作层"""

    @staticmethod
    def record(ctx: RunContext, msg: CollaborationMessage) -> CollaborationThreadEntry:
        entry = CollaborationThreadEntry(
            protocol=msg.protocol,
            from_agent=msg.from_agent,
            to_agent=msg.to_agent,
            phase_id=msg.phase_id,
            summary=msg.summary,
            workspace_refs=[workspace_ref_label(r) for r in msg.workspace_refs if workspace_ref_label(r)],
            metadata=msg.metadata,
        )
        ctx.add_collaboration_entry(entry)
        for ref in msg.workspace_refs:
            if ref.artifact_id or ref.path:
                ctx.workspace_refs.append(ref)
        if ctx.project_id:
            try:
                from core.events import event_bus
                event_bus.broadcast(
                    event="collaboration",
                    data={
                        "protocol": entry.protocol,
                        "from_agent": entry.from_agent,
                        "to_agent": entry.to_agent,
                        "phase_id": entry.phase_id,
                        "summary": entry.summary,
                        "content": (msg.metadata or {}).get("content") or entry.summary,
                        "workspace_refs": entry.workspace_refs,
                        "metadata": entry.metadata,
                        "created_at": entry.created_at,
                    },
                    project_id=ctx.project_id,
                )
            except Exception as e:
                _logger.warning("协商广播失败: {}", e)
        return entry

    @staticmethod
    async def handoff(
        ctx: RunContext,
        from_agent: str,
        to_agent: str,
        result: Any,
        *,
        phase_id: str = "",
        from_name: str = "",
        to_name: str = "",
        workspace_refs: Optional[List[WorkspaceRef]] = None,
    ) -> HandoffPayload:
        payload = build_handoff_payload(
            from_agent=from_agent,
            to_agent=to_agent,
            result=result,
            from_name=from_name,
            to_name=to_name,
            phase=phase_id,
            artifact_keys=[
                workspace_ref_label(r) for r in (workspace_refs or []) if workspace_ref_label(r)
            ],
        )
        handoff_body = payload.to_context_text()
        CollaborationProtocol.record(
            ctx,
            CollaborationMessage(
                protocol="handoff",
                from_agent=from_agent,
                to_agent=to_agent,
                phase_id=phase_id,
                summary=payload.summary,
                workspace_refs=workspace_refs or [],
                metadata={**payload.to_metadata(), "content": handoff_body},
            ),
        )
        if ctx.project_id:
            try:
                from core.communication.bus import ConversationBus

                bus = ConversationBus(ctx.project_id)
                await bus.send_handoff_payload(payload)
            except Exception as e:
                _logger.warning("handoff payload 投递 Bus 失败: {}", e)
        return payload

    @staticmethod
    async def acknowledge_handoff(
        ctx: RunContext,
        receiver_id: str,
        sender_id: str,
        *,
        phase_id: str = "",
        agent_name: str = "",
        handoff_phase: str = "",
        handoff_summary: str = "",
    ) -> None:
        """接收方确认已收到上游交接（dialogue_policy != none 时触发）"""
        name = agent_name or receiver_id
        # 上游发送方展示名：优先 metadata / registry，避免正文里露出 agent_id
        sender_name = sender_id
        try:
            from core.agent.registry import get_registry
            sender_def = await get_registry().get(sender_id)
            if sender_def and getattr(sender_def, "name", None):
                sender_name = sender_def.name
        except Exception:
            pass
        phase_label = phase_id
        handoff_label = handoff_phase
        try:
            from core.agent.registry import get_registry
            reg = get_registry()
            if phase_id:
                pdef = await reg.get(phase_id)
                if pdef and getattr(pdef, "name", None):
                    phase_label = pdef.name
            if handoff_phase:
                hdef = await reg.get(handoff_phase)
                if hdef and getattr(hdef, "name", None):
                    handoff_label = hdef.name
        except Exception:
            pass
        body = (
            f"✅ **{name}** 已确认接收 **{sender_name}** 的交接"
            + (f"（上游阶段 `{handoff_label}`）" if handoff_label else "")
            + f"，开始执行 **{phase_label or '本阶段'}**。"
        )
        if handoff_summary:
            body += f"\n\n> {handoff_summary[:240]}"
        CollaborationProtocol.record(
            ctx,
            CollaborationMessage(
                protocol="ack",
                from_agent=receiver_id,
                to_agent=sender_id,
                phase_id=phase_id,
                summary=f"{name} 已确认接收交接",
                metadata={
                    "content": body,
                    "protocol": "handoff_ack",
                    "handoff_phase": handoff_phase,
                },
            ),
        )
        if ctx.project_id:
            try:
                from core.communication.bus import ConversationBus

                bus = ConversationBus(ctx.project_id)
                await bus.send_answer(
                    receiver_id,
                    sender_id,
                    body,
                    metadata={
                        "protocol": "handoff_ack",
                        "phase_id": phase_id,
                        "handoff_phase": handoff_phase,
                    },
                )
            except Exception as e:
                _logger.warning("handoff_ack 广播失败: {}", e)

    @staticmethod
    async def dialogue_turn(
        ctx: RunContext,
        from_agent: str,
        to_agent: str,
        role: str,
        content: str,
        *,
        phase_id: str = "",
        round_num: int = 1,
    ) -> None:
        """shared_thread 问答轮次（question / answer）"""
        CollaborationProtocol.record(
            ctx,
            CollaborationMessage(
                protocol="dialogue",
                from_agent=from_agent,
                to_agent=to_agent,
                phase_id=phase_id,
                summary=content[:120],
                metadata={
                    "protocol": "shared_thread",
                    "dialogue_role": role,
                    "content": content,
                    "round": round_num,
                },
            ),
        )

    @staticmethod
    def status(
        ctx: RunContext,
        agent_id: str,
        summary: str,
        *,
        phase_id: str = "",
    ) -> None:
        CollaborationProtocol.record(
            ctx,
            CollaborationMessage(
                protocol="status",
                from_agent=agent_id,
                phase_id=phase_id,
                summary=summary,
            ),
        )

    @staticmethod
    def broadcast(
        ctx: RunContext,
        agent_id: str,
        summary: str,
        *,
        phase_id: str = "",
    ) -> None:
        CollaborationProtocol.record(
            ctx,
            CollaborationMessage(
                protocol="broadcast",
                from_agent=agent_id,
                phase_id=phase_id,
                summary=summary,
            ),
        )

    @staticmethod
    async def emit_to_bus(bus, msg: CollaborationMessage) -> None:
        """持久化到 ConversationBus（可观测，非业务真相）"""
        if msg.protocol == "handoff":
            await bus.send_handoff(
                msg.from_agent,
                msg.to_agent,
                msg.summary,
                metadata={**msg.metadata, "protocol": "handoff", "phase_id": msg.phase_id},
            )
        elif msg.protocol == "broadcast":
            await bus.send_status(f"📢 {msg.from_agent}: {msg.summary}")
        elif msg.protocol == "ack":
            await bus.send_answer(
                msg.from_agent,
                msg.to_agent,
                str((msg.metadata or {}).get("content") or msg.summary),
                metadata={**(msg.metadata or {}), "protocol": "handoff_ack", "phase_id": msg.phase_id},
            )
        else:
            await bus.send_status(msg.summary)
