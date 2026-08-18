"""Repository 基类 — 统一数据访问接口

所有业务 Repository 继承此类。预留 tenant_id 参数，多租户只需加列 + 传参。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.storage.database import get_db


class BaseRepository:
    """所有 Repository 的基类

    子类只需设置 TABLE 类变量，及覆盖具体业务方法。
    """

    TABLE: str = ""

    def __init__(self, db=None):
        self._db = db

    async def _get_db(self):
        if self._db is not None:
            return self._db
        return await get_db()

    async def find_by_id(
        self, id: str, tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        if tenant_id:
            cursor = await db.execute(
                f"SELECT * FROM {self.TABLE} WHERE id = ? AND tenant_id = ?",
                (id, tenant_id),
            )
        else:
            cursor = await db.execute(
                f"SELECT * FROM {self.TABLE} WHERE id = ?", (id,),
            )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_all(
        self, limit: int = 100, offset: int = 0, tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        db = await self._get_db()
        if tenant_id:
            cursor = await db.execute(
                f"SELECT * FROM {self.TABLE} WHERE tenant_id = ? LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            )
        else:
            cursor = await db.execute(
                f"SELECT * FROM {self.TABLE} LIMIT ? OFFSET ?", (limit, offset),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def insert(self, data: Dict[str, Any]) -> str:
        db = await self._get_db()
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        await db.execute(
            f"INSERT INTO {self.TABLE} ({columns}) VALUES ({placeholders})",
            tuple(data.values()),
        )
        await db.commit()
        return data.get("id", "")

    async def update(self, id: str, data: Dict[str, Any]) -> bool:
        db = await self._get_db()
        set_clause = ", ".join(f"{k} = ?" for k in data.keys())
        await db.execute(
            f"UPDATE {self.TABLE} SET {set_clause} WHERE id = ?",
            (*data.values(), id),
        )
        await db.commit()
        return True

    async def delete(self, id: str) -> bool:
        db = await self._get_db()
        await db.execute(f"DELETE FROM {self.TABLE} WHERE id = ?", (id,))
        await db.commit()
        return True

    async def count(self, tenant_id: Optional[str] = None) -> int:
        db = await self._get_db()
        if tenant_id:
            cursor = await db.execute(
                f"SELECT COUNT(*) as cnt FROM {self.TABLE} WHERE tenant_id = ?",
                (tenant_id,),
            )
        else:
            cursor = await db.execute(
                f"SELECT COUNT(*) as cnt FROM {self.TABLE}"
            )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
