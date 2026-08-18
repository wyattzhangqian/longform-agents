"""产物登记 — 兼容入口，委托 ProjectArtifactService"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.vault.project_artifact_service import ProjectArtifactService

file_path_for_db = ProjectArtifactService.file_path_for_db


async def register_file_artifact(
    *,
    project_id: str,
    agent_id: str,
    resolved_path: Path,
    name: str = "",
    metadata: Optional[dict] = None,
    run_id: str = "",
) -> Optional[str]:
    return await ProjectArtifactService.register_file(
        project_id=project_id,
        agent_id=agent_id,
        resolved_path=resolved_path,
        name=name,
        metadata=metadata,
        run_id=run_id,
    )


async def register_file_artifact_rel(
    *,
    project_id: str,
    agent_id: str,
    rel_path: str,
    name: str = "",
) -> Optional[str]:
    resolved = ProjectArtifactService.resolve_project_path(project_id, rel_path)
    return await register_file_artifact(
        project_id=project_id,
        agent_id=agent_id,
        resolved_path=resolved,
        name=name or resolved.name,
    )
