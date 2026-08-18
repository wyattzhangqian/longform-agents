"""KnowledgeManager — 知识管理入口

封装知识库的检索和种子数据加载，供 Agent prompt 注入使用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.knowledge.vector_kb import bulk_store, get_knowledge_base
from core.logging import get_logger

_logger = get_logger("knowledge")


class KnowledgeManager:
    """知识管理器 — 封装知识库检索和种子数据加载"""

    def __init__(self, domain: str = "generic"):
        self.kb = get_knowledge_base(collection_name=f"knowledge_{domain}")

    async def inject_context(self, query: str, top_k: int = 5) -> str:
        """为 Agent prompt 注入相关知识"""
        try:
            results = await self.kb.retrieve(query, top_k=top_k)
        except Exception as e:
            _logger.debug("Knowledge search failed: %s", e)
            return ""

        if not results:
            return ""

        context_lines = [f"- {r.content}" for r in results]
        return "## 相关知识参考\n\n" + "\n".join(context_lines)

    async def load_seed_data(self, entries: List[Dict[str, Any]]) -> int:
        """加载种子知识（幂等）"""
        from core.knowledge import KnowledgeItem
        items = [
            KnowledgeItem(
                id=e.get("id", ""),
                domain_id=e.get("metadata", {}).get("domain", "general"),
                content=e.get("content", ""),
                tags=e.get("tags", []),
            )
            for e in entries
        ]
        return await bulk_store(self.kb, items)


async def seed_knowledge() -> int:
    """启动时加载种子知识到 Chroma（幂等）

    种子数据在 seed_knowledge.py 中按域组织，
    Chroma 目前只加载通用（非领域专属）条目。
    """
    km = KnowledgeManager(domain="generic")
    try:
        current_count = km.kb.size()
    except Exception:
        current_count = 0

    if current_count > 0:
        _logger.info("Knowledge already seeded: %d entries", current_count)
        return 0

    # 使用 DB seed 中已有的通用知识（P1-11 修复：原查不存在的 `knowledge` 表，
    # 静默吞错导致 generic 向量库永远为空；真实表是 knowledge_entries）
    from core.storage.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, title, content, domain_id AS domain "
            "FROM knowledge_entries WHERE domain_id = 'platform:general' LIMIT 50",
        )
        rows = await cursor.fetchall()
    except Exception:
        rows = []

    all_entries: List[Dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        all_entries.append({
            "id": r.get("id", f"kb_{len(all_entries)}"),
            "content": r.get("content", r.get("title", "")),
            "metadata": {"domain": r.get("domain", "generic"), "source": "db"},
        })

    if not all_entries:
        _logger.info("No generic seed entries to load (DB empty)")
        return 0

    total = await km.load_seed_data(all_entries)
    _logger.info("Seeded %d knowledge entries to Chroma from DB", total)
    return total
