"""ProjectArtifactService — 项目绑定产物唯一写入/登记入口"""

from __future__ import annotations

import json
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional

from core.communication.content_format import (
    extract_deliverable_body,
    extract_deliverable_for_path,
    extract_display_content,
    is_clean_deliverable_body,
    is_deliverable_file_content,
    is_ephemeral_agent_output,
    looks_like_markdown,
)
from core.logging import get_logger
from core.run.run_context import WorkspaceRef
from core.storage.database import get_db

_logger = get_logger("project_artifact")

OUTPUTS_ROOT = Path("outputs").resolve()

# 领域中立：默认产物文件 schema 为空，不预设任何特定领域的文件名。
# 产物文件名由 project 绑定的 domain_id 对应的 phase_definitions.artifact_files 决定。
_DEFAULT_PHASE_ARTIFACTS: dict[str, list[str]] = {}

PREVIEWABLE_SUFFIXES = {
    ".md", ".markdown", ".mdx", ".txt", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".mp4", ".webm", ".mov",
}

# Agent 兜底物化误用的 {agent_id}.md 前缀
_AGENT_FALLBACK_MD_PREFIXES = ("fixture_agent_",)


class ArtifactCheckResult(str, Enum):
    """产物检查三类状态（P0-3）：正常 / 无产物 / 查询异常。"""

    PRESENT = "present"
    MISSING = "missing"
    CHECK_FAILED = "check_failed"


async def get_phase_artifacts_for_project(
    project_id: str,
    phase_id: str,
) -> list[str]:
    """根据项目绑定的领域读取该 phase 应产出的文件清单。

    优先级：
    1. project.config 中显式配置的 phase_artifacts
    2. 项目 domain_id 对应 quality_domains.phase_definitions 中的 artifact_files
    3. _DEFAULT_PHASE_ARTIFACTS（空）
    """
    # 延迟导入避免循环依赖
    try:
        from core.project.manager import ProjectManager
        project = await ProjectManager.get(project_id)
    except Exception:
        project = None

    if project:
        cfg = getattr(project, "config", None) or {}
        if isinstance(cfg, str):
            import json as _json
            try:
                cfg = _json.loads(cfg)
            except Exception:
                cfg = {}
        explicit = cfg.get("phase_artifacts", {}).get(phase_id)
        if explicit and isinstance(explicit, list):
            return [str(x) for x in explicit]

        domain_id = cfg.get("domain_id", "")
        if domain_id:
            try:
                from core.gateway.domain_registry import DomainRegistry
                domain = await DomainRegistry.get_domain(domain_id)
                if domain:
                    phase_defs = domain.get("phase_definitions", [])
                    if isinstance(phase_defs, str):
                        import json as _json
                        phase_defs = _json.loads(phase_defs)
                    for pd in phase_defs:
                        if pd.get("id") == phase_id:
                            return pd.get("artifact_files", [])
            except Exception:
                pass

    return _DEFAULT_PHASE_ARTIFACTS.get(phase_id, [])


def get_known_deliverable_names() -> frozenset[str]:
    """动态获取已知交付文件名集合（领域中立，当前为空）。

    如需领域专属文件名，请在调用时通过 get_phase_artifacts_for_project 获取。
    """
    return frozenset(
        name for names in _DEFAULT_PHASE_ARTIFACTS.values() for name in names
    )


class ProjectArtifactService:
    """项目级产物：统一目录 outputs/{project_id}/ + artifacts 表"""

    @staticmethod
    def project_dir(project_id: str) -> Path:
        return OUTPUTS_ROOT / project_id

    @staticmethod
    def resolve_tool_path(project_id: str, path: str) -> str:
        """Agent 工具 path → 相对 outputs/ 根的路径"""
        raw = (path or "").replace("\\", "/").strip().lstrip("/")
        while raw.startswith("outputs/"):
            raw = raw[len("outputs/"):]
        if project_id:
            if raw.startswith(f"{project_id}/"):
                return raw
            return f"{project_id}/{raw}"
        return raw

    @staticmethod
    def relative_to_outputs(resolved: Path) -> str:
        try:
            return resolved.resolve().relative_to(OUTPUTS_ROOT).as_posix()
        except ValueError:
            return resolved.name

    @staticmethod
    def file_path_for_db(resolved: Path) -> str:
        rel = ProjectArtifactService.relative_to_outputs(resolved)
        return f"outputs/{rel}"

    @classmethod
    def resolve_project_path(cls, project_id: str, relative_path: str) -> Path:
        rel = cls.resolve_tool_path(project_id, relative_path)
        return cls._validate_under_outputs(rel)

    @staticmethod
    def _validate_under_outputs(relative_path: str) -> Path:
        p = Path(relative_path)
        if not p.is_absolute():
            p = OUTPUTS_ROOT / relative_path
        p = p.resolve()
        if not str(p).startswith(str(OUTPUTS_ROOT)):
            raise ValueError(f"路径越权: {relative_path}")
        return p

    @classmethod
    async def normalize_deliverable_file(
        cls,
        project_id: str,
        filename: str,
        *,
        agent_id: str = "",
    ) -> bool:
        """将 Agent 过程文本规范化为干净交付物；成功返回 True。"""
        if filename not in get_known_deliverable_names():
            return False
        try:
            full = cls.resolve_project_path(project_id, filename)
        except ValueError:
            return False
        if not full.is_file():
            return False
        try:
            raw = full.read_text(encoding="utf-8")
        except OSError:
            return False
        cleaned = extract_deliverable_body(raw, filename)
        if not cleaned:
            return False
        if cleaned.strip() == raw.strip():
            return is_clean_deliverable_body(raw, filename)
        if not is_clean_deliverable_body(cleaned, filename):
            return False
        full.write_text(cleaned, encoding="utf-8")
        await cls.register_file(
            project_id=project_id,
            agent_id=agent_id,
            resolved_path=full,
            name=filename,
            artifact_type="file",
            metadata={"source": "normalized_deliverable"},
        )
        _logger.info("已规范化交付物 %s/%s", project_id, filename)
        return True

    @classmethod
    async def normalize_project_deliverables(cls, project_id: str) -> list[str]:
        """扫描并规范化项目目录下所有已知交付文件名。"""
        normalized: list[str] = []
        for name in sorted(get_known_deliverable_names()):
            if await cls.normalize_deliverable_file(project_id, name):
                normalized.append(name)
        return normalized

    @classmethod
    async def write_text(
        cls,
        *,
        project_id: str,
        relative_path: str,
        content: str,
        agent_id: str = "",
        artifact_type: str = "file",
        metadata: Optional[dict] = None,
    ) -> Optional[Path]:
        if not project_id or not content:
            return None
        rel = cls.resolve_tool_path(project_id, relative_path)
        full = cls._validate_under_outputs(rel)

        # 版本管理：写入前保存当前版本（如果文件已存在）
        try:
            from core.vault.versioning import ArtifactVersioning
            await ArtifactVersioning.save_version_before_write(project_id, full.name)
        except Exception as e:
            _logger.debug("版本保存跳过（非致命）: %s", e)

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        await cls.register_file(
            project_id=project_id,
            agent_id=agent_id,
            resolved_path=full,
            name=full.name,
            artifact_type=artifact_type,
            metadata=metadata,
        )
        return full

    @classmethod
    async def register_file(
        cls,
        *,
        project_id: str,
        agent_id: str,
        resolved_path: Path,
        name: str = "",
        artifact_type: str = "file",
        metadata: Optional[dict] = None,
        run_id: str = "",
    ) -> Optional[str]:
        if not project_id or not resolved_path.is_file():
            return None
        file_path = cls.file_path_for_db(resolved_path)
        display = name or resolved_path.name
        art_id = f"art_{resolved_path.stem}_{uuid.uuid4().hex[:8]}"
        try:
            db = await get_db()
            await db.execute(
                """INSERT OR REPLACE INTO artifacts
                   (id, project_id, agent_id, type, name, file_path, metadata, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    art_id,
                    project_id,
                    agent_id or "",
                    artifact_type,
                    display,
                    file_path,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    run_id or "",
                ),
            )
            await db.commit()
            return art_id
        except Exception as e:
            _logger.warning("产物登记失败 %s: %s", file_path, e)
            return None

    @classmethod
    async def has_phase_artifact(
        cls,
        project_id: str,
        agent_id: str,
        phase_id: str,
        expected_files: Optional[List[str]] = None,
        *,
        run_id: str = "",
    ) -> bool:
        """本阶段是否已有落盘产物（按期望文件名 / 工具写入登记）。

        P0-3：传 run_id 时校验产物归属该 run（旧产物 run_id 为空/NULL 视为兼容）。
        """
        candidates: list[str] = list(expected_files or [])
        seen: set[str] = set()
        for fname in candidates:
            if not fname or fname in seen:
                continue
            seen.add(fname)
            if cls.resolve_project_path(project_id, fname).is_file():
                return True
        try:
            db = await get_db()
            if run_id:
                # P0-3 归属校验：artifacts 无 phase_id 列（phase 在 metadata JSON），
                # 用 project+agent+run_id 判定该 agent 在本 run 是否有产物。
                cursor = await db.execute(
                    """SELECT 1 FROM artifacts
                       WHERE project_id = ? AND agent_id = ?
                         AND (run_id IS NULL OR run_id = '' OR run_id = ?)
                         AND type != 'materialized'
                       LIMIT 1""",
                    (project_id, agent_id, run_id),
                )
            else:
                cursor = await db.execute(
                    """SELECT 1 FROM artifacts
                       WHERE project_id = ? AND agent_id = ?
                         AND type != 'materialized'
                       LIMIT 1""",
                    (project_id, agent_id),
                )
            if await cursor.fetchone():
                return True
        except Exception:
            pass
        return False

    @classmethod
    async def check_phase_artifact(
        cls,
        project_id: str,
        agent_id: str,
        phase_id: str,
        expected_files: Optional[List[str]] = None,
        run_id: str = "",
    ) -> "ArtifactCheckResult":
        """三类状态检查（P0-3）：区分 正常/无产物/查询异常。

        生产级：CHECK_FAILED 不能等同于 MISSING（前者是异常需治理，后者是确实没有）。
        """
        if not project_id or not phase_id:
            return ArtifactCheckResult.MISSING

        expected = list(expected_files or [])
        if not expected:
            try:
                expected = await get_phase_artifacts_for_project(project_id, phase_id)
            except Exception as e:
                _logger.error(
                    "获取阶段预期产物列表失败: project=%s phase=%s error=%s",
                    project_id, phase_id, e,
                )
                return ArtifactCheckResult.CHECK_FAILED

        try:
            has = await cls.has_phase_artifact(
                project_id, agent_id, phase_id, expected or None, run_id=run_id,
            )
        except Exception as e:
            _logger.error(
                "产物检查异常: project=%s agent=%s phase=%s error=%s",
                project_id, agent_id, phase_id, e,
            )
            return ArtifactCheckResult.CHECK_FAILED

        return ArtifactCheckResult.PRESENT if has else ArtifactCheckResult.MISSING

    @classmethod
    def extract_result_text(cls, result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, dict):
            if is_ephemeral_agent_output(result):
                return ""
            for key in ("result", "content", "markdown", "text", "output", "body"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    body = extract_display_content(val) or val
                    if body.strip() and not is_ephemeral_agent_output(body):
                        return body.strip()
            body = extract_display_content(result)
            if body.strip() and not is_ephemeral_agent_output(body):
                return body.strip()
            return ""
        text = extract_display_content(result) or str(result).strip()
        if is_ephemeral_agent_output(text):
            return ""
        return text

    @classmethod
    def primary_filename(cls, phase_id: str, expected_files: Optional[List[str]] = None) -> Optional[str]:
        if expected_files:
            return expected_files[0]
        return None

    @classmethod
    async def materialize_phase_output(
        cls,
        project_id: str,
        agent_id: str,
        phase_id: str,
        result: Any,
        *,
        expected_files: Optional[List[str]] = None,
    ) -> List[WorkspaceRef]:
        """阶段完成兜底：仅从 file_write/交付正文中落盘，绝不写入 Agent 思考过程。"""
        if not project_id:
            return []

        expected = list(expected_files or [])
        if not expected:
            # 从项目绑定的领域动态获取产物文件清单
            try:
                expected = await get_phase_artifacts_for_project(project_id, phase_id)
            except Exception:
                expected = []

        if await cls.has_phase_artifact(project_id, agent_id, phase_id, expected or None):
            return []

        raw_text = cls.extract_result_text(result)
        if isinstance(result, dict):
            raw_text = str(result.get("raw") or result.get("result") or raw_text)

        filename = cls.primary_filename(phase_id, expected)
        if not filename:
            # 兜底推断文件名（P0）：领域未配 artifact_files 时，从 phase_id / agent_id 推断，
            # 避免 writer 产出正文却因“无期望产物名”被跳过物化（writer_2 无产出根因之一）。
            filename = f"{phase_id or agent_id}.md"
            # loguru 不做 %s 格式化（坑），必须用 {} 或 f-string
            _logger.info(
                f"物化兜底推断文件名: {filename} (phase={phase_id} agent={agent_id})",
            )

        body = extract_deliverable_body(raw_text, filename)
        if not body:
            body = extract_deliverable_for_path(raw_text, filename)
        if not body or not is_clean_deliverable_body(body, filename):
            _logger.debug(
                "跳过物化：%s 无干净交付正文（Agent 须 file_write 或产出结构化文档）",
                filename,
            )
            return []

        full = await cls.write_text(
            project_id=project_id,
            relative_path=filename,
            content=body,
            agent_id=agent_id,
            artifact_type="file",
            metadata={
                "phase_id": phase_id,
                "source": "extracted_deliverable",
            },
        )
        if not full:
            return []

        art_id = f"mat_{phase_id}_{uuid.uuid4().hex[:6]}"
        rel = cls.relative_to_outputs(full)
        return [
            WorkspaceRef(
                artifact_id=art_id,
                path=cls.file_path_for_db(full),
                summary=f"物化产物 {filename}"[:200],
                agent_id=agent_id,
                phase_id=phase_id,
            )
        ]

    # --- Phase 1 新增：Artifact 状态机 ---

    VALID_TRANSITIONS = {
        "draft": ["review", "approved"],
        "review": ["approved", "draft"],
        "approved": ["deprecated"],
        "deprecated": [],
    }

    @classmethod
    async def get_artifact_status(cls, artifact_id: str) -> str:
        """获取 Artifact 当前状态"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT status FROM artifacts WHERE id = ?", (artifact_id,)
        )
        row = await cursor.fetchone()
        return row["status"] if row else "draft"

    @classmethod
    async def transition_status(
        cls, artifact_id: str, new_status: str, actor: str = ""
    ) -> bool:
        """
        状态流转。返回是否成功。
        不合法的流转会抛出 ValueError。
        """
        current = await cls.get_artifact_status(artifact_id)
        valid_next = cls.VALID_TRANSITIONS.get(current, [])
        if new_status not in valid_next:
            raise ValueError(
                f"Invalid status transition: {current} → {new_status}. "
                f"Valid: {valid_next}"
            )

        db = await get_db()
        from datetime import datetime
        approved_at = datetime.now().isoformat() if new_status == "approved" else ""
        await db.execute(
            "UPDATE artifacts SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?",
            (new_status, actor if new_status == "approved" else "", approved_at, artifact_id),
        )
        await db.commit()
        return True

    @classmethod
    async def is_approved(cls, artifact_id: str) -> bool:
        """快速检查是否已批准（供 Scheduler 约束使用）"""
        status = await cls.get_artifact_status(artifact_id)
        return status == "approved"


# Agent ID 后缀别名映射（领域中立，不再绑定特定领域）
_STEP_ALIASES: dict[str, str] = {}


def resolve_phase_step(phase_id: str, raw: dict) -> str:
    """从 phase 配置推断 workflow step。

    领域中立：仅从 raw 配置中读取 step 字段，不再推断特定领域的 step 映射。
    """
    step = str(raw.get("step") or "").strip()
    if step:
        return _STEP_ALIASES.get(step, step)
    return str(phase_id or "").strip()


async def expected_artifacts_for_phase_async(
    project_id: str,
    raw: dict,
    phase_id: str,
) -> list[str]:
    """从模板 phase 配置或项目绑定的领域解析期望产物文件列表。

    优先级：
    1. raw 中显式配置的 expected_artifacts
    2. 项目 domain_id 对应的 phase_definitions.artifact_files
    3. 空列表
    """
    explicit = raw.get("expected_artifacts")
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit]
    if isinstance(explicit, str) and explicit.strip():
        return [explicit.strip()]
    # 从项目绑定的领域动态获取
    try:
        return await get_phase_artifacts_for_project(project_id, phase_id)
    except Exception:
        return []


def expected_artifacts_for_phase(raw: dict, phase_id: str) -> list[str]:
    """同步版本：从模板 phase 配置解析期望产物文件列表（领域中立）。

    注意：此函数不从领域动态获取（同步上下文无法做 async DB 查询）。
    如需领域动态获取，请使用 expected_artifacts_for_phase_async。
    """
    explicit = raw.get("expected_artifacts")
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit]
    if isinstance(explicit, str) and explicit.strip():
        return [explicit.strip()]
    return []


def is_agent_fallback_artifact_name(name: str) -> bool:
    """{agent_id}.md 兜底文件 — 对话/工具中间态，非用户可见产物。"""
    base = Path(name or "").name
    if not base.lower().endswith(".md"):
        return False
    stem = base[:-3]
    return any(stem.startswith(p) for p in _AGENT_FALLBACK_MD_PREFIXES)


def is_user_facing_artifact(row: dict) -> bool:
    """是否应在 Workspace「产物」Tab 展示。"""
    atype = str(row.get("type") or "")
    meta = row.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError, ValueError):
            meta = {}
    if meta.get("source") == "phase_materialize" or atype == "materialized":
        return False

    file_path = str(row.get("file_path") or row.get("download_path") or "")
    name = Path(file_path).name or str(row.get("name") or "")

    if is_agent_fallback_artifact_name(name):
        return False

    # 领域中立：不再硬编码"已知交付文件名"白名单
    # 改为基于文件后缀和 metadata 判断

    suffix = Path(name).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm", ".mov"}:
        return True

    if atype in {"file", "vault"} and meta.get("source") != "phase_materialize":
        if suffix in PREVIEWABLE_SUFFIXES:
            return True

    return False
