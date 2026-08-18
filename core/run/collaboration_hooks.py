"""Phase 切换协作钩子 — delegate / status / handoff / broadcast (D6)"""

from __future__ import annotations
from typing import Any, List, Optional, TYPE_CHECKING

from core.run.collaboration_protocol import (
    CollaborationMessage,
    CollaborationProtocol,
    workspace_ref_label,
)
from core.run.phase_spec import PhaseSpec
from core.run.run_context import RunContext
from core.run.workspace_refs import resolve_workspace_refs
from core.logging import get_logger

_logger = get_logger("run.collaboration_hooks")

if TYPE_CHECKING:
    from core.graph.types import WorkflowGraph


async def on_parallel_fork(
    ctx: RunContext,
    *,
    target_node_ids: List[str],
    graph: Optional["WorkflowGraph"] = None,
    fork_node_id: str = "",
    reason: str = "fork",
) -> None:
    """并行 / Fork 场景显式 broadcast（D6）"""
    agents: List[str] = []
    phase_ids: List[str] = []
    for nid in target_node_ids:
        if graph:
            node = graph.get_node(nid)
            if node:
                if node.agent_id:
                    agents.append(node.agent_id)
                pid = str(node.metadata.get("phase_id") or nid)
                phase_ids.append(pid)
                continue
        agents.append(nid)

    label = ", ".join(agents or target_node_ids)
    if reason == "fork":
        summary = f"分叉启动并行分支: {label}"
    else:
        summary = f"并行执行: {label}"

    phase_id = ""
    if phase_ids and len(set(phase_ids)) == 1:
        phase_id = phase_ids[0]

    CollaborationProtocol.broadcast(
        ctx,
        fork_node_id or "system",
        summary,
        phase_id=phase_id,
    )


async def on_graph_status(
    ctx: RunContext,
    *,
    node_id: str,
    summary: str,
    phase_id: str = "",
    agent_id: str = "system",
) -> None:
    """图节点级 status 广播（D6）"""
    CollaborationProtocol.status(ctx, agent_id or node_id, summary, phase_id=phase_id or node_id)


async def on_phase_enter(
    ctx: RunContext,
    spec: PhaseSpec,
    agent_id: str,
    *,
    agent_name: str = "",
) -> None:
    """Phase 入口：status + 可选 delegate + handoff 接收确认"""
    CollaborationProtocol.status(
        ctx,
        agent_id,
        f"进入阶段 {spec.label or spec.id}",
        phase_id=spec.id,
    )

    # 圆桌模式：emit 轮次事件（前端按轮次渲染讨论）
    if spec.collaboration == "deliberative":
        try:
            from core.events import event_bus
            round_num = ctx.config_snapshot.get("deliberative_round", 0) + 1
            ctx.config_snapshot["deliberative_round"] = round_num
            event_bus.broadcast("roundtable_round", {
                "round": round_num,
                "phase_id": spec.id,
                "agent_id": agent_id,
                "agent_name": agent_name or agent_id,
            }, project_id=ctx.project_id)
        except Exception as e:
            _logger.warning("roundtable 轮次 SSE 广播失败: {}", e)

    if spec.dialogue_policy == "none":
        return

    # 收集所有待确认的交接（定向 + 广播），去重 by from_agent
    pending_handoffs = []
    seen_from = set()
    for entry in reversed(ctx.collaboration_thread):
        if entry.protocol != "handoff":
            continue
        if entry.from_agent == agent_id:
            continue  # 不确认自己发的
        is_directed = entry.to_agent == agent_id
        is_broadcast = entry.to_agent == "*"
        if not (is_directed or is_broadcast):
            continue
        if entry.from_agent in seen_from:
            continue
        seen_from.add(entry.from_agent)
        pending_handoffs.append(entry)

    for pending in pending_handoffs[:5]:  # 最多确认 5 条
        await CollaborationProtocol.acknowledge_handoff(
            ctx,
            agent_id,
            pending.from_agent,
            phase_id=spec.id,
            agent_name=agent_name or agent_id,
            handoff_phase=pending.phase_id,
            handoff_summary=pending.summary,
        )


async def on_phase_complete(
    ctx: RunContext,
    spec: PhaseSpec,
    agent_id: str,
    result: Any,
    *,
    to_agent: str = "",
    to_name: str = "",
    from_name: str = "",
) -> None:
    """Phase 完成：兜底物化 + status + handoff（含 workspace ref）"""
    from core.communication.handoff import deliverable_refs_from_files
    from core.vault.project_artifact_service import (
        ProjectArtifactService,
        expected_artifacts_for_phase,
    )

    expected = spec.metadata.get("expected_artifacts")
    if not expected:
        expected = expected_artifacts_for_phase(spec.metadata, spec.id)
    mat_refs = await ProjectArtifactService.materialize_phase_output(
        ctx.project_id,
        agent_id,
        spec.id,
        result,
        expected_files=expected if expected else None,
    )
    refs = deliverable_refs_from_files(ctx.project_id, agent_id, spec.id, list(expected or []))
    if not refs:
        refs = await resolve_workspace_refs(ctx.project_id, agent_id, spec.id, result)
    if mat_refs:
        seen = {r.path or r.summary for r in refs}
        for r in mat_refs:
            key = r.path or r.summary
            if key and key not in seen:
                refs.append(r)
                seen.add(key)

    missing = []
    for fname in expected or []:
        try:
            if not ProjectArtifactService.resolve_project_path(ctx.project_id, str(fname)).is_file():
                missing.append(str(fname))
        except ValueError:
            missing.append(str(fname))

    CollaborationProtocol.status(
        ctx,
        agent_id,
        f"完成阶段 {spec.label or spec.id}"
        + (f"（缺少: {', '.join(missing)}）" if missing else ""),
        phase_id=spec.id,
    )
    # 构建完成摘要（含产出文件名、内容预览）
    from core.communication.handoff import build_handoff_payload
    payload = build_handoff_payload(
        from_agent=agent_id,
        to_agent=to_agent or "*",
        result=result,
        from_name=from_name or agent_id,
        to_name=to_name or to_agent or "所有下游",
        phase=spec.label or spec.id,
        artifact_keys=[workspace_ref_label(r) for r in refs if workspace_ref_label(r)],
    )
    handoff_body = payload.to_context_text()

    if missing:
        # 有缺失产物时仍记录 handoff（标注缺失），不阻塞下游
        payload.summary += f"（⚠️ 缺少: {', '.join(missing)}）"
        handoff_body += f"\n\n⚠️ 注意：以下期望产物未生成: {', '.join(missing)}"

    if to_agent and to_agent != agent_id:
        # 有明确下游 → 定向 handoff
        await CollaborationProtocol.handoff(
            ctx,
            agent_id,
            to_agent,
            result,
            phase_id=spec.id,
            from_name=from_name or agent_id,
            to_name=to_name or to_agent,
            workspace_refs=refs,
        )
    else:
        # 并行/末端 → broadcast handoff（所有下游可见）
        CollaborationProtocol.record(
            ctx,
            CollaborationMessage(
                protocol="handoff",
                from_agent=agent_id,
                to_agent="*",
                phase_id=spec.id,
                summary=payload.summary,
                workspace_refs=refs or [],
                metadata={
                    **payload.to_metadata(),
                    "content": handoff_body,
                    "result_preview": str(result)[:500] if result else "",
                },
            ),
        )
        # 同时写入 ConversationBus（SSE 广播到前端 Agent 对话面板）
        if ctx.project_id:
            try:
                from core.communication.bus import ConversationBus
                bus = ConversationBus(ctx.project_id)
                await bus.send_handoff_payload(payload)
            except Exception as e:
                _logger.warning("handoff payload 投递 Bus 失败: {}", e)

    # --- Phase 2 新增：长内容记忆维护 ---
    if getattr(ctx, 'skeleton', None) and getattr(ctx, 'current_unit', None):
        try:
            from core.memory.curator import MemoryCurator
            from tools.llm_client import LLMClient
            curator = MemoryCurator(llm_client=LLMClient())
            ctx_dict = ctx.model_dump()
            updated = await curator.process_unit_completion(
                run_context_dict=ctx_dict,
                unit_number=ctx.current_unit,
                agent_output=result if isinstance(result, dict) else {"raw_text": str(result)},
            )
            ctx.layered_memory = updated.get("layered_memory")
            ctx.continuity_state = updated.get("continuity_state")
            ctx.current_unit = updated.get("current_unit", ctx.current_unit)
            if updated.get("skeleton"):
                ctx.skeleton = updated.get("skeleton")
            if updated.get("atmosphere_state") is not None:
                ctx.atmosphere_state = updated.get("atmosphere_state")
            if updated.get("character_profiles") is not None:
                ctx.character_profiles = updated.get("character_profiles")

            # --- 修正2：emit SSE（continuity/memory/atmosphere/character）---
            try:
                from core.events import broadcast_event
                await broadcast_event(ctx.project_id, "continuity_updated", {
                    "continuity_state": ctx.continuity_state,
                    "current_unit": ctx.current_unit,
                })
                await broadcast_event(ctx.project_id, "memory_layer_updated", {
                    "layered_memory": ctx.layered_memory,
                })
                if ctx.atmosphere_state:
                    await broadcast_event(ctx.project_id, "atmosphere_updated", {
                        "atmosphere_state": ctx.atmosphere_state,
                    })
                if ctx.character_profiles:
                    await broadcast_event(ctx.project_id, "character_profiles_updated", {
                        "character_profiles": ctx.character_profiles,
                    })
            except Exception as e:
                _logger.warning("角色档案 SSE 广播失败: {}", e)

            # --- 修正4：骨架调整通知 ---
            adjusted_info = updated.get("_skeleton_just_adjusted")
            if adjusted_info:
                try:
                    from core.events import broadcast_event
                    await broadcast_event(ctx.project_id, "skeleton_adjusted", {
                        **adjusted_info,
                        "skeleton": ctx.skeleton,
                    })
                except Exception as e:
                    _logger.warning("骨架调整 SSE 广播失败: {}", e)
        except Exception as e:
            import logging
            logging.getLogger("collaboration_hooks").warning(
                "MemoryCurator failed for unit %s: %s", ctx.current_unit, e
            )
