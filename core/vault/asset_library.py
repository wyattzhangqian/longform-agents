"""AssetLibrary — 平台级资产管理服务

三个核心操作：promote / reference / fork
"""
from __future__ import annotations
import json, shutil, uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.storage.database import get_db
from core.logging import get_logger
from core.vault.project_artifact_service import ProjectArtifactService, OUTPUTS_ROOT

_logger = get_logger("asset_library")
ASSETS_ROOT = Path("assets").resolve()
ASSETS_ROOT.mkdir(exist_ok=True)


class AssetLibrary:

    @staticmethod
    async def list_assets(
        type: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        visibility: str = "all",
        owner_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        db = await get_db()
        conds, params = [], []
        if type:
            conds.append("type = ?"); params.append(type)
        if category:
            conds.append("category = ?"); params.append(category)
        if visibility != "all":
            conds.append("visibility = ?"); params.append(visibility)
        if owner_id:
            conds.append("owner_id = ?"); params.append(owner_id)
        if search:
            conds.append("(name LIKE ? OR description LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if tags:
            for tag in tags:
                conds.append("tags LIKE ?"); params.append(f'%"{tag}"%')
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        params.extend([limit, offset])
        cursor = await db.execute(
            f"SELECT * FROM asset_library {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", params)
        return [dict(r) for r in await cursor.fetchall()]

    @staticmethod
    async def get_asset(asset_id: str) -> Optional[Dict[str, Any]]:
        db = await get_db()
        cursor = await db.execute("SELECT * FROM asset_library WHERE id = ?", (asset_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    async def delete_asset(asset_id: str) -> bool:
        """删除资产（同时清理项目引用）"""
        db = await get_db()
        asset = await AssetLibrary.get_asset(asset_id)
        if not asset:
            return False
        # 清理项目引用
        await db.execute("DELETE FROM project_asset_refs WHERE asset_id = ?", (asset_id,))
        # 删除资产记录
        await db.execute("DELETE FROM asset_library WHERE id = ?", (asset_id,))
        await db.commit()
        # 删除关联文件（如果有）
        if asset.get("content_path"):
            try:
                path = ASSETS_ROOT / asset["content_path"]
                if path.is_file():
                    path.unlink()
            except Exception:
                pass
        return True

    @staticmethod
    async def get_asset_content(asset_id: str) -> Optional[str]:
        asset = await AssetLibrary.get_asset(asset_id)
        if not asset:
            return None
        if asset.get("content_text"):
            return asset["content_text"]
        if asset.get("content_path"):
            path = ASSETS_ROOT / asset["content_path"]
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None

    @staticmethod
    async def promote(
        project_id: str, artifact_name: str, *,
        name: str = "", type: str = "document", category: str = "",
        tags: List[str] = None, description: str = "",
        owner_id: str = "", visibility: str = "private",
    ) -> Dict[str, Any]:
        """[结合点] 调用 ProjectArtifactService.resolve_project_path() 读取项目产物"""
        source_path = ProjectArtifactService.resolve_project_path(project_id, artifact_name)
        if not source_path.is_file():
            raise FileNotFoundError(f"产物 {artifact_name} 不存在于项目 {project_id}")
        content = source_path.read_text(encoding="utf-8")
        file_size = source_path.stat().st_size
        asset_id = f"asset_{uuid.uuid4().hex[:12]}"
        asset_dir = ASSETS_ROOT / asset_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        content_text = content if file_size <= 65536 else None
        content_path = ""
        if file_size > 65536:
            shutil.copy2(source_path, asset_dir / artifact_name)
            content_path = f"{asset_id}/{artifact_name}"
        db = await get_db()
        await db.execute(
            """INSERT INTO asset_library
               (id,name,type,category,tags,description,content_path,content_text,
                mime_type,file_size,source_project_id,source_artifact,owner_id,visibility)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (asset_id, name or artifact_name, type, category,
             json.dumps(tags or [], ensure_ascii=False), description,
             content_path, content_text, _guess_mime(artifact_name), file_size,
             project_id, artifact_name, owner_id, visibility))
        await db.commit()
        return {"asset_id": asset_id, "name": name or artifact_name, "type": type}

    @staticmethod
    async def reference(project_id: str, asset_id: str, alias: str = "") -> Dict[str, Any]:
        asset = await AssetLibrary.get_asset(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id} not found")
        db = await get_db()
        ref_id = f"ref_{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT OR REPLACE INTO project_asset_refs (id,project_id,asset_id,ref_type,alias) VALUES (?,?,?,'reference',?)",
            (ref_id, project_id, asset_id, alias or asset["name"]))
        await db.execute("UPDATE asset_library SET ref_count=ref_count+1 WHERE id=?", (asset_id,))
        await db.commit()
        return {"ref_id": ref_id, "asset_id": asset_id, "alias": alias or asset["name"]}

    @staticmethod
    async def fork(project_id: str, asset_id: str, target_filename: str = "") -> Dict[str, Any]:
        """[结合点] 调用 ProjectArtifactService.write_text() 写入项目工作区

        注意: write_text 参数名是 relative_path，不是 filename。
        """
        asset = await AssetLibrary.get_asset(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id} not found")
        filename = target_filename or asset.get("source_artifact") or f"{asset['name']}.md"
        content = await AssetLibrary.get_asset_content(asset_id)
        if not content:
            raise ValueError(f"Asset {asset_id} has no content")
        await ProjectArtifactService.write_text(
            project_id=project_id, relative_path=filename,
            content=content, agent_id="system",
            metadata={"source": "asset_fork"})
        db = await get_db()
        ref_id = f"fork_{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT OR REPLACE INTO project_asset_refs (id,project_id,asset_id,ref_type,alias) VALUES (?,?,?,'fork',?)",
            (ref_id, project_id, asset_id, filename))
        await db.execute("UPDATE asset_library SET fork_count=fork_count+1 WHERE id=?", (asset_id,))
        await db.commit()
        return {"ref_id": ref_id, "asset_id": asset_id, "filename": filename}

    @staticmethod
    async def list_project_refs(project_id: str) -> List[Dict[str, Any]]:
        db = await get_db()
        cursor = await db.execute(
            """SELECT r.*, a.name as asset_name, a.type as asset_type, a.description, a.tags
               FROM project_asset_refs r JOIN asset_library a ON r.asset_id=a.id
               WHERE r.project_id=? ORDER BY r.created_at DESC""", (project_id,))
        return [dict(r) for r in await cursor.fetchall()]


def _guess_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {".md":"text/markdown",".txt":"text/plain",".json":"application/json",
            ".png":"image/png",".jpg":"image/jpeg",".mp4":"video/mp4",".mp3":"audio/mpeg"
    }.get(ext, "application/octet-stream")
