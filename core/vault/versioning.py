"""Artifact Versioning — 产出物版本管理

在 ProjectArtifactService.write_text 写入新内容前，将当前文件备份到
.versions/ 目录，按时间戳命名。支持版本列表、读取指定版本、回滚。

版本存储路径: outputs/{project_id}/.versions/{artifact_name}/{timestamp}.md
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.logging import get_logger
from core.vault.project_artifact_service import OUTPUTS_ROOT, ProjectArtifactService

_logger = get_logger("vault.versioning")


class ArtifactVersioning:
    """产出物版本管理（基于文件系统 .versions/ 目录）"""

    @staticmethod
    def versions_dir(project_id: str, artifact_name: str) -> Path:
        """获取指定产物的版本目录"""
        return ProjectArtifactService.project_dir(project_id) / ".versions" / artifact_name

    @classmethod
    async def save_version_before_write(
        cls,
        project_id: str,
        artifact_name: str,
    ) -> Optional[str]:
        """在写入新内容前，备份当前文件版本

        Returns:
            version_id（时间戳字符串），如果当前文件不存在则返回 None
        """
        artifact_path = ProjectArtifactService.resolve_project_path(project_id, artifact_name)
        if not artifact_path.exists():
            return None

        version_dir = cls.versions_dir(project_id, artifact_name)
        version_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 避免同一秒内多次写入冲突
        version_path = version_dir / f"{timestamp}.md"
        counter = 0
        while version_path.exists():
            counter += 1
            version_path = version_dir / f"{timestamp}_{counter}.md"

        shutil.copy2(artifact_path, version_path)
        _logger.info("已保存版本: %s/%s → %s", project_id, artifact_name, version_path.name)
        return version_path.stem

    @classmethod
    async def write_with_version(
        cls,
        project_id: str,
        relative_path: str,
        content: str,
        agent_id: str = "",
        artifact_type: str = "file",
        metadata: Optional[dict] = None,
    ) -> dict:
        """写入 artifact 并保存前一版本

        Returns:
            {"name": str, "version": str|None, "path": str}
        """
        # 保存当前版本（如果文件存在）
        version_id = await cls.save_version_before_write(project_id, relative_path)

        # 写入新内容
        full = await ProjectArtifactService.write_text(
            project_id=project_id,
            relative_path=relative_path,
            content=content,
            agent_id=agent_id,
            artifact_type=artifact_type,
            metadata=metadata,
        )

        return {
            "name": relative_path,
            "version": version_id or "initial",
            "path": str(full) if full else "",
        }

    @classmethod
    async def get_versions(cls, project_id: str, artifact_name: str) -> List[dict]:
        """获取 artifact 的所有历史版本

        Returns:
            [{"version_id": str, "timestamp": str, "size": int}, ...]
            按 timestamp 降序排列
        """
        version_dir = cls.versions_dir(project_id, artifact_name)
        if not version_dir.exists():
            return []

        versions = []
        for f in sorted(version_dir.iterdir(), reverse=True):
            if not f.is_file():
                continue
            versions.append({
                "version_id": f.stem,
                "timestamp": f.stem,
                "size": f.stat().st_size,
            })
        return versions

    @classmethod
    async def get_version_content(
        cls, project_id: str, artifact_name: str, version_id: str
    ) -> str:
        """读取指定版本的内容

        Raises:
            FileNotFoundError: 版本不存在
        """
        version_dir = cls.versions_dir(project_id, artifact_name)
        # 尝试精确匹配 + 通配匹配（含 _counter 后缀）
        path = version_dir / f"{version_id}.md"
        if not path.exists():
            # 尝试 {version_id}_N.md
            for f in version_dir.iterdir():
                if f.name.startswith(f"{version_id}"):
                    path = f
                    break
            else:
                raise FileNotFoundError(f"版本 {version_id} 不存在")

        return path.read_text(encoding="utf-8")

    @classmethod
    async def revert_to_version(
        cls, project_id: str, artifact_name: str, version_id: str
    ) -> dict:
        """回滚到指定版本（当前版本先存档，不丢失）

        Returns:
            {"name": str, "version": str, "path": str}
        """
        old_content = await cls.get_version_content(project_id, artifact_name, version_id)
        return await cls.write_with_version(
            project_id=project_id,
            relative_path=artifact_name,
            content=old_content,
            agent_id="",
            artifact_type="file",
            metadata={"reverted_from": version_id},
        )

    @classmethod
    async def get_current_content(cls, project_id: str, artifact_name: str) -> str:
        """读取当前版本内容"""
        path = ProjectArtifactService.resolve_project_path(project_id, artifact_name)
        if not path.exists():
            raise FileNotFoundError(f"产物 {artifact_name} 不存在")
        return path.read_text(encoding="utf-8")
