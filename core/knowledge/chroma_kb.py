"""基于 Chroma 的持久化向量知识库

继承 VectorKnowledgeBase，覆写 store/delete/_load 实现 Chroma 持久化。
- store：父类内存索引 + Chroma upsert（双写）
- delete：父类内存删除 + Chroma delete
- _load：启动时从 Chroma collection.get() 恢复全部条目到内存索引
- retrieve：走父类 TF-IDF/embedding 检索（内存），Chroma 仅做持久化层

配置：
    CHROMA_PERSIST_DIR=.chroma_data  （本地持久化，无需 Docker）
    CHROMA_HOST=localhost + CHROMA_PORT=8001  （远程 Chroma 服务）
    不配则回退纯 VectorKnowledgeBase（内存 TF-IDF）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from core.knowledge.vector_kb import VectorKnowledgeBase
from core.knowledge import KnowledgeItem

logger = logging.getLogger(__name__)


class ChromaVectorKnowledgeBase(VectorKnowledgeBase):
    """持久化向量知识库 — Chroma 后端

    使用 ChromaDB PersistentClient（本地）或 HttpClient（远程），
    默认 all-MiniLM-L6-v2 embedding（Chroma 内置）。
    初始化失败时自动回退到纯 VectorKnowledgeBase（内存 TF-IDF）。
    """

    def __init__(self, domain_id: str = "general", dim: int = 1024) -> None:
        super().__init__(domain_id, dim)
        self._chroma_collection = None
        self._chroma_ready = False

        try:
            import chromadb
            from chromadb.config import Settings

            host = os.getenv("CHROMA_HOST", "")
            port = os.getenv("CHROMA_PORT", "8000")
            persist_dir = os.getenv("CHROMA_PERSIST_DIR", "")

            if host:
                # 远程 Chroma 服务
                self._chroma_client = chromadb.HttpClient(
                    host=host, port=int(port),
                    settings=Settings(anonymized_telemetry=False),
                )
                logger.info("ChromaKB(HttpClient): host=%s:%s collection=%s", host, port, domain_id)
            else:
                # 本地持久化
                path = persist_dir or f"./data/chroma_{domain_id}"
                self._chroma_client = chromadb.PersistentClient(
                    path=path,
                    settings=Settings(anonymized_telemetry=False),
                )
                logger.info("ChromaKB(PersistentClient): path=%s collection=%s", path, domain_id)

            self._chroma_collection = self._chroma_client.get_or_create_collection(
                name=f"kb_{domain_id}",
                metadata={"hnsw:space": "cosine"},
            )
            self._chroma_ready = True

            # 启动时从 Chroma 恢复到内存索引
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 在运行中的事件循环里，延迟加载（register_domain 后调 _load）
                    pass
                else:
                    loop.run_until_complete(self._load())
            except RuntimeError:
                pass  # 无事件循环，跳过同步加载

        except ImportError:
            logger.warning(
                "chromadb not installed — ChromaKB 回退到内存 TF-IDF。"
                "Run: pip install chromadb>=0.4.0"
            )
        except Exception as e:
            logger.warning("ChromaKB 初始化失败，回退到内存 TF-IDF: %s", e)

    async def store(self, item: KnowledgeItem) -> str:
        """双写：父类内存索引 + Chroma"""
        item_id = await super().store(item)

        if self._chroma_ready and self._chroma_collection is not None:
            try:
                self._chroma_collection.upsert(
                    ids=[item_id],
                    documents=[item.content or ""],
                    metadatas=[{
                        "domain_id": item.domain_id,
                        "agent_id": item.agent_id,
                        "type": item.type,
                        "confidence": item.confidence,
                        "tags": ",".join(item.tags),
                        "source": item.source,
                    }],
                )
            except Exception as e:
                logger.debug("Chroma upsert 失败（不影响内存索引）: %s", e)

        return item_id

    async def delete(self, item_id: str) -> bool:
        """双删：父类内存 + Chroma"""
        existed = await super().delete(item_id)

        if self._chroma_ready and self._chroma_collection is not None:
            try:
                self._chroma_collection.delete(ids=[item_id])
            except Exception as e:
                logger.debug("Chroma delete 失败: %s", e)

        return existed

    async def _load(self) -> None:
        """从 Chroma 恢复全部条目到内存索引"""
        if not self._chroma_ready or self._chroma_collection is None:
            return

        try:
            result = self._chroma_collection.get()
            if not result or not result.get("ids"):
                return

            count = 0
            for i, doc_id in enumerate(result["ids"]):
                doc = result["documents"][i] if result.get("documents") else ""
                meta = result["metadatas"][i] if result.get("metadatas") else {}

                # 跳过已存在于内存的（避免重复）
                if doc_id in self._store:
                    continue

                item = KnowledgeItem(
                    id=doc_id,
                    domain_id=meta.get("domain_id", self._domain_id),
                    agent_id=meta.get("agent_id", ""),
                    type=meta.get("type", "fact"),
                    content=doc,
                    tags=meta.get("tags", "").split(",") if meta.get("tags") else [],
                    confidence=float(meta.get("confidence", 0.5)),
                    source=meta.get("source", ""),
                )
                self._store[doc_id] = item
                count += 1

            if count:
                logger.info("ChromaKB 从 Chroma 恢复 %d 条到内存索引 (domain=%s)", count, self._domain_id)
        except Exception as e:
            logger.warning("Chroma _load 失败: %s", e)
