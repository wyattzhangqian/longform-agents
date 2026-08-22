"""DomainRegistry — 领域+规则的统一注册中心（DB CRUD）

权限模型：
- source='platform' 的领域和规则：只读，不可编辑/删除
- source='user' 的领域和规则：所有者可 CRUD
- fork：从 platform domain 复制一份，source 变为 user，可自由修改
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from core.storage.database import get_db
from core.logging import get_logger

_logger = get_logger("domain_registry")


class DomainRegistry:
    """领域+规则的统一注册中心"""

    # ==================== Domain CRUD ====================

    @staticmethod
    async def list_domains(
        source: Optional[str] = None,
        owner_id: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """列出领域（支持按 source/owner 过滤）"""
        db = await get_db()
        conditions: List[str] = []
        params: List[Any] = []

        if not include_archived:
            conditions.append("is_archived = 0")
        if source:
            conditions.append("source = ?")
            params.append(source)
        if owner_id:
            conditions.append("owner_id = ?")
            params.append(owner_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await db.execute(
            f"SELECT * FROM quality_domains {where} ORDER BY source, name",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    async def get_domain(domain_id: str) -> Optional[Dict[str, Any]]:
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM quality_domains WHERE id = ?", (domain_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    async def create_domain(domain: Dict[str, Any]) -> str:
        """创建用户自定义领域"""
        db = await get_db()
        domain_id = domain["id"]
        await db.execute(
            """INSERT INTO quality_domains 
               (id, name, description, emoji, source, owner_id, forked_from, phase_definitions, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                domain_id,
                domain["name"],
                domain.get("description", ""),
                domain.get("emoji", "📦"),
                domain.get("source", "user"),
                domain.get("owner_id", ""),
                domain.get("forked_from", ""),
                json.dumps(domain.get("phase_definitions", []), ensure_ascii=False),
                json.dumps(domain.get("metadata", {}), ensure_ascii=False),
            ),
        )
        await db.commit()
        return domain_id

    @staticmethod
    async def fork_domain(
        source_domain_id: str,
        new_owner_id: str,
        new_name: Optional[str] = None,
    ) -> str:
        """Fork 平台内置领域为用户自定义副本（含所有规则）"""
        db = await get_db()

        # 1. 复制领域定义
        source = await DomainRegistry.get_domain(source_domain_id)
        if not source:
            raise ValueError(f"源领域 {source_domain_id} 不存在")

        new_id = f"user:{new_owner_id}:{source['name']}_{int(time.time())}"
        new_domain = {
            **source,
            "id": new_id,
            "name": new_name or f"{source['name']}(自定义)",
            "source": "user",
            "owner_id": new_owner_id,
            "forked_from": source_domain_id,
        }
        await DomainRegistry.create_domain(new_domain)

        # 2. 复制所有规则
        cursor = await db.execute(
            "SELECT * FROM quality_rules WHERE domain_id = ?",
            (source_domain_id,),
        )
        rules = [dict(row) for row in await cursor.fetchall()]

        for rule in rules:
            new_rule_id = f"user:{new_owner_id}:{rule['rule_id']}_{int(time.time())}"
            await db.execute(
                """INSERT INTO quality_rules
                   (id, domain_id, rule_id, name, description, phase_id, check_type,
                    severity, enabled, config, fix_hint, auto_fix_capable, source, owner_id, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'user', ?, ?)""",
                (
                    new_rule_id, new_id, rule["rule_id"], rule["name"],
                    rule["description"], rule["phase_id"], rule["check_type"],
                    rule["severity"], rule["enabled"], rule["config"],
                    rule["fix_hint"], rule["auto_fix_capable"], new_owner_id,
                    rule["sort_order"],
                ),
            )

        await db.commit()
        _logger.info("领域 fork 完成: %s → %s (%d 条规则)", source_domain_id, new_id, len(rules))

        # 3. 复制知识库条目
        try:
            from core.knowledge.store import KnowledgeStore
            ke_count = await KnowledgeStore.fork_domain_knowledge(source_domain_id, new_id, new_owner_id)
            _logger.info("知识库 fork 完成: %s → %s (%d 条)", source_domain_id, new_id, ke_count)
        except Exception as e:
            _logger.warning("知识库 fork 失败（非致命）: %s", e)

        return new_id

    # ==================== Rule CRUD ====================

    @staticmethod
    async def list_rules(
        domain_id: str,
        phase_id: Optional[str] = None,
        enabled_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """列出领域下的规则"""
        db = await get_db()
        conditions: List[str] = ["domain_id = ?"]
        params: List[Any] = [domain_id]

        if phase_id:
            conditions.append("(phase_id = ? OR phase_id = '')")
            params.append(phase_id)
        if enabled_only:
            conditions.append("enabled = 1")

        cursor = await db.execute(
            f"""SELECT * FROM quality_rules 
                WHERE {' AND '.join(conditions)} 
                ORDER BY sort_order, rule_id""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    async def get_rule(rule_id: str) -> Optional[Dict[str, Any]]:
        """获取单条规则"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM quality_rules WHERE id = ?", (rule_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    async def create_rule(rule: Dict[str, Any]) -> str:
        """创建用户自定义规则"""
        db = await get_db()
        rule_pk = rule["id"]
        await db.execute(
            """INSERT INTO quality_rules
               (id, domain_id, rule_id, name, description, phase_id, check_type,
                severity, enabled, config, fix_hint, auto_fix_capable, source, owner_id, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_pk, rule["domain_id"], rule["rule_id"], rule["name"],
                rule.get("description", ""), rule.get("phase_id", ""),
                rule["check_type"], rule.get("severity", "warning"),
                rule.get("enabled", 1),
                json.dumps(rule.get("config", {}), ensure_ascii=False),
                rule.get("fix_hint", ""), rule.get("auto_fix_capable", 0),
                rule.get("source", "user"), rule.get("owner_id", ""),
                rule.get("sort_order", 0),
            ),
        )
        await db.commit()
        return rule_pk

    @staticmethod
    async def update_rule(rule_id: str, updates: Dict[str, Any], owner_id: str) -> bool:
        """更新规则（仅 owner 可改 user 规则，platform 规则不可改）"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT source, owner_id FROM quality_rules WHERE id = ?",
            (rule_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if row["source"] == "platform":
            raise PermissionError("平台内置规则不可编辑，请 fork 后修改")
        if row["owner_id"] != owner_id:
            raise PermissionError("只能编辑自己的规则")

        # 构建 UPDATE
        allowed_fields = [
            "name", "description", "severity", "enabled",
            "config", "fix_hint", "auto_fix_capable", "sort_order",
        ]
        sets: List[str] = []
        params: List[Any] = []
        for k, v in updates.items():
            if k in allowed_fields:
                if k == "config":
                    v = json.dumps(v, ensure_ascii=False)
                sets.append(f"{k} = ?")
                params.append(v)

        if not sets:
            return False

        sets.append("updated_at = datetime('now','localtime')")
        params.append(rule_id)

        await db.execute(
            f"UPDATE quality_rules SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()
        return True

    @staticmethod
    async def delete_rule(rule_id: str, owner_id: str) -> bool:
        """删除规则（仅 owner 可删 user 规则，platform 规则不可删）"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT source, owner_id FROM quality_rules WHERE id = ?",
            (rule_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if row["source"] == "platform":
            raise PermissionError("平台内置规则不可删除")
        if row["owner_id"] != owner_id:
            raise PermissionError("只能删除自己的规则")

        await db.execute("DELETE FROM quality_rules WHERE id = ?", (rule_id,))
        await db.commit()
        return True

    # ==================== Domain Update / Archive ====================

    @staticmethod
    async def update_domain(domain_id: str, updates: Dict[str, Any]) -> bool:
        """更新自定义领域（platform 不可改）"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT source, owner_id FROM quality_domains WHERE id = ?",
            (domain_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if row["source"] == "platform":
            raise PermissionError("平台内置领域不可编辑，请先 Fork")
        if row["owner_id"] != updates.get("_owner_id", ""):
            raise PermissionError("只能编辑自己的领域")

        allowed_fields = ["name", "description", "emoji"]
        sets: List[str] = []
        params: List[Any] = []
        for k, v in updates.items():
            if k.startswith("_"):
                continue
            if k == "phase_definitions":
                sets.append("phase_definitions = ?")
                params.append(json.dumps(v, ensure_ascii=False))
            elif k == "metadata":
                sets.append("metadata = ?")
                params.append(json.dumps(v, ensure_ascii=False))
            elif k in allowed_fields:
                sets.append(f"{k} = ?")
                params.append(v)

        if not sets:
            return False

        sets.append("updated_at = datetime('now','localtime')")
        params.append(domain_id)

        await db.execute(
            f"UPDATE quality_domains SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()
        return True

    @staticmethod
    async def archive_domain(domain_id: str) -> bool:
        """归档（软删除）领域"""
        db = await get_db()
        await db.execute(
            "UPDATE quality_domains SET is_archived = 1, updated_at = datetime('now','localtime') WHERE id = ?",
            (domain_id,),
        )
        await db.commit()
        return True


# ==================== 种子初始化 ====================

async def seed_platform_domains():
    """启动时初始化平台内置领域（幂等，已存在则跳过）"""
    from core.gateway.seed_domains import PLATFORM_DOMAINS, PLATFORM_RULES

    db = await get_db()

    # 清理旧 ID（轮次 15 产生的，已被轮次 16 替代）
    deprecated_ids = ["platform:generic", "platform:research"]
    for old_id in deprecated_ids:
        await db.execute("DELETE FROM quality_rules WHERE domain_id = ?", (old_id,))
        await db.execute("DELETE FROM quality_domains WHERE id = ?", (old_id,))
    await db.commit()

    domains_added = 0
    rules_added = 0

    for domain in PLATFORM_DOMAINS:
        cursor = await db.execute(
            "SELECT id FROM quality_domains WHERE id = ?", (domain["id"],)
        )
        if await cursor.fetchone():
            continue
        await DomainRegistry.create_domain(domain)
        domains_added += 1

    for rule in PLATFORM_RULES:
        cursor = await db.execute(
            "SELECT id FROM quality_rules WHERE id = ?", (rule["id"],)
        )
        if await cursor.fetchone():
            continue
        await DomainRegistry.create_rule(rule)
        rules_added += 1

    await db.commit()
    if domains_added or rules_added:
        _logger.info(
            "平台领域种子初始化: +{} 领域, +{} 规则 (总计 {} 领域, {} 规则)",
            domains_added, rules_added,
            len(PLATFORM_DOMAINS), len(PLATFORM_RULES),
        )
