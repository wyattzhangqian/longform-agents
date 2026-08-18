"""AgentRegistry — Agent 注册中心

职责：
- Agent CRUD（创建/读取/更新/删除）
- 运行时实例化
- 项目类型推荐
- 兼容性检查
- 进化历史 & 性能指标查询
"""

from __future__ import annotations
import json
from typing import Optional, List, Dict, Any
from core.storage.database import get_db
from core.agent.types import (
    AgentDefinition, AgentTool, InteractionConfig, AgentRuntimeInfo,
    MemoryConfig, PerformanceMetrics, EvolutionEvent,
)
from core.models.types import AgentModelProfile
from config import normalize_llm_model


def _safe_json(raw: Any, default: Any = None) -> Any:
    """安全 JSON 解析：脏数据返回 default 而非抛异常"""
    if not raw:
        return default if default is not None else None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default if default is not None else None


def _serialize_model_profile(mp: Any) -> dict:
    """将 model_profile 序列化为可 JSON 的 dict"""
    if mp is None:
        return {}
    if isinstance(mp, AgentModelProfile):
        return mp.model_dump()
    if isinstance(mp, dict):
        return mp
    return {}


class AgentRegistry:
    """Agent 注册中心 — 所有 Agent 的唯一真相源"""

    def __init__(self):
        self._instances: Dict[str, Any] = {}  # 运行时实例缓存
        self._agent_classes: Dict[str, type] = {}  # agent_id → Agent 类

    # ==================== CRUD ====================

    async def register(self, definition: dict | AgentDefinition) -> AgentDefinition:
        """注册新 Agent"""
        if isinstance(definition, AgentDefinition):
            def_dict = definition.model_dump()
        else:
            def_dict = definition

        agent_id = def_dict.get("id") or f"agent_{self._ts()}"
        def_dict["id"] = agent_id

        db = await get_db()
        await db.execute("""
            INSERT INTO agents (id, name, type, status, emoji, role,
                capabilities, current_version, creator, model,
                specialized_knowledge, reasoning_engine,
                temperature, max_tokens,
                memory_config, tools, tool_ids, skill_ids, model_profile, interaction_config,
                system_prompt, peers, evolution_history, performance_metrics,
                input_schema, output_schema, tool_namespaces, runtime_info, extra)
            VALUES (:id, :name, :type, :status, :emoji, :role,
                :capabilities, :version, :creator, :model,
                :specialized_knowledge, :reasoning_engine,
                :temperature, :max_tokens,
                :memory_config, :tools, :tool_ids, :skill_ids, :model_profile, :interaction_config,
                :system_prompt, :peers, :evolution_history, :performance_metrics,
                :input_schema, :output_schema, :tool_namespaces, :runtime_info, :extra)
        """, self._to_row(def_dict))
        await db.commit()
        return await self.get(agent_id)

    async def get(self, agent_id: str) -> Optional[AgentDefinition]:
        """获取 Agent 定义"""
        db = await get_db()
        cursor = await db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_definition(dict(row))

    async def list(self, filters: Optional[dict] = None) -> List[AgentDefinition]:
        """列出所有 Agent"""
        filters = filters or {}
        sql = "SELECT * FROM agents WHERE 1=1"
        params: list = []

        if filters.get("type"):
            sql += " AND type = ?"
            params.append(filters["type"])
        if filters.get("status"):
            sql += " AND status = ?"
            params.append(filters["status"])
        if filters.get("search"):
            sql += " AND (name LIKE ? OR role LIKE ?)"
            params.extend([f"%{filters['search']}%", f"%{filters['search']}%"])

        sql += " ORDER BY type, name"

        db = await get_db()
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_definition(dict(r)) for r in rows]

    async def update(self, agent_id: str, updates: dict) -> Optional[AgentDefinition]:
        """更新 Agent"""
        existing = await self.get(agent_id)
        if not existing:
            return None

        merged = {**existing.model_dump(), **updates}

        db = await get_db()
        await db.execute("""
            UPDATE agents SET
                name = :name, type = :type, status = :status, role = :role, emoji = :emoji,
                capabilities = :capabilities, current_version = :version,
                model = :model, specialized_knowledge = :specialized_knowledge,
                reasoning_engine = :reasoning_engine,
                temperature = :temperature, max_tokens = :max_tokens,
                memory_config = :memory_config, tools = :tools, tool_ids = :tool_ids,
                skill_ids = :skill_ids, model_profile = :model_profile,
                interaction_config = :interaction_config,
                system_prompt = :system_prompt, peers = :peers,
                evolution_history = :evolution_history,
                performance_metrics = :performance_metrics,
                input_schema = :input_schema,
                output_schema = :output_schema,
                tool_namespaces = :tool_namespaces,
                runtime_info = :runtime_info,
                extra = :extra
            WHERE id = :id
        """, {
            "id": agent_id,
            "name": merged.get("name", ""),
            "type": merged.get("type", "custom"),
            "status": merged.get("status", "active"),
            "role": merged.get("role", ""),
            "emoji": merged.get("emoji", "🤖"),
            "capabilities": json.dumps(merged.get("capabilities", []), ensure_ascii=False),
            "version": merged.get("version", "1.0.0"),
            "model": normalize_llm_model(merged.get("model")),
            "specialized_knowledge": json.dumps(merged.get("specialized_knowledge", [])),
            "reasoning_engine": merged.get("reasoning_engine", "chain-of-thought"),
            "temperature": merged.get("temperature", 0.7),
            "max_tokens": merged.get("max_tokens", 4096),
            "memory_config": json.dumps(merged.get("memory_config", {}), ensure_ascii=False),
            "tools": json.dumps(
                [t if isinstance(t, dict) else t.model_dump() for t in merged.get("tools", [])],
                ensure_ascii=False
            ),
            "tool_ids": json.dumps(merged.get("tool_ids", []), ensure_ascii=False),
            "skill_ids": json.dumps(merged.get("skill_ids", []), ensure_ascii=False),
            "model_profile": json.dumps(_serialize_model_profile(merged.get("model_profile")), ensure_ascii=False),
            "interaction_config": json.dumps(
                merged.get("interaction_config", {}) if isinstance(merged.get("interaction_config"), dict)
                else merged["interaction_config"].model_dump(),
                ensure_ascii=False
            ),
            "system_prompt": merged.get("system_prompt", ""),
            "peers": json.dumps(merged.get("peers", []), ensure_ascii=False),
            "evolution_history": json.dumps(
                merged.get("evolution_history", []) if isinstance(merged.get("evolution_history"), list)
                else [e.model_dump() for e in merged.get("evolution_history", [])],
                ensure_ascii=False
            ),
            "performance_metrics": json.dumps(
                merged.get("performance_metrics", {}) if isinstance(merged.get("performance_metrics"), dict)
                else merged["performance_metrics"].model_dump(),
                ensure_ascii=False
            ),
            "input_schema": json.dumps(merged.get("input_schema"), ensure_ascii=False) if merged.get("input_schema") else None,
            "output_schema": json.dumps(merged.get("output_schema"), ensure_ascii=False) if merged.get("output_schema") else None,
            "tool_namespaces": json.dumps(merged.get("tool_namespaces", []), ensure_ascii=False),
            "runtime_info": json.dumps(
                merged.get("runtime_info", {}) if isinstance(merged.get("runtime_info"), dict)
                else merged["runtime_info"].model_dump() if hasattr(merged.get("runtime_info"), "model_dump") else {},
                ensure_ascii=False,
            ),
            "extra": json.dumps(
                merged.get("extra", {}) if isinstance(merged.get("extra"), dict) else {},
                ensure_ascii=False,
            ),
        })
        await db.commit()
        self._instances.pop(agent_id, None)
        return await self.get(agent_id)

    async def remove(self, agent_id: str) -> bool:
        """删除 Agent"""
        db = await get_db()
        await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await db.commit()
        self._instances.pop(agent_id, None)
        return True

    async def toggle_status(self, agent_id: str, status: str) -> bool:
        """切换 Agent 状态"""
        db = await get_db()
        await db.execute("UPDATE agents SET status = ? WHERE id = ?", (status, agent_id))
        await db.commit()
        return True

    # ==================== 模板系统 ====================

    async def suggest(self, project_type: str) -> dict:
        """根据项目类型推荐 Agent 组合（item5：preset_templates 废弃 → plans）"""
        db = await get_db()
        # 从 plans（统一模板体系）查匹配类型的计划，提取 phases 中的 agent_ids
        cursor = await db.execute(
            "SELECT id, name, tags, phases, created_at FROM plans "
            "WHERE tags LIKE ? ORDER BY use_count DESC LIMIT 20",
            (f"%{project_type}%",)
        )
        template_rows = await cursor.fetchall()
        templates = []
        for r in template_rows:
            phases = json.loads(r["phases"] or "[]")
            agent_ids = [p.get("agent_id") for p in phases if p.get("agent_id")]
            templates.append({
                "id": r["id"],
                "name": r["name"] or r["id"],
                "emoji": "📋",
                "description": "",
                "tags": json.loads(r["tags"] or "[]"),
                "agent_ids": agent_ids,
                "workflow_config": {},
                "is_default": False,
                "created_at": r.get("created_at") or "",
            })

        # 获取活跃 Agent（CapabilityDiscovery 排序）
        agents = await self.list({"status": "active"})
        from core.discovery.capability import CapabilityDiscovery
        ranked = CapabilityDiscovery(agents).discover(
            tags=[project_type] if project_type else None,
            limit=50,
        )
        suggested = [
            {
                "id": r["id"],
                "name": r["name"],
                "role": r["role"],
                "emoji": r["emoji"],
                "type": r["type"],
                "capabilities": r["capabilities"],
                "score": r["score"],
            }
            for r in ranked
        ]

        return {"templates": templates, "suggestedAgents": suggested}

    # ==================== DomainAdapter 注册 ====================

    def register_class(self, agent_id: str, agent_cls: type):
        """注册 Agent 类（由 DomainAdapter 调用）

        引擎实例化 Agent 时优先从 _agent_classes 查找领域类，
        找不到则使用 BaseAgent 基类作为 fallback。
        """
        self._agent_classes[agent_id] = agent_cls

    def get_class(self, agent_id: str) -> Optional[type]:
        """获取已注册的 Agent 类"""
        return self._agent_classes.get(agent_id)

    # ==================== 兼容性检查 ====================

    async def check_compatibility(self, agent_id: str, target_id: str) -> dict:
        """检查两个 Agent 的兼容性"""
        agent = await self.get(agent_id)
        target = await self.get(target_id)
        if not agent or not target:
            return {"compatible": False, "reason": "Agent 不存在"}

        # 检查 peers 白名单
        if agent.peers and target_id not in agent.peers:
            return {"compatible": False, "reason": f"{agent.name} 的协作白名单中不包含 {target.name}"}

        # 检查能力标签交集
        shared = [c for c in agent.capabilities if c in target.capabilities]
        if not shared:
            return {"compatible": False, "reason": "无共同能力标签", "shared_capabilities": []}

        return {"compatible": True, "reason": f"共享能力: {', '.join(shared)}", "shared_capabilities": shared}

    # ==================== 进化与性能 ====================

    async def get_evolution_history(self, agent_id: str) -> Optional[dict]:
        """获取 Agent 进化历史"""
        agent = await self.get(agent_id)
        if not agent:
            return None

        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM agent_learnings WHERE agent_id = ? ORDER BY created_at DESC LIMIT 50",
            (agent_id,)
        )
        learnings = await cursor.fetchall()

        return {
            "agent": {"id": agent.id, "name": agent.name, "role": agent.role},
            "evolution_events": [e.model_dump() for e in agent.evolution_history],
            "learning_records": [
                {
                    "id": r["id"],
                    "project_id": r["project_id"],
                    "knowledge_gained": r["knowledge_gained"],
                    "capability_scores_after": json.loads(r["capability_scores_after"] or "{}"),
                    "created_at": r["created_at"],
                }
                for r in learnings
            ],
            "current_metrics": agent.performance_metrics.model_dump(),
        }

    async def get_performance_metrics(self, agent_id: str) -> Optional[dict]:
        """获取 Agent 性能指标"""
        agent = await self.get(agent_id)
        if not agent:
            return None

        db = await get_db()
        cursor = await db.execute(
            "SELECT memory_type, COUNT(*) as count FROM agent_memories WHERE agent_id = ? GROUP BY memory_type",
            (agent_id,)
        )
        mem_stats = {r["memory_type"]: r["count"] for r in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM agent_learnings WHERE agent_id = ?", (agent_id,)
        )
        learn_count = (await cursor.fetchone())["cnt"]

        return {
            "agent_id": agent_id,
            "name": agent.name,
            "role": agent.role,
            "version": agent.version,
            "metrics": agent.performance_metrics.model_dump(),
            "memory_stats": mem_stats,
            "total_learnings": learn_count,
            "evolution_events": len(agent.evolution_history),
        }

    # ==================== 内部工具 ====================

    def _row_to_definition(self, row: dict) -> AgentDefinition:
        from core.models.types import AgentModelProfile, PLATFORM_DEFAULT

        raw_profile = row.get("model_profile")
        if raw_profile:
            try:
                profile = AgentModelProfile.model_validate(json.loads(raw_profile))
            except Exception:
                profile = AgentModelProfile.from_legacy(
                    row.get("model"), row.get("temperature"), row.get("max_tokens"),
                )
        else:
            profile = AgentModelProfile.from_legacy(
                row.get("model"), row.get("temperature"), row.get("max_tokens"),
            )

        legacy_model = normalize_llm_model(row.get("model"))
        if profile.llm.model_id == PLATFORM_DEFAULT and legacy_model:
            profile.llm.model_id = legacy_model
        if profile.llm.temperature is None and row.get("temperature") is not None:
            profile.llm.temperature = row.get("temperature")
        if profile.llm.max_tokens is None and row.get("max_tokens") is not None:
            profile.llm.max_tokens = row.get("max_tokens")

        return AgentDefinition(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            status=row["status"],
            emoji=row["emoji"] or "🤖",
            role=row.get("role") or "",
            capabilities=json.loads(row.get("capabilities") or "[]"),
            version=row.get("current_version") or "1.0.0",
            creator=row.get("creator") or "user",
            model=legacy_model,
            specialized_knowledge=json.loads(row.get("specialized_knowledge") or "[]"),
            reasoning_engine=row.get("reasoning_engine") or "chain-of-thought",
            temperature=profile.llm.temperature if profile.llm.temperature is not None else 0.7,
            max_tokens=profile.llm.max_tokens if profile.llm.max_tokens is not None else 4096,
            model_profile=profile,
            input_schema=_safe_json(row.get("input_schema")),
            output_schema=_safe_json(row.get("output_schema")),
            tool_namespaces=_safe_json(row.get("tool_namespaces"), []),
            memory_config=MemoryConfig(**_safe_json(row.get("memory_config"), {})),
            tools=[
                AgentTool(**t) if isinstance(t, dict) else t
                for t in _safe_json(row.get("tools"), [])
            ],
            tool_ids=_safe_json(row.get("tool_ids"), []),
            skill_ids=_safe_json(row.get("skill_ids"), []),
            interaction_config=InteractionConfig(**_safe_json(row.get("interaction_config"), {})),
            runtime_info=AgentRuntimeInfo(**_safe_json(row.get("runtime_info"), {})),
            system_prompt=row.get("system_prompt") or "",
            peers=_safe_json(row.get("peers"), []),
            evolution_history=[
                EvolutionEvent(**e) if isinstance(e, dict) else e
                for e in _safe_json(row.get("evolution_history"), [])
            ],
            performance_metrics=PerformanceMetrics(**_safe_json(row.get("performance_metrics"), {})),
            extra=_safe_json(row.get("extra"), {}),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def _to_row(self, definition: dict) -> dict:
        """将 Agent 定义转为数据库行"""
        from core.models.types import AgentModelProfile, PLATFORM_DEFAULT

        profile_raw = definition.get("model_profile")
        if isinstance(profile_raw, AgentModelProfile):
            profile = profile_raw
        elif isinstance(profile_raw, dict):
            profile = AgentModelProfile.model_validate(profile_raw)
        else:
            profile = AgentModelProfile.from_legacy(
                definition.get("model"),
                definition.get("temperature"),
                definition.get("max_tokens"),
            )

        # 同步 legacy 列
        if definition.get("model"):
            if profile.llm.model_id == PLATFORM_DEFAULT:
                profile.llm.model_id = definition["model"]
        if definition.get("temperature") is not None:
            profile.llm.temperature = definition.get("temperature")
        if definition.get("max_tokens") is not None:
            profile.llm.max_tokens = definition.get("max_tokens")

        legacy = profile.sync_legacy_fields()
        if profile.llm.model_id == PLATFORM_DEFAULT:
            from core.models.registry import get_model_registry
            model_val = get_model_registry().get_default_id("llm")
        else:
            model_val = normalize_llm_model(legacy.get("model") or definition.get("model") or profile.llm.model_id)

        return {
            "id": definition.get("id", ""),
            "name": definition.get("name", ""),
            "type": definition.get("type", "custom"),
            "status": definition.get("status", "active"),
            "emoji": definition.get("emoji", "🤖"),
            "role": definition.get("role", ""),
            "capabilities": json.dumps(definition.get("capabilities", []), ensure_ascii=False),
            "version": definition.get("version", "1.0.0"),
            "creator": definition.get("creator", "user"),
            "model": model_val,
            "specialized_knowledge": json.dumps(definition.get("specialized_knowledge", [])),
            "reasoning_engine": definition.get("reasoning_engine", "chain-of-thought"),
            "temperature": profile.llm.temperature if profile.llm.temperature is not None else 0.7,
            "max_tokens": profile.llm.max_tokens if profile.llm.max_tokens is not None else 4096,
            "model_profile": json.dumps(profile.model_dump(), ensure_ascii=False),
            "input_schema": json.dumps(definition.get("input_schema"), ensure_ascii=False) if definition.get("input_schema") else None,
            "output_schema": json.dumps(definition.get("output_schema"), ensure_ascii=False) if definition.get("output_schema") else None,
            "tool_namespaces": json.dumps(definition.get("tool_namespaces", []), ensure_ascii=False),
            "memory_config": json.dumps(
                definition.get("memory_config", {}) if isinstance(definition.get("memory_config"), dict)
                else definition["memory_config"].model_dump() if hasattr(definition.get("memory_config"), "model_dump") else {},
                ensure_ascii=False
            ),
            "tools": json.dumps(
                [t if isinstance(t, dict) else t.model_dump() for t in definition.get("tools", [])],
                ensure_ascii=False
            ),
            "tool_ids": json.dumps(definition.get("tool_ids", []), ensure_ascii=False),
            "skill_ids": json.dumps(definition.get("skill_ids", []), ensure_ascii=False),
            "interaction_config": json.dumps(
                definition.get("interaction_config", {}) if isinstance(definition.get("interaction_config"), dict)
                else definition["interaction_config"].model_dump() if hasattr(definition.get("interaction_config"), "model_dump") else {},
                ensure_ascii=False
            ),
            "system_prompt": definition.get("system_prompt", ""),
            "peers": json.dumps(definition.get("peers", []), ensure_ascii=False),
            "evolution_history": json.dumps(
                definition.get("evolution_history", []) if isinstance(definition.get("evolution_history"), list)
                else [e.model_dump() for e in definition.get("evolution_history", [])],
                ensure_ascii=False
            ),
            "performance_metrics": json.dumps(
                definition.get("performance_metrics", {}) if isinstance(definition.get("performance_metrics"), dict)
                else definition["performance_metrics"].model_dump() if hasattr(definition.get("performance_metrics"), "model_dump") else {},
                ensure_ascii=False
            ),
            "runtime_info": json.dumps(
                definition.get("runtime_info", {}) if isinstance(definition.get("runtime_info"), dict)
                else definition["runtime_info"].model_dump() if hasattr(definition.get("runtime_info"), "model_dump") else {},
                ensure_ascii=False,
            ),
            "extra": json.dumps(definition.get("extra", {}), ensure_ascii=False),
        }

    @staticmethod
    def _ts() -> str:
        import time
        return str(int(time.time() * 1000))


# 单例
_registry: Optional[AgentRegistry] = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
