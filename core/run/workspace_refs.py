"""WorkspaceRef 解析 — 从 artifact 表 / 产出结构提取引用 (D6)"""

from __future__ import annotations
from typing import Any, Dict, List

from core.run.run_context import WorkspaceRef
from core.logging import get_logger

_logger = get_logger("run.workspace_refs")


async def resolve_workspace_refs(
    project_id: str,
    agent_id: str,
    phase_id: str,
    result: Any,
    *,
    limit: int = 5,
) -> List[WorkspaceRef]:
    """解析 Agent 产出的 artifact 引用（不传全文）"""
    refs: List[WorkspaceRef] = []
    seen: set[str] = set()

    def _add(artifact_id: str = "", path: str = "", summary: str = "") -> None:
        key = artifact_id or path
        if not key or key in seen:
            return
        seen.add(key)
        refs.append(
            WorkspaceRef(
                artifact_id=artifact_id,
                path=path,
                summary=(summary or path or artifact_id)[:200],
                agent_id=agent_id,
                phase_id=phase_id,
            )
        )

    if isinstance(result, dict):
        for key in ("artifact_id", "file_path", "path", "output_path"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                _add(artifact_id=result.get("artifact_id", "") or "", path=val, summary=str(result.get("name") or key))
        arts = result.get("artifacts") or result.get("artifact_keys") or []
        if isinstance(arts, list):
            for item in arts[:limit]:
                if isinstance(item, str):
                    _add(path=item, summary=item)
                elif isinstance(item, dict):
                    _add(
                        artifact_id=str(item.get("id") or item.get("artifact_id") or ""),
                        path=str(item.get("file_path") or item.get("path") or ""),
                        summary=str(item.get("name") or item.get("type") or "artifact"),
                    )

    if project_id and len(refs) < limit:
        try:
            from core.storage.database import get_db

            db = await get_db()
            cursor = await db.execute(
                """SELECT id, name, file_path, type FROM artifacts
                   WHERE project_id = ? AND agent_id = ?
                   ORDER BY rowid DESC LIMIT ?""",
                (project_id, agent_id, limit),
            )
            rows = await cursor.fetchall()
            for row in rows:
                _add(
                    artifact_id=str(row["id"] or ""),
                    path=str(row["file_path"] or ""),
                    summary=str(row["name"] or row["type"] or "artifact"),
                )
        except Exception as e:
            _logger.warning("工作区引用解析失败，产物引用缺失: {}", e)

    return refs[:limit]
