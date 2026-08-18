"""MemoryManager — Agent 记忆读写

对接 agent_memories 表，在 execute 前后加载/保存 episodic 记忆。
"""

from __future__ import annotations
import json
from typing import List, Dict, Any, Optional
from core.storage.database import get_db
from core.logging import get_logger

_logger = get_logger("memory")


class MemoryManager:
    """Agent 记忆管理器"""

    @staticmethod
    async def recall(
        agent_id: str,
        project_id: str = "",
        memory_type: str = "episodic",
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """读取 Agent 记忆"""
        db = await get_db()
        if project_id:
            cursor = await db.execute(
                """SELECT key, value, importance, created_at FROM agent_memories
                   WHERE agent_id = ? AND project_id = ? AND memory_type = ?
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (agent_id, project_id, memory_type, limit),
            )
        else:
            cursor = await db.execute(
                """SELECT key, value, importance, created_at FROM agent_memories
                   WHERE agent_id = ? AND memory_type = ?
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (agent_id, memory_type, limit),
            )
        rows = await cursor.fetchall()
        memories = []
        for row in rows:
            r = dict(row)
            try:
                r["value"] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                pass
            memories.append(r)
            await db.execute(
                "UPDATE agent_memories SET access_count = access_count + 1, last_accessed = datetime('now', 'localtime') WHERE agent_id = ? AND key = ?",
                (agent_id, r["key"]),
            )
        if memories:
            await db.commit()
        return memories

    @staticmethod
    async def store(
        agent_id: str,
        key: str,
        value: Any,
        project_id: str = "",
        memory_type: str = "episodic",
        importance: float = 0.5,
    ) -> None:
        """写入或更新一条记忆（UPSERT）"""
        db = await get_db()
        pid = project_id or ""
        value_str = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        cursor = await db.execute(
            """UPDATE agent_memories
               SET value = ?, importance = ?,
                   last_accessed = datetime('now', 'localtime'),
                   access_count = access_count + 1
               WHERE agent_id = ? AND COALESCE(project_id, '') = ? AND memory_type = ? AND key = ?""",
            (value_str, importance, agent_id, pid, memory_type, key),
        )
        if cursor.rowcount == 0:
            await db.execute(
                """INSERT INTO agent_memories (agent_id, project_id, memory_type, key, value, importance)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (agent_id, pid, memory_type, key, value_str, importance),
            )
        await db.commit()

    @staticmethod
    async def store_execution_summary(
        agent_id: str,
        project_id: str,
        result: Any,
        task: str = "",
    ) -> None:
        """保存一次执行摘要到 episodic 记忆"""
        import time
        summary = _summarize_result(result)
        key = f"exec_{int(time.time())}"
        await MemoryManager.store(
            agent_id=agent_id,
            key=key,
            value={"task": task[:200], "summary": summary, "result_preview": str(result)[:500]},
            project_id=project_id,
            memory_type="episodic",
            importance=0.6,
        )

    @staticmethod
    def format_for_context(memories: List[Dict[str, Any]]) -> str:
        """将记忆格式化为可注入上下文的文本"""
        if not memories:
            return ""
        lines = ["## 历史记忆"]
        for m in memories:
            val = m.get("value", "")
            if isinstance(val, dict):
                val = val.get("summary") or json.dumps(val, ensure_ascii=False)[:200]
            lines.append(f"- [{m.get('key', '')}] {str(val)[:150]}")
        return "\n".join(lines)


def _summarize_result(result: Any) -> str:
    if isinstance(result, dict):
        parts = []
        for k, v in list(result.items())[:5]:
            if k.startswith("_"):
                continue
            if isinstance(v, str):
                parts.append(f"{k}: {v[:80]}")
            else:
                parts.append(f"{k}: {type(v).__name__}")
        return "; ".join(parts) or str(result)[:200]
    return str(result)[:300]
