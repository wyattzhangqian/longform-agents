"""Knowledge Layer — 知识层基础设施

纯通用层：提供知识存储、检索、管理的抽象接口和基础实现。

知识层是 Agent 之间共享经验的桥梁：
- 领域知识：特定领域的结构化知识（由 DomainAdapter 提供）
- 经验知识：Agent 执行成功案例的积累（由 Evolution 自动收集）
- 共享知识：Agent 之间传递的上下文和学习

架构：
    KnowledgeItem → KnowledgeBase（存储+检索）→ AgentKnowledge（Agent 视角）
"""

from __future__ import annotations
from typing import Optional, List, Dict, Any
from datetime import datetime
import json
from pydantic import BaseModel, Field
from abc import ABC, abstractmethod
from core.logging import get_logger

_logger = get_logger("knowledge")


# ============================================================
# KnowledgeItem — 知识单元
# ============================================================

class KnowledgeItem(BaseModel):
    """一条知识单元"""
    id: str                                              # 唯一标识
    domain_id: str = "general"                           # 所属领域
    agent_id: str = ""                                   # 来源 Agent（空=系统级）
    type: str = "fact"                                   # fact | rule | pattern | experience | reference
    content: str = ""                                    # 知识内容
    embedding: Optional[List[float]] = None              # 向量嵌入（可选）
    tags: List[str] = Field(default_factory=list)        # 标签
    confidence: float = Field(default=0.5, ge=0, le=1)   # 置信度
    source: str = ""                                     # 来源（agent_id / document / url）
    usage_count: int = 0                                 # 被引用次数
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


# ============================================================
# KnowledgeBase — 抽象接口
# ============================================================

class KnowledgeBase(ABC):
    """知识库抽象接口

    应用层实现具体存储（向量数据库、图数据库、全文索引等）。
    通用层只定义接口和基础内存实现。
    """

    @abstractmethod
    async def store(self, item: KnowledgeItem) -> str:
        """存储知识条目，返回 id"""
        ...

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        domain_id: str = None,
        agent_id: str = None,
        type: str = None,
        top_k: int = 5,
        min_confidence: float = 0.0,
    ) -> List[KnowledgeItem]:
        """检索相关知识条目"""
        ...

    @abstractmethod
    async def search_by_tags(
        self,
        tags: List[str],
        domain_id: str = None,
        limit: int = 20,
    ) -> List[KnowledgeItem]:
        """按标签检索"""
        ...

    @abstractmethod
    async def update_confidence(self, item_id: str, delta: float):
        """更新知识置信度（+为增强，-为减弱）"""
        ...

    @abstractmethod
    async def get_domain_knowledge(self, domain_id: str, limit: int = 100) -> List[KnowledgeItem]:
        """获取领域全部知识"""
        ...

    @abstractmethod
    async def delete(self, item_id: str) -> bool:
        """删除知识条目"""
        ...


# ============================================================
# InMemoryKnowledgeBase — 基础内存实现
# ============================================================

class InMemoryKnowledgeBase(KnowledgeBase):
    """内存知识库（开发/测试用，生产环境替换为向量数据库）"""

    def __init__(self, domain_id: str = "general"):
        self._store: Dict[str, KnowledgeItem] = {}
        self._domain_id = domain_id
        _logger.info("初始化 InMemoryKnowledgeBase (domain=%s)", domain_id)

    async def store(self, item: KnowledgeItem) -> str:
        self._store[item.id] = item
        return item.id

    async def retrieve(
        self,
        query: str,
        domain_id: str = None,
        agent_id: str = None,
        type: str = None,
        top_k: int = 5,
        min_confidence: float = 0.0,
    ) -> List[KnowledgeItem]:
        results = []
        query_lower = query.lower()

        for item in self._store.values():
            # 过滤器
            if domain_id and item.domain_id != domain_id:
                continue
            if agent_id and item.agent_id != agent_id:
                continue
            if type and item.type != type:
                continue
            if item.confidence < min_confidence:
                continue

            # 简单关键词匹配（生产环境应使用向量相似度）
            score = self._match_score(query_lower, item)
            if score > 0:
                results.append((score, item))

        results.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in results[:top_k]]

    async def search_by_tags(
        self,
        tags: List[str],
        domain_id: str = None,
        limit: int = 20,
    ) -> List[KnowledgeItem]:
        tag_set = set(tags)
        results = []
        for item in self._store.values():
            if domain_id and item.domain_id != domain_id:
                continue
            if tag_set & set(item.tags):
                results.append(item)
        return results[:limit]

    async def update_confidence(self, item_id: str, delta: float):
        if item_id in self._store:
            item = self._store[item_id]
            item.confidence = max(0.0, min(1.0, item.confidence + delta))
            item.updated_at = datetime.now().isoformat()

    async def get_domain_knowledge(self, domain_id: str, limit: int = 100) -> List[KnowledgeItem]:
        return [item for item in self._store.values()
                if item.domain_id == domain_id][:limit]

    async def delete(self, item_id: str) -> bool:
        return self._store.pop(item_id, None) is not None

    def size(self) -> int:
        return len(self._store)

    @staticmethod
    def _match_score(query: str, item: KnowledgeItem) -> float:
        """简单关键词匹配评分"""
        score = 0.0
        if query in item.content.lower():
            score += 3.0
        for tag in item.tags:
            if query in tag.lower():
                score += 1.0
        # 标签匹配加分
        words = query.split()
        for word in words:
            if word in item.content.lower():
                score += 0.5
        return score


# ============================================================
# AgentKnowledge — Agent 个人知识上下文
# ============================================================

class AgentKnowledge:
    """Agent 的知识上下文

    每个 Agent 实例持有一个 AgentKnowledge，管理它：
    - 领域知识（从 KnowledgeBase 检索）
    - 经验记忆（从 Evolution 案例提取）
    - 上下文窗口（当前任务相关的知识片段）

    用法：
        ak = AgentKnowledge(agent_id="analyzer", kb=knowledge_base)
        await ak.load_domain_knowledge("data_analysis")
        context = ak.build_context(query="用户留存分析")
        # context → 注入到 LLM system prompt
    """

    def __init__(self, agent_id: str, kb: KnowledgeBase):
        self.agent_id = agent_id
        self.kb = kb
        self._domain_cache: List[KnowledgeItem] = []
        self._context_window: List[KnowledgeItem] = []
        self._max_context_items = 10

    async def load_domain_knowledge(self, domain_id: str):
        """加载领域知识到缓存"""
        items = await self.kb.get_domain_knowledge(domain_id)
        self._domain_cache = items

    async def learn(self, item: KnowledgeItem):
        """学习新知识"""
        item.agent_id = self.agent_id
        await self.kb.store(item)
        self._domain_cache.append(item)

    async def build_context(self, query: str, domain_id: str = None) -> str:
        """构建当前任务的知识上下文（用于注入 LLM prompt）

        返回格式化的知识上下文字符串。
        """
        # 检索相关知识
        relevant = await self.kb.retrieve(
            query=query,
            domain_id=domain_id,
            top_k=self._max_context_items,
            min_confidence=0.3,
        )

        if not relevant:
            return ""

        self._context_window = relevant

        # 格式化为上下文
        parts = ["## 相关知识\n"]
        for item in relevant:
            parts.append(
                f"- [{item.type}] (置信度:{item.confidence:.1%}) {item.content[:200]}"
            )
        return "\n".join(parts)

    async def reinforce(self, item_id: str, was_helpful: bool):
        """根据知识是否有用来增强/减弱置信度"""
        delta = 0.1 if was_helpful else -0.05
        await self.kb.update_confidence(item_id, delta)


# ============================================================
# KnowledgeManager — 全局知识管理器
# ============================================================

class KnowledgeManager:
    """知识管理器单例

    管理多个领域的知识库实例和 Agent 知识上下文。

    用法：
        km = KnowledgeManager()
        km.register_domain("data_analysis", InMemoryKnowledgeBase("data_analysis"))

        agent_knowledge = km.get_agent_knowledge("analyzer", "data_analysis")
        context = await agent_knowledge.build_context("用户留存")
    """

    def __init__(self):
        self._kb: Dict[str, KnowledgeBase] = {}
        self._agent_knowledge: Dict[str, AgentKnowledge] = {}

    def register_domain(self, domain_id: str, kb: KnowledgeBase = None):
        """注册领域知识库

        #2 自动检测 Chroma 配置（CHROMA_HOST 或 CHROMA_PERSIST_DIR），
        配置了则用 ChromaVectorKnowledgeBase（持久化），否则用 VectorKnowledgeBase（内存 TF-IDF）。
        """
        if kb is None:
            import os as _os
            _use_chroma = _os.getenv("CHROMA_HOST") or _os.getenv("CHROMA_PERSIST_DIR") or _os.getenv("KNOWLEDGE_BACKEND", "") == "chroma"
            if _use_chroma:
                try:
                    from core.knowledge.chroma_kb import ChromaVectorKnowledgeBase
                    kb = ChromaVectorKnowledgeBase(domain_id)
                    _logger.info("领域 %s 使用 ChromaVectorKnowledgeBase（持久化）", domain_id)
                except Exception as e:
                    _logger.warning("ChromaKB 初始化失败，回退 VectorKnowledgeBase: %s", e)
                    from core.knowledge.vector_kb import VectorKnowledgeBase
                    kb = VectorKnowledgeBase(domain_id)
            else:
                from core.knowledge.vector_kb import VectorKnowledgeBase
                kb = VectorKnowledgeBase(domain_id)
        self._kb[domain_id] = kb

    def get_knowledge_base(self, domain_id: str) -> Optional[KnowledgeBase]:
        """获取领域知识库"""
        return self._kb.get(domain_id)

    def get_agent_knowledge(
        self,
        agent_id: str,
        domain_id: str = "general",
    ) -> AgentKnowledge:
        """获取或创建 Agent 知识上下文"""
        key = f"{domain_id}:{agent_id}"
        if key not in self._agent_knowledge:
            kb = self._kb.get(domain_id) or InMemoryKnowledgeBase(domain_id)
            self._agent_knowledge[key] = AgentKnowledge(agent_id, kb)
        return self._agent_knowledge[key]

    async def share_knowledge(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        domain_id: str = "general",
        confidence: float = 0.7,
    ):
        """Agent 之间共享知识"""
        kb = self._kb.get(domain_id) or InMemoryKnowledgeBase(domain_id)
        item = KnowledgeItem(
            id=f"shared_{from_agent}_{datetime.now().timestamp()}",
            domain_id=domain_id,
            agent_id=from_agent,
            type="experience",
            content=content,
            tags=["shared", from_agent, to_agent],
            confidence=confidence,
            source=f"agent:{from_agent}",
        )
        await kb.store(item)


# 全局单例
_knowledge_manager: Optional[KnowledgeManager] = None


def get_knowledge_manager() -> KnowledgeManager:
    global _knowledge_manager
    if _knowledge_manager is None:
        _knowledge_manager = KnowledgeManager()
    return _knowledge_manager
