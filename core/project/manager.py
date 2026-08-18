"""ProjectManager — 项目 CRUD（通用层）"""

from __future__ import annotations
import json
import uuid
from typing import Optional, List
from core.storage.database import get_db
from core.agent.types import Project, ProjectConfig, ProjectAgent


class ProjectManager:
    """项目持久化管理"""

    @staticmethod
    async def get(project_id: str) -> Optional[Project]:
        db = await get_db()
        cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return await ProjectManager._row_to_project(dict(row))

    @staticmethod
    async def create(
        name: str,
        project_id: str = "",
        project_type: str = "custom",
        mode: str = "sequential",
        config: ProjectConfig = None,
        template_id: str = "",
        emoji: str = "📦",
    ) -> Project:
        from core.auth import generate_project_access_token

        pid = project_id or f"proj_{uuid.uuid4().hex[:12]}"
        cfg = config or ProjectConfig()
        access_token = generate_project_access_token()
        db = await get_db()
        await db.execute(
            """INSERT INTO projects (id, name, type, status, emoji, mode, config, template_id, access_token)
               VALUES (?, ?, ?, 'idle', ?, ?, ?, ?, ?)""",
            (pid, name, project_type, emoji, mode, json.dumps(cfg.model_dump(), ensure_ascii=False),
             template_id or None, access_token),
        )
        await db.commit()
        project = await ProjectManager.get(pid)
        project._access_token = access_token  # type: ignore[attr-defined]
        return project

    @staticmethod
    async def get_access_token(project_id: str) -> Optional[str]:
        db = await get_db()
        cursor = await db.execute(
            "SELECT access_token FROM projects WHERE id = ?",
            (project_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return str(row[0])
        return None

    @staticmethod
    async def regenerate_access_token(project_id: str) -> Optional[str]:
        from core.auth import generate_project_access_token

        token = generate_project_access_token()
        db = await get_db()
        cursor = await db.execute(
            "UPDATE projects SET access_token = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (token, project_id),
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
        return token

    @staticmethod
    async def list(
        status: str = None,
        project_type: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Project], int]:
        """列出项目"""
        db = await get_db()
        sql = "SELECT * FROM projects WHERE 1=1"
        count_sql = "SELECT COUNT(*) as cnt FROM projects WHERE 1=1"
        params: list = []

        if status:
            sql += " AND status = ?"
            count_sql += " AND status = ?"
            params.append(status)
        else:
            # B1 默认排除已归档项目
            sql += " AND status != 'archived'"
            count_sql += " AND status != 'archived'"
        if project_type:
            sql += " AND type = ?"
            count_sql += " AND type = ?"
            params.append(project_type)

        cursor = await db.execute(count_sql, params)
        total = (await cursor.fetchone())["cnt"]

        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        cursor = await db.execute(sql, params + [limit, offset])
        projects = [await ProjectManager._row_to_project(dict(r)) for r in await cursor.fetchall()]
        return projects, total

    @staticmethod
    async def update(project_id: str, updates: dict) -> Optional[Project]:
        """更新项目"""
        existing = await ProjectManager.get(project_id)
        if not existing:
            return None

        name = updates.get("name", existing.name)
        project_type = updates.get("type", existing.type)
        emoji = updates.get("emoji", existing.emoji)
        mode = updates.get("mode", existing.mode)
        status = updates.get("status", existing.status)
        template_id = updates.get("template_id", existing.template_id)

        config = existing.config.model_dump()
        if "config" in updates and isinstance(updates["config"], dict):
            config.update(updates["config"])
        if "task_description" in updates:
            config["task_description"] = updates["task_description"]

        db = await get_db()
        await db.execute(
            """UPDATE projects SET name=?, type=?, emoji=?, mode=?, status=?,
               config=?, template_id=?, updated_at=datetime('now', 'localtime') WHERE id=?""",
            (name, project_type, emoji, mode, status,
             json.dumps(config, ensure_ascii=False), template_id, project_id),
        )
        await db.commit()
        return await ProjectManager.get(project_id)

    @staticmethod
    async def delete(project_id: str) -> bool:
        db = await get_db()
        cursor = await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await db.commit()
        return cursor.rowcount > 0

    @staticmethod
    async def add_agent(project_id: str, agent_id: str, role: str = "", join_order: int = None) -> bool:
        project = await ProjectManager.get(project_id)
        if not project:
            return False
        db = await get_db()
        order = join_order if join_order is not None else len(project.agents)
        await db.execute(
            """INSERT INTO project_agents (project_id, agent_id, join_order, role, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (project_id, agent_id, order, role),
        )
        await db.commit()
        return True

    @staticmethod
    async def remove_agent(project_id: str, agent_id: str) -> bool:
        db = await get_db()
        cursor = await db.execute(
            "DELETE FROM project_agents WHERE project_id = ? AND agent_id = ?",
            (project_id, agent_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    @staticmethod
    async def update_status(project_id: str, status: str) -> None:
        db = await get_db()
        await db.execute(
            "UPDATE projects SET status = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (status, project_id),
        )
        await db.commit()

    @staticmethod
    async def set_agents(project_id: str, agent_ids: List[str]) -> None:
        db = await get_db()
        await db.execute("DELETE FROM project_agents WHERE project_id = ?", (project_id,))
        for order, agent_id in enumerate(agent_ids):
            await db.execute(
                """INSERT INTO project_agents (project_id, agent_id, join_order, status)
                   VALUES (?, ?, ?, 'active')""",
                (project_id, agent_id, order),
            )
        await db.commit()

    @staticmethod
    async def assign_agents(
        project_id: str,
        top_n: int = 10,
    ) -> List[dict]:
        """智能匹配 Agent：基于项目任务描述和 Agent 能力标签做关键词匹配

        Args:
            project_id: 项目 ID
            top_n: 返回前 N 个匹配的 Agent

        Returns:
            [{agent_id, match_score, agent_name}, ...] 按匹配度降序
        """
        project = await ProjectManager.get(project_id)
        if not project:
            return []

        task_desc = project.config.extra.get("task_description", "") if hasattr(project.config, "extra") else ""
        task_desc = str(task_desc).lower()

        if not task_desc:
            return []

        # 从数据库获取所有 active Agent 及其 capabilities
        db = await get_db()
        cursor = await db.execute(
            """SELECT id, name, capabilities FROM agents WHERE status = 'active'""",
        )
        all_agents = await cursor.fetchall()

        # 分词：简单按空格和标点拆分任务描述
        task_tokens = set()
        for token in task_desc.replace(",", " ").replace("，", " ").replace(".", " ").split():
            if len(token) >= 2:
                task_tokens.add(token)

        results = []
        for row in all_agents:
            agent_id = row["id"]
            agent_name = row["name"]
            try:
                caps = json.loads(row.get("capabilities") or "[]")
            except (json.JSONDecodeError, TypeError):
                caps = []
            cap_tokens = set()
            for cap in caps:
                for t in str(cap).lower().replace(" ", " ").split():
                    if len(t) >= 2:
                        cap_tokens.add(t)

            # Jaccard 系数
            if not task_tokens and not cap_tokens:
                continue
            intersection = task_tokens & cap_tokens
            union = task_tokens | cap_tokens
            score = len(intersection) / len(union) if union else 0.0

            if score > 0:
                results.append({
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "match_score": round(score, 4),
                })

        results.sort(key=lambda x: x["match_score"], reverse=True)
        return results[:top_n]

    @staticmethod
    async def _row_to_project(row: dict) -> Project:
        config_raw = json.loads(row.get("config") or "{}")
        extra = {k: v for k, v in config_raw.items() if k not in ProjectConfig.model_fields}
        core = {k: v for k, v in config_raw.items() if k in ProjectConfig.model_fields}
        if "extra" not in core:
            core["extra"] = extra
        else:
            core["extra"] = {**core.get("extra", {}), **extra}

        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM project_agents WHERE project_id = ? ORDER BY join_order",
            (row["id"],),
        )
        agents = [
            ProjectAgent(
                id=rd["id"],
                project_id=rd["project_id"],
                agent_id=rd["agent_id"],
                join_order=rd["join_order"],
                role=rd.get("role") or "",
                status=rd.get("status") or "active",
            )
            for r in await cursor.fetchall()
            for rd in [dict(r)]
        ]

        return Project(
            id=row["id"],
            name=row["name"],
            type=row.get("type") or "custom",
            status=row.get("status") or "idle",
            emoji=row.get("emoji") or "📦",
            mode=row.get("mode") or "sequential",
            config=ProjectConfig(**core),
            template_id=row.get("template_id"),
            agents=agents,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
