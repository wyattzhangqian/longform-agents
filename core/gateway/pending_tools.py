"""待审批工具调用暂存 — 审批闭环（P0：落库持久化，重启不丢）

设计：
  - 内存 dict 仍是同步读写的主路径（调用方多为同步上下文）
  - 每次 register/pop 以 write-behind 方式尽力写入 pending_tool_approvals 表
  - 服务启动时 load_pending_from_db() 把未决审批恢复进内存
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

_logger = __import__("logging").getLogger(__name__)

_pending: Dict[str, Dict[str, Any]] = {}


# ============================================================
# 持久化（write-behind，失败不致命）
# ============================================================

def _schedule(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
        # 强引用跟踪，防 write-behind 持久化任务被 GC 静默中止（item8 同类）
        from core.task_tracker import create_task
        create_task(coro, name="pending_tool_persist")
    except RuntimeError:
        # 无运行中事件循环（如同步测试），跳过持久化
        coro.close()


async def _persist_register(tool_call_id: str, record: Dict[str, Any]) -> None:
    try:
        from core.storage.database import get_db
        db = await get_db()
        await db.execute(
            """INSERT OR REPLACE INTO pending_tool_approvals (tool_call_id, project_id, payload)
               VALUES (?, ?, ?)""",
            (tool_call_id, record.get("project_id", ""), json.dumps(record, ensure_ascii=False)),
        )
        await db.commit()
    except Exception as e:
        _logger.warning("pending_tool 持久化失败（非致命）: %s", e)


async def _persist_remove(tool_call_id: str) -> None:
    try:
        from core.storage.database import get_db
        db = await get_db()
        await db.execute(
            "DELETE FROM pending_tool_approvals WHERE tool_call_id = ?",
            (tool_call_id,),
        )
        await db.commit()
    except Exception as e:
        _logger.warning("pending_tool 删除持久化失败（非致命）: %s", e)


async def load_pending_from_db() -> int:
    """服务启动时恢复未决审批（main.py lifespan 调用）"""
    try:
        from core.storage.database import get_db
        db = await get_db()
        cursor = await db.execute(
            "SELECT tool_call_id, payload FROM pending_tool_approvals"
        )
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            try:
                record = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            tool_call_id = row["tool_call_id"]
            if tool_call_id not in _pending:
                _pending[tool_call_id] = record
                count += 1
        return count
    except Exception as e:
        _logger.warning("pending_tool 启动恢复失败（非致命）: %s", e)
        return 0


# ============================================================
# 同步主路径 API（契约不变）
# ============================================================

def register_pending(
    tool_call_id: str,
    *,
    project_id: str,
    agent_id: str,
    tool_name: str,
    tool_args: dict,
    namespaces: Optional[List[str]] = None,
    allowed_tools: Optional[List[str]] = None,
    output_dir: str = "",
    reason: str = "",
) -> None:
    record = {
        "tool_call_id": tool_call_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "tool_name": tool_name,
        "tool_args": dict(tool_args or {}),
        "namespaces": list(namespaces or []),
        "allowed_tools": list(allowed_tools or []),
        "output_dir": output_dir,
        "reason": reason,
        "created_at": time.time(),
    }
    _pending[tool_call_id] = record
    _schedule(_persist_register(tool_call_id, record))


def get_pending(tool_call_id: str) -> Optional[Dict[str, Any]]:
    return _pending.get(tool_call_id)


def list_pending(project_id: str) -> List[Dict[str, Any]]:
    return [
        dict(v)
        for v in _pending.values()
        if v.get("project_id") == project_id
    ]


def pop_pending(tool_call_id: str) -> Optional[Dict[str, Any]]:
    record = _pending.pop(tool_call_id, None)
    if record is not None:
        _schedule(_persist_remove(tool_call_id))
    return record


def clear_all() -> None:
    """仅供测试使用"""
    _pending.clear()
