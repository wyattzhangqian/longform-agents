"""CrossRunMemoryStore — 跨 Run 记忆存储 + GC

职责：
- CRUD 跨 run 记忆（SQLite cross_run_memories 表）
- 记忆衰减（decay_score 每次写入新记忆时递减）
- 容量淘汰（超过 max_capacity 时保留 decay_score 最高的）
- 访问 boost（被注入上下文时 decay_score 增加）
- GC 统计记录
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from core.memory.models import MemoryEntry, MemoryType
from core.storage.database import get_db
from core.logging import get_logger

_logger = get_logger("memory.store")


class CrossRunMemoryStore:
    """跨 Run 记忆存储"""

    MAX_CAPACITY = 100
    DECAY_RATE = 0.05        # 每次 GC 衰减量
    ACCESS_BOOST = 0.1       # 被访问时的 boost
    GC_THRESHOLD = 0.1       # 低于此分数的记忆被移除

    # ==================== 读取 ====================

    @staticmethod
    async def get_all(agent_id: str) -> List[MemoryEntry]:
        """获取 agent 的所有跨 run 记忆"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM cross_run_memories WHERE agent_id = ? ORDER BY decay_score DESC, created_at DESC",
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [MemoryEntry.from_row(dict(r)) for r in rows]

    @staticmethod
    async def get_relevant_memories(
        agent_id: str,
        limit: int = 10,
        min_decay_score: float = 0.3,
    ) -> List[MemoryEntry]:
        """获取 decay_score 达标的记忆（供上下文注入）"""
        db = await get_db()
        cursor = await db.execute(
            """SELECT * FROM cross_run_memories
               WHERE agent_id = ? AND decay_score >= ?
               ORDER BY confidence DESC, decay_score DESC
               LIMIT ?""",
            (agent_id, min_decay_score, limit),
        )
        rows = await cursor.fetchall()
        return [MemoryEntry.from_row(dict(r)) for r in rows]

    @staticmethod
    async def get_by_type(
        agent_id: str,
        memory_type: MemoryType,
        limit: int = 20,
    ) -> List[MemoryEntry]:
        """按类型获取记忆"""
        db = await get_db()
        cursor = await db.execute(
            """SELECT * FROM cross_run_memories
               WHERE agent_id = ? AND memory_type = ?
               ORDER BY decay_score DESC, created_at DESC LIMIT ?""",
            (agent_id, memory_type.value, limit),
        )
        rows = await cursor.fetchall()
        return [MemoryEntry.from_row(dict(r)) for r in rows]

    # ==================== 写入 ====================

    @staticmethod
    async def add_memories(agent_id: str, entries: List[MemoryEntry]) -> int:
        """批量写入记忆（会触发 GC）"""
        if not entries:
            return 0
        db = await get_db()
        added = 0
        for entry in entries:
            d = entry.to_dict()
            await db.execute(
                """INSERT OR REPLACE INTO cross_run_memories
                   (id, agent_id, memory_type, content, source_run_id, source_phase,
                    confidence, created_at, last_accessed, access_count, decay_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d["id"], d["agent_id"], d["memory_type"], d["content"],
                    d["source_run_id"], d["source_phase"], d["confidence"],
                    d["created_at"], d["last_accessed"], d["access_count"],
                    d["decay_score"],
                ),
            )
            added += 1
        await db.commit()

        # 触发 GC
        if added > 0:
            await CrossRunMemoryStore.gc(agent_id)

        _logger.info("写入 %d 条跨 run 记忆: agent=%s", added, agent_id)
        return added

    @staticmethod
    async def record_access(memory_id: str) -> None:
        """记忆被使用时 boost decay_score + 更新访问计数"""
        db = await get_db()
        await db.execute(
            """UPDATE cross_run_memories
               SET access_count = access_count + 1,
                   last_accessed = datetime('now', 'localtime'),
                   decay_score = MIN(1.0, decay_score + ?)
               WHERE id = ?""",
            (CrossRunMemoryStore.ACCESS_BOOST, memory_id),
        )
        await db.commit()

    # ==================== GC ====================

    @staticmethod
    async def gc(agent_id: str) -> int:
        """衰减 + 容量淘汰，返回移除的记忆数"""
        db = await get_db()

        # 1. 衰减所有记忆
        await db.execute(
            """UPDATE cross_run_memories
               SET decay_score = MAX(0, decay_score - ?)
               WHERE agent_id = ?""",
            (CrossRunMemoryStore.DECAY_RATE, agent_id),
        )

        # 2. 移除低于阈值的
        cursor = await db.execute(
            "DELETE FROM cross_run_memories WHERE agent_id = ? AND decay_score < ?",
            (agent_id, CrossRunMemoryStore.GC_THRESHOLD),
        )
        removed = cursor.rowcount

        # 3. 容量淘汰（保留 decay_score 最高的 max_capacity 条）
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM cross_run_memories WHERE agent_id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        total = row["cnt"] if row else 0

        if total > CrossRunMemoryStore.MAX_CAPACITY:
            excess = total - CrossRunMemoryStore.MAX_CAPACITY
            await db.execute(
                """DELETE FROM cross_run_memories
                   WHERE id IN (
                       SELECT id FROM cross_run_memories
                       WHERE agent_id = ?
                       ORDER BY decay_score ASC, created_at ASC
                       LIMIT ?
                   )""",
                (agent_id, excess),
            )
            removed += excess

        await db.commit()

        # 4. 更新 GC 统计
        await CrossRunMemoryStore._update_gc_stats(agent_id, removed)

        if removed > 0:
            _logger.info("GC 完成: agent=%s, removed=%d", agent_id, removed)

        return removed

    @staticmethod
    async def _update_gc_stats(agent_id: str, removed: int) -> None:
        """更新 GC 统计表"""
        db = await get_db()
        # UPSERT
        cursor = await db.execute(
            "SELECT total_gc_runs, total_removed FROM cross_run_memory_gc_stats WHERE agent_id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                """UPDATE cross_run_memory_gc_stats
                   SET total_gc_runs = total_gc_runs + 1,
                       total_removed = total_removed + ?,
                       last_gc_at = datetime('now', 'localtime'),
                       current_capacity = (
                           SELECT COUNT(*) FROM cross_run_memories WHERE agent_id = ?
                       )
                   WHERE agent_id = ?""",
                (removed, agent_id, agent_id),
            )
        else:
            cursor2 = await db.execute(
                "SELECT COUNT(*) as cnt FROM cross_run_memories WHERE agent_id = ?",
                (agent_id,),
            )
            row2 = await cursor2.fetchone()
            cap = row2["cnt"] if row2 else 0
            await db.execute(
                """INSERT INTO cross_run_memory_gc_stats
                   (agent_id, total_gc_runs, total_removed, last_gc_at, current_capacity)
                   VALUES (?, 1, ?, datetime('now', 'localtime'), ?)""",
                (agent_id, removed, cap),
            )
        await db.commit()

    @staticmethod
    async def get_gc_stats(agent_id: str) -> Dict:
        """获取 GC 统计"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM cross_run_memory_gc_stats WHERE agent_id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"total_gc_runs": 0, "total_removed": 0, "last_gc_at": "", "current_capacity": 0}
        return dict(row)

    @staticmethod
    async def format_for_context(memories: List[MemoryEntry]) -> str:
        """格式化记忆为可注入上下文的文本"""
        if not memories:
            return ""
        lines = ["## 学习到的偏好与模式（跨 Run 记忆）"]
        for mem in memories:
            lines.append(f"- [{mem.memory_type.value}] {mem.content}")
        return "\n".join(lines)
