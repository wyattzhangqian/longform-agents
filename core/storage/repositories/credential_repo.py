"""CredentialRepository — 统一凭证库 CRUD

凭证 Fernet 加密存储（兼容旧 base64），查询时不返回明文。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core.security.secret_box import FERNET_PREFIX, decrypt_secret, encrypt_secret
from core.storage.repositories.base import BaseRepository


class CredentialRepository(BaseRepository):
    TABLE = "credentials"

    async def create(self, data: dict) -> str:
        cred_id = data["id"]
        encrypted = encrypt_secret(data["api_key"])
        db = await self._get_db()
        await db.execute(
            "INSERT OR REPLACE INTO credentials (id, name, provider, api_key, base_url, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                cred_id, data["name"], data["provider"], encrypted,
                data.get("base_url", ""), data.get("description", ""),
            ),
        )
        await db.commit()
        return cred_id

    async def list_all(self) -> List[dict]:
        """列表（不返回 api_key 明文）"""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, name, provider, base_url, description, created_at FROM credentials "
            "ORDER BY provider, name"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_decrypted_key(self, cred_id: str) -> Optional[str]:
        """返回解密后的 api_key 明文"""
        db = await self._get_db()
        cursor = await db.execute("SELECT api_key FROM credentials WHERE id = ?", (cred_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return decrypt_secret(row["api_key"])

    async def migrate_legacy_credentials(self) -> int:
        """将存量 base64 凭证重加密为 Fernet，返回迁移条数"""
        db = await self._get_db()
        cursor = await db.execute("SELECT id, api_key FROM credentials")
        rows = await cursor.fetchall()
        n = 0
        for row in rows:
            raw = row["api_key"] or ""
            if raw.startswith(FERNET_PREFIX):
                continue
            plain = decrypt_secret(raw)
            if not plain:
                continue
            await db.execute(
                "UPDATE credentials SET api_key=? WHERE id=?",
                (encrypt_secret(plain), row["id"]),
            )
            n += 1
        if n:
            await db.commit()
        return n

    async def get_by_provider(self, provider: str) -> Optional[dict]:
        """按 provider 取第一条凭证"""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, name, provider, base_url FROM credentials "
            "WHERE provider = ? ORDER BY created_at LIMIT 1",
            (provider,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete(self, cred_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))
        await db.commit()
        return cursor.rowcount > 0
