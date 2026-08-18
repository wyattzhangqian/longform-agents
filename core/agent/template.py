"""Agent 模板系统 — 数据模型 + 注册中心

TemplateRegistry 是模板的数据访问层，管理 agent_templates 和 agent_temporary 两张表。
TemplateMatcher (在 core/orchestration/template_matcher.py) 负责匹配逻辑。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from core.storage.database import get_db

# ── 数据模型 ──────────────────────────────────────────────


class AgentTemplate(BaseModel):
    """Agent 模板 — 创建 Agent 实例的完整蓝图"""

    id: str
    name: str
    domain: str  # 领域短 key（如 web_novel；合法值由 quality_domains 数据决定）
    phase: str = ""
    emoji: str = "🤖"
    role: str = ""
    system_prompt: str = ""
    capability_tags: List[str] = Field(default_factory=list)
    recommended_model: str = "deepseek-v4-flash"
    temperature: float = 0.7
    max_tokens: int = 4096
    tool_ids: List[str] = Field(default_factory=list)
    skill_ids: List[str] = Field(default_factory=list)  # S2: 模板声明额外 skill 包
    max_tool_iterations: Optional[int] = None  # S4: 模板可配工具迭代上限（默认8，None=用默认）
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    source: str = "user"  # platform | user | community
    version: int = 1
    created_at: str = ""
    updated_at: str = ""

    class Config:
        extra = "forbid"


# ── 模板注册中心 ──────────────────────────────────────────


class TemplateRegistry:
    """Agent 模板注册中心 (单例模式)

    职责:
    - 管理 agent_templates 表的 CRUD
    - 管理 agent_temporary 表 (临时 Agent)
    - 提供 instantiate() 从模板创建 Agent dict
    - platform 模板不可编辑/不可删除
    """

    def __init__(self):
        self._cache: Dict[str, AgentTemplate] = {}

    # ═══════════════════════════════════════════════════════
    # CRUD — agent_templates
    # ═══════════════════════════════════════════════════════

    async def register(self, template: AgentTemplate) -> AgentTemplate:
        """注册/更新模板 (INSERT OR REPLACE)"""
        db = await get_db()
        now = datetime.now().isoformat()
        if not template.created_at:
            template.created_at = now
        template.updated_at = now

        row = self._to_row(template)
        await db.execute(
            """INSERT OR REPLACE INTO agent_templates
               (id, name, domain, phase, emoji, role, system_prompt,
                capability_tags, recommended_model, temperature, max_tokens,
                tool_ids, skill_ids, max_tool_iterations, input_schema, output_schema,
                source, version, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            list(row.values()),
        )
        self._cache[template.id] = template
        return template

    async def get(self, template_id: str) -> Optional[AgentTemplate]:
        """获取单个模板"""
        if template_id in self._cache:
            return self._cache[template_id]

        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        tmpl = self._row_to_template(dict(row))
        self._cache[tmpl.id] = tmpl
        return tmpl

    async def list(
        self,
        domain: Optional[str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[AgentTemplate]:
        """列表查询，支持按领域/来源/搜索过滤"""
        db = await get_db()
        clauses = ["1=1"]
        params: list = []

        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if search:
            clauses.append("(name LIKE ? OR role LIKE ? OR capability_tags LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])

        sql = f"SELECT * FROM agent_templates WHERE {' AND '.join(clauses)} ORDER BY domain, phase LIMIT ?"
        params.append(limit)

        cursor = await db.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        return [self._row_to_template(dict(r)) for r in rows]

    async def update(self, template_id: str, updates: dict) -> Optional[AgentTemplate]:
        """更新模板 (platform 模板不可编辑)"""
        existing = await self.get(template_id)
        if existing is None:
            return None
        if existing.source == "platform":
            raise ValueError("platform 模板不可编辑，请先克隆为用户模板")

        for k, v in updates.items():
            if k in ("id", "source", "created_at"):
                continue
            if hasattr(existing, k):
                setattr(existing, k, v)
        existing.version += 1
        existing.updated_at = datetime.now().isoformat()

        await self.register(existing)
        return existing

    async def remove(self, template_id: str) -> bool:
        """删除模板 (platform 模板不可删除)"""
        existing = await self.get(template_id)
        if existing is None:
            return False
        if existing.source == "platform":
            raise ValueError("platform 模板不可删除")

        db = await get_db()
        await db.execute("DELETE FROM agent_templates WHERE id = ?", (template_id,))
        self._cache.pop(template_id, None)
        return True

    async def count(self, domain: Optional[str] = None) -> int:
        """统计模板数量"""
        db = await get_db()
        if domain:
            cursor = await db.execute(
                "SELECT COUNT(*) AS cnt FROM agent_templates WHERE domain = ?",
                (domain,),
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) AS cnt FROM agent_templates")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def list_domains(self) -> List[str]:
        """列出所有已注册模板的领域"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT DISTINCT domain FROM agent_templates ORDER BY domain"
        )
        rows = await cursor.fetchall()
        return [r["domain"] for r in rows]

    # ═══════════════════════════════════════════════════════
    # 能力匹配辅助
    # ═══════════════════════════════════════════════════════

    async def search_by_capability(
        self,
        capability_tags: List[str],
        domain: Optional[str] = None,
        limit: int = 10,
    ) -> List[tuple]:
        """按 capability 标签精确匹配，返回 (template, hit_count) 列表"""
        templates = await self.list(domain=domain)
        scored: List[tuple] = []
        for tmpl in templates:
            hits = sum(1 for tag in capability_tags if tag in tmpl.capability_tags)
            if hits > 0:
                scored.append((tmpl, hits))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    # ═══════════════════════════════════════════════════════
    # 实例化 — 从模板创建 Agent
    # ═══════════════════════════════════════════════════════

    async def instantiate(
        self,
        template_id: str,
        custom_name: Optional[str] = None,
        overrides: Optional[dict] = None,
    ) -> dict:
        """从模板创建一个 Agent 实例，返回 AgentRegistry.register() 可用的 dict"""
        tmpl = await self.get(template_id)
        if tmpl is None:
            raise ValueError(f"模板 {template_id} 不存在")

        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        agent_dict = {
            "id": agent_id,
            "name": custom_name or tmpl.name,
            "type": "template",  # 模板实例：从 Agent 蓝图实例化的 Agent
            "status": "active",
            "emoji": tmpl.emoji,
            "role": tmpl.role,
            "capabilities": list(tmpl.capability_tags),
            "system_prompt": tmpl.system_prompt,
            "model": tmpl.recommended_model,
            "temperature": tmpl.temperature,
            "max_tokens": tmpl.max_tokens,
            # 2026-08-13：模板实例化必须带 thinking:disabled —— 写作/长文模板（32000 档）
            # 若开思维链，思维链与正文共享 max_tokens，预算被吃光 → 空产出/截断（实测）。
            "model_profile": {
                "llm": {
                    "model_id": tmpl.recommended_model,
                    "temperature": tmpl.temperature,
                    "max_tokens": tmpl.max_tokens,
                    "extra": {"thinking": "disabled"},
                },
                "image": None, "video": None, "music": None, "judge": None,
            },
            "tool_ids": list(tmpl.tool_ids),
            "input_schema": tmpl.input_schema,
            "output_schema": tmpl.output_schema,
            "skill_ids": list(set([
                "skill_platform_file_read",
                "skill_platform_file_write",
                "skill_platform_web_search",
            ] + list(getattr(tmpl, 'skill_ids', None) or []))),  # S2: 合并模板声明的 skill
            "version": "1.0.0",
            "extra": {
                "template_id": template_id,
                "template_source": tmpl.source,
                "domain": tmpl.domain,
                "phase": tmpl.phase,
                # S4: 透传模板声明的工具迭代上限（经 agents.extra → definition.extra → config）
                **({"max_tool_iterations": tmpl.max_tool_iterations} if tmpl.max_tool_iterations else {}),
            },
        }

        if overrides:
            for k, v in overrides.items():
                if k not in ("id", "type", "extra"):
                    agent_dict[k] = v

        return agent_dict

    # ═══════════════════════════════════════════════════════
    # 临时 Agent 管理
    # ═══════════════════════════════════════════════════════

    async def save_temporary_agent(self, temp: dict) -> str:
        """保存 LLM 即时生成的 Agent 到 agent_temporary 表，返回 temp_id"""
        temp_id = f"temp_{uuid.uuid4().hex[:12]}"
        expires = (datetime.now() + timedelta(days=7)).isoformat()

        db = await get_db()
        await db.execute(
            """INSERT INTO agent_temporary
               (id, project_id, subtask_title, name, emoji, role, system_prompt,
                capability_tags, recommended_model, temperature, max_tokens,
                match_score, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                temp_id,
                temp.get("project_id", ""),
                temp.get("subtask_title", ""),
                temp.get("name", ""),
                temp.get("emoji", "🤖"),
                temp.get("role", ""),
                temp.get("system_prompt", ""),
                json.dumps(temp.get("capability_tags", []), ensure_ascii=False),
                temp.get("recommended_model", "deepseek-v4-flash"),
                temp.get("temperature", 0.7),
                temp.get("max_tokens", 4096),
                temp.get("match_score"),
                datetime.now().isoformat(),
                expires,
            ),
        )
        return temp_id

    async def get_temporary(self, temp_id: str) -> Optional[dict]:
        """获取临时 Agent"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM agent_temporary WHERE id = ?", (temp_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def promote_temporary(self, temp_id: str) -> Optional[str]:
        """将临时 Agent 升级为 user 模板，返回新模板 ID"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM agent_temporary WHERE id = ?", (temp_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        rowd = dict(row)
        tmpl_id = f"tpl_user_{uuid.uuid4().hex[:8]}"

        # [修复] 从临时 Agent 的 capability_tags 推断 domain，而非硬编码
        tags = json.loads(rowd.get("capability_tags", "[]"))
        domain = _infer_domain_from_tags(tags) or ""

        tmpl = AgentTemplate(
            id=tmpl_id,
            name=rowd.get("name", ""),
            domain=domain,
            emoji=rowd.get("emoji", "🤖"),
            role=rowd.get("role", ""),
            system_prompt=rowd.get("system_prompt", ""),
            capability_tags=json.loads(rowd.get("capability_tags", "[]")),
            recommended_model=rowd.get("recommended_model", "deepseek-v4-flash"),
            temperature=rowd.get("temperature", 0.7),
            max_tokens=rowd.get("max_tokens", 4096),
            source="user",
        )
        await self.register(tmpl)

        # 回写关联
        await db.execute(
            "UPDATE agent_temporary SET saved_as_template_id = ? WHERE id = ?",
            (tmpl_id, temp_id),
        )
        return tmpl_id

    async def cleanup_expired_temporary(self) -> int:
        """清理过期且未保存的临时 Agent，返回清理数量"""
        db = await get_db()
        result = await db.execute(
            "DELETE FROM agent_temporary WHERE expires_at < datetime('now')"
            " AND saved_as_template_id IS NULL"
        )
        return result.rowcount if result else 0

    # ═══════════════════════════════════════════════════════
    # 内部工具
    # ═══════════════════════════════════════════════════════

    def _to_row(self, tmpl: AgentTemplate) -> dict:
        return {
            "id": tmpl.id,
            "name": tmpl.name,
            "domain": tmpl.domain,
            "phase": tmpl.phase,
            "emoji": tmpl.emoji,
            "role": tmpl.role,
            "system_prompt": tmpl.system_prompt,
            "capability_tags": json.dumps(tmpl.capability_tags, ensure_ascii=False),
            "recommended_model": tmpl.recommended_model,
            "temperature": tmpl.temperature,
            "max_tokens": tmpl.max_tokens,
            "tool_ids": json.dumps(tmpl.tool_ids),
            "skill_ids": json.dumps(tmpl.skill_ids, ensure_ascii=False),
            "max_tool_iterations": tmpl.max_tool_iterations,
            "input_schema": (
                json.dumps(tmpl.input_schema, ensure_ascii=False)
                if tmpl.input_schema
                else None
            ),
            "output_schema": (
                json.dumps(tmpl.output_schema, ensure_ascii=False)
                if tmpl.output_schema
                else None
            ),
            "source": tmpl.source,
            "version": tmpl.version,
            "created_at": tmpl.created_at,
            "updated_at": tmpl.updated_at,
        }

    def _row_to_template(self, row: dict) -> AgentTemplate:
        return AgentTemplate(
            id=row["id"],
            name=row["name"],
            domain=row["domain"],
            phase=row.get("phase", ""),
            emoji=row.get("emoji", "🤖"),
            role=row.get("role", ""),
            system_prompt=row.get("system_prompt", ""),
            capability_tags=json.loads(row.get("capability_tags", "[]")),
            recommended_model=row.get("recommended_model", "deepseek-v4-flash"),
            temperature=row.get("temperature", 0.7),
            max_tokens=row.get("max_tokens", 4096),
            tool_ids=json.loads(row.get("tool_ids", "[]")),
            skill_ids=json.loads(row.get("skill_ids", "[]") or "[]"),
            max_tool_iterations=row.get("max_tool_iterations"),
            input_schema=(
                json.loads(row["input_schema"]) if row.get("input_schema") else None
            ),
            output_schema=(
                json.loads(row["output_schema"]) if row.get("output_schema") else None
            ),
            source=row.get("source", "user"),
            version=row.get("version", 1),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )


# ── 单例访问 ──────────────────────────────────────────────

_registry: Optional[TemplateRegistry] = None


def get_template_registry() -> TemplateRegistry:
    """获取 TemplateRegistry 单例"""
    global _registry
    if _registry is None:
        _registry = TemplateRegistry()
    return _registry


def _infer_domain_from_tags(tags: list) -> str:
    """从 capability_tags 推断所属领域"""
    DOMAIN_KEYWORDS = {
        "web_novel": ["writing", "novel", "chapter", "plot", "character_design"],
        "comic_drama": ["comic", "storyboard", "panel", "visual_narrative"],
        "comic_static": ["illustration", "lineart", "coloring"],
        "music_production": ["lyrics", "melody", "chord", "arrangement", "mixing"],
        "research_report": ["research", "analysis", "data_collection", "report"],
    }
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(kw in tag for tag in tags for kw in keywords):
            return domain
    return ""
