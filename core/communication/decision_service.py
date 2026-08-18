"""决策卡加载、持久化与恢复 — Graph resume 路径 (D1 人机统一)"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional, Tuple

from core.agent.state_manager import StateManager
from core.communication.decision_defaults import (
    DEFAULT_DECISION_QUESTION,
    default_decision_option_dicts,
    ensure_decision_options,
)
from core.events import event_bus
from core.project.manager import ProjectManager
from core.storage.database import get_db


async def load_pending_decision_raw(project_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT * FROM decisions
           WHERE project_id = ? AND resolved_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (project_id,),
    )
    row = await cursor.fetchone()
    if row:
        decision = dict(row)
        raw_opts = json.loads(decision.get("options") or "[]")
        opts = [o.model_dump() for o in ensure_decision_options(raw_opts)]
        if not raw_opts:
            await db.execute(
                "UPDATE decisions SET options = ?, question = COALESCE(NULLIF(question, ''), ?) WHERE id = ?",
                (json.dumps(opts, ensure_ascii=False), DEFAULT_DECISION_QUESTION, decision["id"]),
            )
            await db.commit()
            if not (decision.get("question") or "").strip():
                decision["question"] = DEFAULT_DECISION_QUESTION
        decision["options"] = opts
        return decision, "database"

    ctx = await StateManager.load_run_context(project_id)
    if ctx and ctx.control.pending_decision_id:
        return {
            "id": ctx.control.pending_decision_id,
            "project_id": project_id,
            "question": DEFAULT_DECISION_QUESTION,
            "options": default_decision_option_dicts(),
        }, "run_context"

    project = await ProjectManager.get(project_id)
    if project and project.status in ("paused", "paused_decision"):
        return {
            "id": 0,
            "project_id": project_id,
            "question": DEFAULT_DECISION_QUESTION,
            "options": default_decision_option_dicts(),
        }, "inferred"

    return None, None


async def _patch_run_context_decision_id(project_id: str, decision_id: int) -> None:
    ctx = await StateManager.load_run_context(project_id)
    if not ctx:
        return
    ctx.control.pending_decision_id = decision_id
    ctx.control.pause_reason = "decision_required"
    await StateManager.save_run_context(project_id, ctx)


async def persist_pending_decision(
    project_id: str,
    *,
    question: str,
    options: list,
    phase: str = "roundtable",
    escalated_by: str = "system",
) -> int:
    db = await get_db()
    opts = [o.model_dump() if hasattr(o, "model_dump") else o for o in ensure_decision_options(options)]
    cursor = await db.execute(
        """INSERT INTO decisions (project_id, phase, question, options, escalated_by)
           VALUES (?, ?, ?, ?, ?)""",
        (
            project_id,
            phase,
            question or DEFAULT_DECISION_QUESTION,
            json.dumps(opts, ensure_ascii=False),
            escalated_by,
        ),
    )
    await db.commit()
    return int(cursor.lastrowid or 0)


async def ensure_decision_persisted(
    project_id: str,
    decision: Dict[str, Any],
) -> Dict[str, Any]:
    did = int(decision.get("id") or 0)
    if did > 0:
        db = await get_db()
        cur = await db.execute(
            "SELECT id FROM decisions WHERE id = ? AND project_id = ? AND resolved_at IS NULL",
            (did, project_id),
        )
        if await cur.fetchone():
            return decision

    new_id = await persist_pending_decision(
        project_id,
        question=str(decision.get("question") or DEFAULT_DECISION_QUESTION),
        options=decision.get("options") or default_decision_option_dicts(),
        phase=str(decision.get("phase") or "roundtable"),
        escalated_by=str(decision.get("escalated_by") or "system"),
    )
    if new_id:
        await _patch_run_context_decision_id(project_id, new_id)
    return {**decision, "id": new_id}


async def get_pending_decision_enriched(project_id: str) -> Dict[str, Any]:
    decision, source = await load_pending_decision_raw(project_id)
    if not decision:
        return {"decision": None, "source": None}
    decision = await ensure_decision_persisted(project_id, decision)
    return {"decision": decision, "source": source}


async def resolve_decision_and_resume(decision_id: int, option: str) -> str:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,))
    row = await cursor.fetchone()
    if not row:
        raise ValueError(f"决策不存在: {decision_id}")
    if row["resolved_at"]:
        raise ValueError("决策已解决")

    decision = dict(row)
    options = json.loads(decision.get("options") or "[]")
    chosen_content = option
    for opt in options:
        if opt.get("id") == option:
            chosen_content = opt.get("agent_view") or opt.get("label") or option
            break

    await db.execute(
        "UPDATE decisions SET chosen_option = ?, decided_by = 'user', resolved_at = datetime('now') WHERE id = ?",
        (option, decision_id),
    )
    await db.commit()

    project_id = decision["project_id"]

    async def _resume_graph():
        from core.logging import get_logger
        from core.run.graph_registry import ensure_collaboration_graph, remove_collaboration_graph

        log = get_logger("decision_service")
        from core.run.run_lock import run_lock
        try:
            collab = await ensure_collaboration_graph(project_id)
            collab.run_context.control.pending_decision_id = None
            collab.run_context.control.pause_reason = ""
            if collab.runtime and collab.runtime.state:
                collab.runtime.state.set("_decision_choice", option)
                collab.runtime.state.set("_decision_conclusion", chosen_content)
            async with run_lock(project_id):
                _, status = await collab.resume(user_input={
                    "decision": option,
                    "conclusion": chosen_content,
                })
            await ProjectManager.update_status(project_id, status)
            if status != "paused":
                remove_collaboration_graph(project_id)
            event_bus.broadcast(
                event="agent_state",
                data={"state": status, "project_id": project_id},
                project_id=project_id,
            )
        except Exception as e:
            log.exception("Graph 决策恢复失败: %s", e)
            await ProjectManager.update_status(project_id, "failed")

    from core.task_tracker import create_task as _tracked_task
    _tracked_task(_resume_graph(), name=f"resume_graph_{project_id}")

    event_bus.broadcast(
        event="decision_resolved",
        data={"decision_id": decision_id, "chosen_option": option, "project_id": project_id},
        project_id=project_id,
    )
    return project_id
