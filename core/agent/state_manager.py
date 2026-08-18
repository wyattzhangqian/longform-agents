"""StateManager — 工作流状态持久化管理

提供 checkpoint 的保存/加载/删除，通过 SQLite projects 表的 checkpoint_state 列存储。
支持：
- 每个 project_id 保存一个 checkpoint 快照
- checkpoint_version 乐观锁
- JSON 序列化/反序列化
- 幂等操作（不存在时静默处理）
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional

from core.storage.database import get_db, get_read_db
from core.agent.state import WorkflowState
from core.logging import get_logger

_logger = get_logger("state_manager")


class CheckpointConflictError(Exception):
    """Checkpoint 版本冲突 — 并发写入或 expected_version 不匹配"""

    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(f"Checkpoint 版本冲突: expected={expected}, actual={actual}")


class StateManager:
    """工作流状态持久化管理器"""

    # P1-10: 每项目保留的 checkpoint 历史条数上限（超出裁剪最旧）
    _CHECKPOINT_HISTORY_LIMIT = 100

    @staticmethod
    async def get_checkpoint_version(project_id: str) -> int:
        try:
            db = await get_read_db()
            cursor = await db.execute(
                "SELECT checkpoint_version FROM projects WHERE id = ?",
                (project_id,),
            )
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except Exception as e:
            _logger.debug("读取 checkpoint_version 失败: %s", e)
        return 0

    @staticmethod
    async def save_run_context(
        project_id: str,
        ctx: "RunContext",
        expected_version: Optional[int] = None,
    ) -> int:
        """保存 RunContext（D2 — checkpoint 唯一真相）"""
        from core.run.run_context import RunContext

        if not isinstance(ctx, RunContext):
            ctx = RunContext.model_validate(ctx)
        ws = ctx.to_workflow_state()
        new_version = await StateManager.save_checkpoint(project_id, ws, expected_version)
        ctx.checkpoint_version = new_version
        # P1-10: 追加 checkpoint 历史（run_checkpoints 表），供回放基准与审计
        await StateManager._append_checkpoint_history(project_id, ws, ctx, new_version)
        return new_version

    @staticmethod
    async def _append_checkpoint_history(
        project_id: str,
        ws: WorkflowState,
        ctx,
        version: int,
    ) -> None:
        """P1-10: 写入一条 immutable checkpoint 历史行，并裁剪超限旧行（保留最近 N 条/项目）。

        非致命：历史写入失败不阻塞 checkpoint 主流程（单槽仍是唯一真相）。
        """
        try:
            db = await get_db()
            run_id = str(getattr(ctx, "run_id", "") or "")
            termination = ""
            control = getattr(ctx, "control", None)
            if control is not None:
                termination = str(getattr(control, "termination_reason", "") or "")
            await db.execute(
                """INSERT INTO run_checkpoints
                   (project_id, run_id, version, checkpoint_state, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, run_id, version, ws.dump_json(), termination or "running"),
            )
            # 保留最近 _CHECKPOINT_HISTORY_LIMIT 条/项目
            cursor = await db.execute(
                """SELECT id FROM run_checkpoints
                   WHERE project_id = ? ORDER BY version DESC LIMIT 1 OFFSET ?""",
                (project_id, StateManager._CHECKPOINT_HISTORY_LIMIT),
            )
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    "DELETE FROM run_checkpoints WHERE project_id = ? AND id < ?",
                    (project_id, row[0]),
                )
            await db.commit()
        except Exception as e:
            _logger.warning("checkpoint 历史写入失败（非致命）: {}", e)

    @staticmethod
    async def list_run_checkpoints(project_id: str, limit: int = 50) -> list:
        """P1-10: 列出项目的 checkpoint 历史（id/run_id/version/status/created_at）。"""
        db = await get_read_db()
        cursor = await db.execute(
            """SELECT id, run_id, version, status, created_at
               FROM run_checkpoints WHERE project_id = ?
               ORDER BY version DESC LIMIT ?""",
            (project_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "version": r["version"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @staticmethod
    async def load_run_checkpoint_by_run(project_id: str, run_id: str):
        """P1-10: 按 run_id 取该次 run 的最新历史 checkpoint 恢复 RunContext。

        用于 replay 的 run_id 维度：回放到指定 run 的基线。
        """
        db = await get_read_db()
        cursor = await db.execute(
            """SELECT id FROM run_checkpoints
               WHERE project_id = ? AND run_id = ?
               ORDER BY version DESC LIMIT 1""",
            (project_id, run_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return await StateManager.load_run_checkpoint(project_id, row["id"])

    @staticmethod
    async def load_run_checkpoint(project_id: str, checkpoint_id: int):
        """P1-10: 从历史 checkpoint 恢复 RunContext（replay 基准，不覆盖单槽）。"""
        db = await get_read_db()
        cursor = await db.execute(
            "SELECT checkpoint_state FROM run_checkpoints WHERE id = ? AND project_id = ?",
            (checkpoint_id, project_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            ws = WorkflowState.model_validate_json(row["checkpoint_state"])
            from core.run.run_context import RunContext
            return RunContext.from_workflow_state(ws)
        except Exception as e:
            _logger.warning("历史 checkpoint 解析失败 id={}: {}", checkpoint_id, e)
            return None

    @staticmethod
    async def load_run_context(project_id: str) -> Optional["RunContext"]:
        """从 checkpoint 恢复 RunContext；旧格式自动迁移 (D2)"""
        from core.run.run_context import RunContext
        from core.run.checkpoint_migrate import (
            build_run_context_from_legacy,
            needs_run_context_migration,
        )

        ws = await StateManager.load_checkpoint(project_id)
        if not ws:
            return None

        snap = ws.config_snapshot or {}
        if "_run_context" in snap:
            return RunContext.from_workflow_state(ws)

        ctx = build_run_context_from_legacy(ws, project_id)
        try:
            await StateManager.save_run_context(project_id, ctx, expected_version=ws.checkpoint_version)
        except Exception as e:
            _logger.warning("旧 checkpoint 自动迁移写回失败（仍返回内存 RunContext）: %s", e)
        return ctx

    @staticmethod
    async def save_checkpoint(
        project_id: str,
        state: WorkflowState,
        expected_version: Optional[int] = None,
    ) -> int:
        """保存状态快照到数据库（乐观锁）

        Args:
            project_id: 项目 ID
            state: 要保存的 WorkflowState
            expected_version: 期望的当前版本；None 时不校验（自动递增）

        Returns:
            新的 checkpoint_version

        Raises:
            CheckpointConflictError: 版本不匹配或并发写入失败
        """
        from core.observability.metrics import metrics

        try:
            db = await get_db()
            cursor = await db.execute(
                "SELECT checkpoint_version FROM projects WHERE id = ?",
                (project_id,),
            )
            row = await cursor.fetchone()
            current = int(row[0] if row and row[0] is not None else 0)

            if expected_version is not None and expected_version != current:
                metrics.checkpoint_saved(ok=False)
                raise CheckpointConflictError(expected_version, current)

            new_version = current + 1
            state.checkpoint_version = new_version
            state.updated_at = datetime.now().isoformat()
            json_str = state.dump_json()

            # #7 事务保护：确保 UPDATE + commit 原子化，commit 失败时 rollback
            await db.execute("BEGIN")
            try:
                cursor = await db.execute(
                    """UPDATE projects
                       SET checkpoint_state = ?, checkpoint_version = ?, updated_at = ?
                       WHERE id = ? AND checkpoint_version = ?""",
                    (json_str, new_version, state.updated_at, project_id, current),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

            if cursor.rowcount == 0:
                actual = await StateManager.get_checkpoint_version(project_id)
                metrics.checkpoint_saved(ok=False)
                raise CheckpointConflictError(current, actual)

            metrics.checkpoint_saved(ok=True)
            return new_version
        except CheckpointConflictError:
            raise
        except Exception as e:
            _logger.warning("保存 checkpoint 失败: %s", e)
            metrics.checkpoint_saved(ok=False)
            return state.checkpoint_version

    @staticmethod
    async def load_checkpoint(project_id: str) -> Optional[WorkflowState]:
        """从数据库加载状态快照"""
        try:
            db = await get_read_db()
            cursor = await db.execute(
                "SELECT checkpoint_state, checkpoint_version FROM projects WHERE id = ?",
                (project_id,),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                state = WorkflowState.load_json(row[0])
                if row[1] is not None:
                    state.checkpoint_version = int(row[1])
                return state
        except Exception as e:
            _logger.warning("加载 checkpoint 失败: %s", e)
        return None

    @staticmethod
    async def delete_checkpoint(project_id: str) -> bool:
        """清除项目的状态快照"""
        try:
            db = await get_db()
            await db.execute(
                "UPDATE projects SET checkpoint_state = '', checkpoint_version = 0 WHERE id = ?",
                (project_id,),
            )
            await db.commit()
            return True
        except Exception as e:
            _logger.warning("删除 checkpoint 失败: %s", e)
            return False

    @staticmethod
    async def has_checkpoint(project_id: str) -> bool:
        """检查项目是否有保存的状态快照"""
        try:
            db = await get_read_db()
            cursor = await db.execute(
                "SELECT checkpoint_state FROM projects WHERE id = ?",
                (project_id,),
            )
            row = await cursor.fetchone()
            return row is not None and row[0] and len(row[0]) > 10
        except Exception:
            return False

    @staticmethod
    async def get_checkpoint_info(project_id: str) -> Optional[dict]:
        """返回 checkpoint 元信息（不含完整 state）"""
        try:
            db = await get_read_db()
            cursor = await db.execute(
                """SELECT checkpoint_version, updated_at, status,
                          LENGTH(checkpoint_state) as state_size
                   FROM projects WHERE id = ?""",
                (project_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            has_state = bool(row["state_size"] and row["state_size"] > 10)
            return {
                "project_id": project_id,
                "checkpoint_version": int(row["checkpoint_version"] or 0),
                "updated_at": row["updated_at"],
                "status": row["status"],
                "has_checkpoint": has_state,
            }
        except Exception as e:
            _logger.debug("get_checkpoint_info 失败: %s", e)
            return None

    @staticmethod
    async def partial_reset(project_id: str, node_ids: set) -> None:
        """部分重置：从 RunContext 中删除指定节点的状态，保留其余节点。

        用于 checkpoint replay — 重置目标及下游节点，保留上游输出。
        """
        ctx = await StateManager.load_run_context(project_id)
        if not ctx:
            _logger.warning("partial_reset: 无 RunContext for %s", project_id)
            return

        changed = False
        for node_id in node_ids:
            # 从 agent_outputs 中删除
            if node_id in ctx.agent_outputs:
                del ctx.agent_outputs[node_id]
                changed = True
            # 从 completed_phase_ids 中删除
            if node_id in ctx.completed_phase_ids:
                ctx.completed_phase_ids.remove(node_id)
                changed = True

        # 清除相关质量记录
        ctx.quality_records = [
            r for r in ctx.quality_records
            if r.phase_id not in node_ids
        ]

        # 重置控制状态
        ctx.control.pending_human = False
        ctx.control.pause_reason = ""
        ctx.control.pending_decision_id = None
        ctx.control.termination_reason = ""

        ctx.touch()

        if changed:
            await StateManager.save_run_context(project_id, ctx)
            _logger.info(
                "partial_reset: project=%s reset_nodes=%s",
                project_id, list(node_ids),
            )
