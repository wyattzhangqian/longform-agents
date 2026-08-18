"""工作流运行恢复 — 检测 API 热重载/进程重启后的僵尸 running 状态"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.events import event_bus
from core.logging import get_logger
from core.project.manager import ProjectManager
from core.run.graph_registry import is_execution_alive

_logger = get_logger("run.recovery")

ORPHAN_REASON = "worker_lost"


def is_orphaned_status(status: str) -> bool:
    return status in ("running", "queued")


async def detect_orphaned_run(project_id: str, db_status: Optional[str] = None) -> bool:
    """DB 标记为 running/queued，但进程内无活跃执行。"""
    status = db_status
    if status is None:
        project = await ProjectManager.get(project_id)
        if not project:
            return False
        status = project.status
    if not is_orphaned_status(status):
        return False
    return not is_execution_alive(project_id)


async def reconcile_orphaned_run(project_id: str) -> Optional[Dict[str, Any]]:
    """将僵尸运行标记为 failed，并广播 SSE。返回 orphan 元信息；非 orphan 时返回 None。"""
    project = await ProjectManager.get(project_id)
    if not project or not await detect_orphaned_run(project_id, project.status):
        return None

    previous = project.status
    # 注意：ProjectConfig.model_dump() 会把 extra 展平到顶层（兼容旧 API），
    # 必须从 project.config.extra 读写，且同步清理展平后的顶层键
    config = project.config.model_dump()
    extra = dict(project.config.extra)
    extra["recover_pending"] = True
    extra["orphan_reason"] = ORPHAN_REASON
    await ProjectManager.update(
        project_id,
        {"status": "failed", "config": {**config, "extra": extra}},
    )
    event_bus.broadcast(
        event="agent_state",
        data={
            "state": "failed",
            "project_id": project_id,
            "orphaned": True,
            "orphan_reason": ORPHAN_REASON,
            "message": "工作流进程已中断（常见于 API 热重载），可自动恢复",
        },
        project_id=project_id,
    )
    _logger.warning(
        "僵尸运行已标记 failed: project=%s previous=%s reason=%s",
        project_id,
        previous,
        ORPHAN_REASON,
    )
    return {
        "orphaned": True,
        "orphan_reason": ORPHAN_REASON,
        "previous_status": previous,
        "recoverable": True,
    }


def is_recover_pending(project) -> bool:
    extra = project.config.extra if isinstance(project.config.extra, dict) else {}
    return bool(extra.get("recover_pending"))


async def clear_recover_pending(project_id: str) -> None:
    project = await ProjectManager.get(project_id)
    if not project:
        return
    config = project.config.model_dump()
    extra = dict(project.config.extra)
    removed_pending = extra.pop("recover_pending", None)
    removed_reason = extra.pop("orphan_reason", None)
    if not removed_pending and not removed_reason:
        return
    # model_dump() 展平了 extra，顶层副本也要清掉，否则重载时会被合并回 extra
    config.pop("recover_pending", None)
    config.pop("orphan_reason", None)

    # 直接写 config 列：ProjectManager.update 是合并语义，无法删除已有键
    import json
    from core.storage.database import get_db

    db = await get_db()
    await db.execute(
        "UPDATE projects SET config = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        (json.dumps({**config, "extra": extra}, ensure_ascii=False), project_id),
    )
    await db.commit()


async def mark_recover_pending(project_id: str) -> None:
    """启动失败后保留可恢复标记，刷新时可再次自动 recover。"""
    project = await ProjectManager.get(project_id)
    if not project:
        return
    config = project.config.model_dump()
    extra = dict(project.config.extra)
    extra["recover_pending"] = True
    extra["orphan_reason"] = ORPHAN_REASON
    await ProjectManager.update(project_id, {"config": {**config, "extra": extra}})


async def reconcile_all_orphaned_runs() -> int:
    """服务启动时批量 reconcile（reload 后内存 Graph 全部丢失）。"""
    count = 0
    for st in ("running", "queued"):
        projects, _ = await ProjectManager.list(status=st, limit=500)
        for project in projects:
            if await reconcile_orphaned_run(project.id):
                count += 1
    if count:
        _logger.info("启动时已 reconcile %d 个僵尸运行", count)
    return count


def project_to_run_payload(project) -> Dict[str, Any]:
    """从项目记录构造 /api/run 请求体（用于 recover）。"""
    cfg = project.config
    agent_ids = [
        pa.agent_id for pa in sorted(project.agents, key=lambda x: x.join_order)
    ]
    extra = cfg.extra if isinstance(cfg.extra, dict) else {}
    return {
        "task": cfg.task_description or project.name,
        "project_id": project.id,
        "project_name": project.name,
        "mode": project.mode or "sequential",
        "agent_ids": agent_ids,
        "template_id": project.template_id or "",
        "review_mode": bool(cfg.review_mode),
        "use_tools": extra.get("use_tools", True),
        "use_graph_engine": True,
        "auto_plan": False,
    }
