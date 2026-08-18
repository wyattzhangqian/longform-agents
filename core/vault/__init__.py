"""Asset Vault — 统一资产仓库

提供资产的版本化写入/读取、锁机制、哈希校验。
底层使用 SQLite artifacts 表 + 文件系统。
"""

from __future__ import annotations
import json
import hashlib
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List
from core.storage.database import get_db
from .models import AssetVersion, AssetLock


class AssetVault:
    """资产仓库 — 统一读写接口

    用法：
        vault = AssetVault()
        await vault.write("my_script", {"script": "..."}, agent_id="scriptwriter")
        content = await vault.read("my_script")
    """

    def __init__(self, base_dir: str = "outputs"):
        self.base_dir = Path(base_dir)

    async def write(
        self,
        asset_id: str,
        content: Any,
        agent_id: str = "",
        project_id: str = "",
        asset_type: str = "generic",
        metadata: dict = None,
    ) -> AssetVersion:
        """写入资产（自动版本递增）

        Args:
            asset_id: 资产标识
            content: 资产内容（dict/str/bytes）
            agent_id: 创建者
            project_id: 所属项目
            asset_type: 类型（script/image/video/...）
            metadata: 附加元数据

        Returns:
            AssetVersion 记录
        """
        db = await get_db()

        # 计算版本号
        cursor = await db.execute(
            "SELECT MAX(version_num) FROM asset_versions WHERE asset_id = ?",
            (asset_id,),
        )
        row = await cursor.fetchone()
        version_num = (row[0] or 0) + 1 if row and row[0] else 1

        from core.communication.content_format import (
            extract_display_content,
            is_markdown_path,
            looks_like_markdown,
        )

        # 优先存可读的 Markdown/文本，避免产物 Tab 显示整段 JSON
        display_body = extract_display_content(content)
        if display_body.strip():
            content_str = display_body
            ext = ".md" if looks_like_markdown(display_body) else ".txt"
        elif isinstance(content, str):
            content_str = content
            ext = ".md" if is_markdown_path(content) else ".txt"
        else:
            content_str = json.dumps(content, ensure_ascii=False)
            ext = ".json"
        content_hash = hashlib.sha256(content_str.encode()).hexdigest()[:16]

        # 写入文件系统 — 统一走 outputs/{project_id}/...
        from core.vault.project_artifact_service import ProjectArtifactService

        if project_id:
            rel_name = f"{asset_id}_v{version_num}{ext}"
            full = ProjectArtifactService.resolve_project_path(project_id, rel_name)
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content_str, encoding="utf-8")
            file_path = ProjectArtifactService.file_path_for_db(full)
        else:
            file_path = str(self.base_dir / f"{asset_id}_v{version_num}{ext}")
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            Path(file_path).write_text(content_str, encoding="utf-8")

        version_id = f"ver_{uuid.uuid4().hex[:12]}"

        # 写入数据库
        await db.execute(
            """INSERT INTO asset_versions
               (version_id, asset_id, version_num, content_hash, file_path,
                metadata, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id, asset_id, version_num, content_hash, file_path,
                json.dumps(metadata or {}), agent_id,
            ),
        )

        # 更新 artifacts 表（兼容旧 API）
        display_name = asset_id
        if isinstance(content, dict):
            display_name = str(content.get("role") or content.get("title") or asset_id)
        await db.execute(
            """INSERT OR REPLACE INTO artifacts (id, project_id, agent_id, type, name, file_path, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f"art_{asset_id}", project_id, agent_id, asset_type,
                display_name, file_path, json.dumps(metadata or {}),
            ),
        )

        await db.commit()

        return AssetVersion(
            version_id=version_id,
            asset_id=asset_id,
            version_num=version_num,
            content_hash=content_hash,
            file_path=file_path,
            metadata=metadata or {},
            created_by=agent_id,
        )

    async def read(self, asset_id: str, version_num: int = None) -> Optional[Any]:
        """读取资产最新版本或指定版本

        Returns:
            资产内容（dict/str），不存在返回 None
        """
        db = await get_db()

        if version_num:
            cursor = await db.execute(
                "SELECT * FROM asset_versions WHERE asset_id = ? AND version_num = ?",
                (asset_id, version_num),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM asset_versions WHERE asset_id = ? ORDER BY version_num DESC LIMIT 1",
                (asset_id,),
            )

        row = await cursor.fetchone()
        if not row:
            return None

        file_path = row["file_path"]
        if Path(file_path).exists():
            content = Path(file_path).read_text(encoding="utf-8")
            try:
                return json.loads(content)
            except (json.JSONDecodeError, ValueError):
                return content
        return None

    async def list_versions(self, asset_id: str) -> List[AssetVersion]:
        """列出资产的所有版本"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM asset_versions WHERE asset_id = ? ORDER BY version_num DESC",
            (asset_id,),
        )
        rows = await cursor.fetchall()
        return [
            AssetVersion(
                version_id=r["version_id"],
                asset_id=r["asset_id"],
                version_num=r["version_num"],
                content_hash=r.get("content_hash", ""),
                file_path=r.get("file_path", ""),
                metadata=json.loads(r.get("metadata", "{}")),
                created_by=r.get("created_by", ""),
                created_at=r.get("created_at", ""),
            )
            for r in rows
        ]

    async def lock(self, asset_id: str, agent_id: str, reason: str = "") -> AssetLock:
        """获取资产锁"""
        lock = AssetLock(asset_id=asset_id, locked_by=agent_id, reason=reason)

        db = await get_db()
        await db.execute(
            """INSERT OR REPLACE INTO asset_locks
               (asset_id, locked_by, locked_at, reason)
               VALUES (?, ?, ?, ?)""",
            (asset_id, agent_id, lock.locked_at, reason),
        )
        await db.commit()
        return lock

    async def unlock(self, asset_id: str) -> bool:
        """释放资产锁"""
        db = await get_db()
        await db.execute("DELETE FROM asset_locks WHERE asset_id = ?", (asset_id,))
        await db.commit()
        return True

    async def is_locked(self, asset_id: str) -> Optional[AssetLock]:
        """检查资产是否被锁定"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM asset_locks WHERE asset_id = ?", (asset_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return AssetLock(
            asset_id=row["asset_id"],
            locked_by=row["locked_by"],
            locked_at=row["locked_at"],
            expires_at=row.get("expires_at"),
            reason=row.get("reason", ""),
        )


# 单例
_vault: Optional[AssetVault] = None


def get_vault() -> AssetVault:
    global _vault
    if _vault is None:
        _vault = AssetVault()
    return _vault
