"""ProjectRepository — Project 聚合根"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseRepository


class ProjectRepository(BaseRepository):
    """项目数据访问层"""

    TABLE = "projects"

    async def find_by_status(self, status: str) -> List[Dict[str, Any]]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM projects WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def upsert(self, project_data: Dict[str, Any]) -> str:
        db = await self._get_db()
        columns = ", ".join(project_data.keys())
        placeholders = ", ".join(["?"] * len(project_data))
        await db.execute(
            f"INSERT OR REPLACE INTO projects ({columns}) VALUES ({placeholders})",
            tuple(project_data.values()),
        )
        await db.commit()
        return project_data.get("id", "")
