"""Checkpoint 迁移 — 旧 WorkflowState → RunContext (D2)"""

from __future__ import annotations
from typing import Any, Dict, Tuple

from core.agent.state import WorkflowState
from core.run.run_context import RunContext, RunControl

_GRAPH_KEYS = (
    "graph_def",
    "graph_state",
    "scheduler",
    "runtime_status",
    "paused_node_id",
    "graph_run_config",
    "run_id",
)


def needs_run_context_migration(ws: WorkflowState) -> bool:
    snap = ws.config_snapshot or {}
    return "_run_context" not in snap


def build_run_context_from_legacy(ws: WorkflowState, project_id: str) -> RunContext:
    """从旧 WorkflowState 构造 RunContext 并补全常见 legacy 字段"""
    snap: Dict[str, Any] = dict(ws.config_snapshot or {})
    ctx = RunContext.from_workflow_state(ws)
    ctx.project_id = ctx.project_id or project_id

    if snap.get("current_phase_id"):
        ctx.current_phase_id = str(snap["current_phase_id"])
    if snap.get("phase_specs"):
        ctx.phase_specs = list(snap["phase_specs"])
    elif snap.get("agent_ids"):
        ctx.phase_specs = [
            {"id": aid, "agent_id": aid, "label": aid}
            for aid in snap["agent_ids"]
        ]

    completed = snap.get("completed_phases")
    if completed:
        ctx.completed_phase_ids = list(completed)
    elif not ctx.completed_phase_ids:
        ctx.completed_phase_ids = [
            aid
            for aid, out in ws.agent_outputs.items()
            if out.termination_reason == "completed"
        ]

    graph_meta = {k: snap[k] for k in _GRAPH_KEYS if k in snap}
    if graph_meta:
        ctx.graph.update(graph_meta)
        if graph_meta.get("run_id") and not ctx.run_id:
            ctx.run_id = str(graph_meta["run_id"])

    if not ctx.run_id:
        ctx.run_id = str(snap.get("run_id") or project_id[:8])

    control = RunControl(termination_reason=ws.termination_reason or "")
    if snap.get("pause_reason"):
        control.pause_reason = str(snap["pause_reason"])
        control.pending_human = bool(snap.get("pending_human", True))
    pending_decision = snap.get("pending_decision_id") or snap.get("roundtable_decision_id")
    if pending_decision is not None:
        try:
            control.pending_decision_id = int(pending_decision)
        except (TypeError, ValueError):
            pass
    ctx.control = control

    if ws.workflow_mode and "mode" not in ctx.config_snapshot:
        ctx.config_snapshot["mode"] = ws.workflow_mode

    ctx.touch()
    return ctx


def migrate_checkpoint_json(json_str: str, project_id: str) -> Tuple[str, bool]:
    """返回 (新 JSON, 是否已迁移)"""
    ws = WorkflowState.load_json(json_str)
    if not needs_run_context_migration(ws):
        return json_str, False
    ctx = build_run_context_from_legacy(ws, project_id)
    return ctx.to_workflow_state().dump_json(), True
