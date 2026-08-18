"""KnowledgeStore — 知识条目 DB 操作层

CRUD + 检索 + Fork 复制 + 统计
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from core.knowledge.models_kb import KnowledgeEntry
from core.logging import get_logger
from core.storage.database import get_db

_logger = get_logger("knowledge.store")


class KnowledgeStore:
    """知识条目的 DB 操作层"""

    @staticmethod
    async def list_entries(
        domain_id: str = "",
        domain_ids: Optional[List[str]] = None,
        phase_id: Optional[str] = None,
        category: Optional[str] = None,
        inject_mode: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """列出知识条目（支持多维度过滤）

        Args:
            domain_id: 单领域查询
            domain_ids: 多领域一次查询（优先级高于 domain_id）
        """
        db = await get_db()
        # [修改] 支持 domain_ids 列表查询
        if domain_ids:
            placeholders = ",".join("?" * len(domain_ids))
            conditions = [f"domain_id IN ({placeholders})"]
            params: list = list(domain_ids)
        elif domain_id:
            conditions = ["domain_id = ?"]
            params = [domain_id]
        else:
            conditions = ["1=1"]
            params = []

        if not include_archived:
            conditions.append("is_archived = 0")
        if phase_id is not None:
            conditions.append("(phase_id = ? OR phase_id = '')")
            params.append(phase_id)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if inject_mode:
            conditions.append("inject_mode = ?")
            params.append(inject_mode)

        where = " AND ".join(conditions)
        cursor = await db.execute(
            f"""SELECT * FROM knowledge_entries
                WHERE {where}
                ORDER BY priority DESC, sort_order ASC, created_at DESC
                LIMIT ?""",
            params + [limit],
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if isinstance(d.get("tags"), str):
                try:
                    d["tags"] = json.loads(d["tags"])
                except (json.JSONDecodeError, TypeError):
                    d["tags"] = []
            results.append(d)
        return results

    @staticmethod
    async def get_entry(entry_id: str) -> Optional[Dict[str, Any]]:
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM knowledge_entries WHERE id = ?", (entry_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("tags"), str):
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
        return d

    @staticmethod
    async def create_entry(entry: KnowledgeEntry) -> str:
        """创建知识条目"""
        db = await get_db()
        entry_id = entry.id or f"{entry.source}:{entry.owner_id or 'sys'}:ke_{int(time.time() * 1000)}"
        char_count = len(entry.content)

        await db.execute(
            """INSERT INTO knowledge_entries
               (id, domain_id, phase_id, category, title, content, tags,
                priority, inject_mode, inject_to, source, owner_id, char_count, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id, entry.domain_id, entry.phase_id, entry.category.value,
                entry.title, entry.content,
                json.dumps(entry.tags, ensure_ascii=False),
                entry.priority, entry.inject_mode.value, entry.inject_to,
                entry.source, entry.owner_id, char_count, entry.sort_order,
            ),
        )
        await db.commit()
        _logger.info("知识条目创建: %s [%s] %s", entry_id, entry.category.value, entry.title)
        return entry_id

    @staticmethod
    async def update_entry(entry_id: str, updates: Dict[str, Any], owner_id: str) -> bool:
        """更新知识条目（仅 user 条目可改）"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT source, owner_id FROM knowledge_entries WHERE id = ?", (entry_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if row["source"] == "platform":
            raise PermissionError("平台内置知识不可编辑，请 Fork 领域后修改")
        if row["owner_id"] != owner_id:
            raise PermissionError("只能编辑自己的知识条目")

        allowed_fields = [
            "title", "content", "category", "phase_id", "tags",
            "priority", "inject_mode", "inject_to", "is_archived", "sort_order",
        ]
        sets = []
        params = []
        for k, v in updates.items():
            if k not in allowed_fields:
                continue
            if k == "tags":
                v = json.dumps(v, ensure_ascii=False)
            elif k == "is_archived" and isinstance(v, bool):
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            params.append(v)

        if "content" in updates:
            sets.append("char_count = ?")
            params.append(len(updates["content"]))

        if not sets:
            return False

        sets.append("updated_at = datetime('now','localtime')")
        params.append(entry_id)
        await db.execute(
            f"UPDATE knowledge_entries SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()
        return True

    @staticmethod
    async def delete_entry(entry_id: str, owner_id: str) -> bool:
        """删除用户条目"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT source, owner_id FROM knowledge_entries WHERE id = ?", (entry_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if row["source"] == "platform":
            raise PermissionError("平台内置知识不可删除")
        if row["owner_id"] != owner_id:
            raise PermissionError("只能删除自己的知识条目")

        await db.execute("DELETE FROM knowledge_entries WHERE id = ?", (entry_id,))
        await db.commit()
        return True

    @staticmethod
    async def fork_domain_knowledge(
        source_domain_id: str, target_domain_id: str, owner_id: str
    ) -> int:
        """Fork 领域时复制所有知识条目"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM knowledge_entries WHERE domain_id = ? AND is_archived = 0",
            (source_domain_id,),
        )
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            new_id = f"user:{owner_id}:ke_{int(time.time() * 1000)}_{count}"
            d = dict(row)
            await db.execute(
                """INSERT INTO knowledge_entries
                   (id, domain_id, phase_id, category, title, content, tags,
                    priority, inject_mode, inject_to, source, owner_id, char_count, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'user', ?, ?, ?)""",
                (
                    new_id, target_domain_id, d["phase_id"], d["category"],
                    d["title"], d["content"], d["tags"],
                    d["priority"], d["inject_mode"], d["inject_to"],
                    owner_id, d["char_count"], d.get("sort_order", 0),
                ),
            )
            count += 1
        await db.commit()
        _logger.info("知识库 fork: %s → %s, %d 条", source_domain_id, target_domain_id, count)
        return count

    @staticmethod
    async def count_by_domain(domain_id: str) -> Dict[str, int]:
        """统计领域知识条目数"""
        db = await get_db()
        cursor = await db.execute(
            """SELECT category, COUNT(*) as cnt
               FROM knowledge_entries
               WHERE domain_id = ? AND is_archived = 0
               GROUP BY category""",
            (domain_id,),
        )
        rows = await cursor.fetchall()
        result: Dict[str, int] = {"total": 0}
        for row in rows:
            result[row["category"]] = row["cnt"]
            result["total"] += row["cnt"]
        return result
