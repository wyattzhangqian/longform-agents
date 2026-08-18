"""workflow_events — 轻量事件持久化（debug / replay，非 CQRS）"""

from __future__ import annotations
import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logging import get_logger
from core.observability.trace import get_trace_id
from core.storage.database import get_db, get_read_db

_logger = get_logger("workflow_events")

# 高频事件可跳过持久化
# P0-4: agent_token_stream / agent_thinking 按 token 逐块广播，对 replay/debug
# 无价值，却占 workflow_events 约 98% 行数（实测 22.5 万 + 7 万 行，无保留策略）。
_SKIP_EVENTS = frozenset({
    "progress", "log", "connected",
    "agent_token_stream", "agent_thinking",
})

# P0-4: 全局限定保留 —— 超过上限触发裁剪（保留最新里程碑事件，控制 DB 无界增长）
_RETENTION_MAX_EVENTS = 20000
_RETENTION_PRUNE_EVERY = 100  # 每 N 次写入触发一次裁剪判断（避免每次 COUNT）

_pending_writes: set[asyncio.Task] = set()
_write_counter = 0


class WorkflowEventStore:
    """工作流事件存储 — 异步写入 SQLite workflow_events 表"""

    @staticmethod
    async def append(
        project_id: str,
        event_type: str,
        data: dict,
        trace_id: Optional[str] = None,
    ) -> None:
        global _write_counter
        if not project_id or event_type in _SKIP_EVENTS:
            return
        try:
            db = await get_db()
            await db.execute(
                """INSERT INTO workflow_events
                   (project_id, trace_id, event_type, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    project_id,
                    trace_id or get_trace_id() or "",
                    event_type,
                    json.dumps(data, ensure_ascii=False, default=str),
                    datetime.now().isoformat(),
                ),
            )
            await db.commit()

            # P0-4: 轻量保留策略 —— 周期性裁剪超限旧事件（保留最新里程碑事件）
            _write_counter += 1
            if _write_counter % _RETENTION_PRUNE_EVERY == 0:
                await WorkflowEventStore.prune(max_events=_RETENTION_MAX_EVENTS)
        except Exception as e:
            _logger.debug("workflow_event 写入失败: %s", e)

    @staticmethod
    async def prune(max_events: int = _RETENTION_MAX_EVENTS) -> int:
        """P0-4: 裁剪超过 max_events 的旧事件，保留最新 N 条。返回删除行数。

        按全局 id 排序裁剪（保留最新里程碑事件）。无保留框架时的就地方案。
        """
        try:
            db = await get_db()
            cursor = await db.execute("SELECT COUNT(*) AS cnt FROM workflow_events")
            row = await cursor.fetchone()
            if not row or int(row["cnt"] or 0) <= max_events:
                return 0
            cursor = await db.execute(
                "DELETE FROM workflow_events WHERE id <= "
                "(SELECT id FROM workflow_events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (max_events,),
            )
            await db.commit()
            deleted = getattr(cursor, "rowcount", 0) or 0
            if deleted:
                _logger.info("workflow_events 保留策略：裁剪 {} 行（当前 {} 行）", deleted, row["cnt"])
            return deleted
        except Exception as e:
            _logger.debug("workflow_events 裁剪失败: %s", e)
            return 0

    @staticmethod
    async def flush_pending(timeout: float = 5.0) -> None:
        """等待 append_background 触发的写入完成（close_db 前调用）"""
        pending = list(_pending_writes)
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _logger.debug("workflow_event flush 超时，剩余 %d 个任务", len(_pending_writes))

    @staticmethod
    def append_background(
        project_id: str,
        event_type: str,
        data: dict,
        trace_id: Optional[str] = None,
    ) -> None:
        """非阻塞写入（从 sync EventBus 调用）"""
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                WorkflowEventStore.append(project_id, event_type, data, trace_id)
            )
            _pending_writes.add(task)
            task.add_done_callback(_pending_writes.discard)
        except RuntimeError:
            pass

    @staticmethod
    async def list_events(
        project_id: str,
        event_type: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        db = await get_read_db()
        sql = "SELECT * FROM workflow_events WHERE project_id = ?"
        count_sql = "SELECT COUNT(*) as cnt FROM workflow_events WHERE project_id = ?"
        params: list = [project_id]

        if event_type:
            sql += " AND event_type = ?"
            count_sql += " AND event_type = ?"
            params.append(event_type)
        if trace_id:
            sql += " AND trace_id = ?"
            count_sql += " AND trace_id = ?"
            params.append(trace_id)

        cursor = await db.execute(count_sql, params)
        total = (await cursor.fetchone())["cnt"]

        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        cursor = await db.execute(sql, params + [limit, offset])
        rows = await cursor.fetchall()
        events = []
        for r in rows:
            raw_payload = r["payload"] or "{}"
            try:
                data = json.loads(raw_payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                data = {"_parse_error": True, "raw": str(raw_payload)[:500]}
            events.append({
                "id": r["id"],
                "project_id": r["project_id"],
                "trace_id": r["trace_id"],
                "event_type": r["event_type"],
                "data": data,
                "created_at": r["created_at"],
            })
        return events, total
