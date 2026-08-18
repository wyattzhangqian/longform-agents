"""VectorKnowledgeBase — 向量检索知识库（零外部依赖实现）

设计目标：
1. 零外部向量库依赖 —— 默认用 TF-IDF + 余弦相似度（纯 stdlib），开箱即用
2. 可升级到真实 embedding —— 配置 EMBEDDING_BASE_URL + EMBEDDING_API_KEY + EMBEDDING_MODEL 后自动走 HTTP embedding API
3. 可升级到外部向量库 —— 子类化 VectorKnowledgeBase 并覆写 _persist/_load 即可接 pgvector / Chroma / CloudBase

架构层级（遵循宪法 L2 不依赖 L3/L4）：
    KnowledgeBase(ABC) ← VectorKnowledgeBase(本文件，L2 通用)
                            ↑
              pgvector_kb.py / chroma_kb.py（L3 应用层实现，按需注入）
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from core.knowledge import KnowledgeBase, KnowledgeItem, _logger
from core.logging import get_logger

_logger = get_logger("vector_kb")

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> List[str]:
    """中英文混合分词（零依赖）

    英文/数字 → 整词；中文 → 生成 bigram（二字滑窗）+ 单字，
    解决 "生图尺寸" 与 "生图建议尺寸" 的匹配问题。
    """
    raw = _TOKEN_RE.findall(text or "")
    tokens: List[str] = []
    # 合并连续中文字符做 bigram
    chinese_buf: List[str] = []
    result: List[str] = []
    for tok in raw:
        if "\u4e00" <= tok <= "\u9fff":
            chinese_buf.append(tok)
        else:
            if chinese_buf:
                result.extend(_chinese_ngrams(chinese_buf))
                chinese_buf = []
            result.append(tok.lower())
    if chinese_buf:
        result.extend(_chinese_ngrams(chinese_buf))
    return result


def _chinese_ngrams(chars: List[str]) -> List[str]:
    """中文 bigram + unigram（兼顾召回率与精度）"""
    out: List[str] = list(chars)  # unigram
    for i in range(len(chars) - 1):
        out.append(chars[i] + chars[i + 1])  # bigram
    return out


def _hash_id(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:16]


# ============================================================
# Embedding Provider — 可插拔
# ============================================================

class _EmbeddingProvider:
    """embedding 抽象：有 HTTP 配置则走 API，否则 None（回退 TF-IDF）"""

    def __init__(self) -> None:
        self.base_url = os.getenv("EMBEDDING_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("EMBEDDING_API_KEY", "")
        self.model = os.getenv("EMBEDDING_MODEL", "")
        self.enabled = bool(self.base_url and self.api_key and self.model)

    async def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self.enabled or not texts:
            return None
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "input": texts}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status >= 400:
                        _logger.warning("embedding API %s: %s", resp.status, await resp.text())
                        return None
                    body = await resp.json()
                    data = body.get("data") or []
                    return [d.get("embedding", []) for d in data if isinstance(d, dict)]
        except Exception as e:
            _logger.warning("embedding 调用失败，回退 TF-IDF: %s", e)
            return None


# ============================================================
# VectorKnowledgeBase — 向量检索知识库
# ============================================================

class VectorKnowledgeBase(KnowledgeBase):
    """向量检索知识库

    检索策略（自动降级）：
    1. 若 KnowledgeItem 已有 embedding → 直接余弦相似度
    2. 若配置了 EMBEDDING_BASE_URL → store 时调 API 生成 embedding，retrieve 时用 embedding 余弦
    3. 否则 → TF-IDF 余弦相似度（纯 stdlib，开箱即用）

    生产升级：子类化并覆写 _persist/_load 接入 pgvector / Chroma。
    """

    def __init__(self, domain_id: str = "general", dim: int = 1024) -> None:
        self._domain_id = domain_id
        self._dim = dim
        self._store: Dict[str, KnowledgeItem] = {}
        self._embeddings: Dict[str, List[float]] = {}  # item_id → vector
        self._df: Counter = Counter()  # document frequency（TF-IDF 用）
        self._embedding_provider = _EmbeddingProvider()
        _logger.info(
            "初始化 VectorKnowledgeBase(domain=%s, embedding=%s, dim=%s)",
            domain_id, "api" if self._embedding_provider.enabled else "tfidf", dim,
        )

    # ---------- KnowledgeBase 接口 ----------

    async def store(self, item: KnowledgeItem) -> str:
        if not item.id:
            item.id = _hash_id(item.content)
        self._store[item.id] = item
        # 更新 DF
        tokens = set(_tokenize(item.content))
        for t in tokens:
            self._df[t] += 1
        # 若已有 embedding 直接存
        if item.embedding:
            self._embeddings[item.id] = item.embedding
            return item.id
        # 尝试调 API 生成
        if self._embedding_provider.enabled:
            vecs = await self._embedding_provider.embed([item.content])
            if vecs and vecs[0]:
                self._embeddings[item.id] = vecs[0]
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
        candidates = self._filter(domain_id, agent_id, type, min_confidence)
        if not candidates:
            return []
        scores = await self._score(query, candidates)
        # 按 score 降序；score 相同时按 id 排序避免 KnowledgeItem 不可比较报错
        scores.sort(key=lambda x: (-x[0], x[1].id))
        return [item for _, item in scores[:top_k]]

    async def search_by_tags(
        self, tags: List[str], domain_id: str = None, limit: int = 20,
    ) -> List[KnowledgeItem]:
        tag_set = set(tags)
        results = []
        for item in self._store.values():
            if domain_id and item.domain_id != domain_id:
                continue
            if tag_set & set(item.tags):
                results.append(item)
        return results[:limit]

    async def update_confidence(self, item_id: str, delta: float) -> None:
        if item_id in self._store:
            item = self._store[item_id]
            item.confidence = max(0.0, min(1.0, item.confidence + delta))
            item.updated_at = datetime.now().isoformat()

    async def get_domain_knowledge(self, domain_id: str, limit: int = 100) -> List[KnowledgeItem]:
        return [i for i in self._store.values() if i.domain_id == domain_id][:limit]

    async def delete(self, item_id: str) -> bool:
        existed = self._store.pop(item_id, None) is not None
        self._embeddings.pop(item_id, None)
        return existed

    def size(self) -> int:
        return len(self._store)

    # ---------- 内部方法 ----------

    def _filter(
        self, domain_id: Optional[str], agent_id: Optional[str],
        type: Optional[str], min_confidence: float,
    ) -> List[KnowledgeItem]:
        out = []
        for item in self._store.values():
            if domain_id and item.domain_id != domain_id:
                continue
            if agent_id and item.agent_id != agent_id:
                continue
            if type and item.type != type:
                continue
            if item.confidence < min_confidence:
                continue
            out.append(item)
        return out

    async def _score(self, query: str, candidates: List[KnowledgeItem]) -> List[Tuple[float, KnowledgeItem]]:
        """打分：embedding 优先，回退 TF-IDF"""
        # 若候选有 embedding 且 query 可 embed → 余弦
        has_vec = any(self._embeddings.get(i.id) for i in candidates)
        if has_vec and self._embedding_provider.enabled:
            qvecs = await self._embedding_provider.embed([query])
            if qvecs and qvecs[0]:
                qv = qvecs[0]
                return [
                    (self._cosine(qv, self._embeddings.get(i.id, [])), i)
                    for i in candidates
                ]
        # 回退 TF-IDF 余弦
        q_vec = self._tfidf_vector(query)
        results = []
        for item in candidates:
            iv = self._tfidf_vector(item.content)
            score = self._cosine_sparse(q_vec, iv)
            results.append((score, item))
        return results

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    @staticmethod
    def _cosine_sparse(a: Dict[str, float], b: Dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        common = set(a) & set(b)
        dot = sum(a[t] * b[t] for t in common)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return dot / (na * nb) if na and nb else 0.0

    def _tfidf_vector(self, text: str) -> Dict[str, float]:
        tokens = _tokenize(text)
        if not tokens:
            return {}
        tf = Counter(tokens)
        n_docs = max(len(self._store), 1)
        vec: Dict[str, float] = {}
        for term, count in tf.items():
            df = self._df.get(term, 0) + 1  # +1 平滑
            idf = math.log((n_docs + 1) / df) + 1.0
            vec[term] = (count / len(tokens)) * idf
        return vec

    # ---------- 持久化钩子（子类可覆写接外部向量库） ----------

    def _persist(self) -> None:
        """同步到外部向量库（默认 no-op，子类实现）"""
        pass

    async def _load(self) -> None:
        """从外部向量库加载（默认 no-op，子类实现）"""
        pass


# ============================================================
# 工厂函数 — 根据配置选择知识库实现
# ============================================================

def get_knowledge_base(collection_name: str = "platform_knowledge"):
    """工厂函数：根据配置选择知识库实现

    KNOWLEDGE_BACKEND=chroma 或配置了 CHROMA_HOST/CHROMA_PERSIST_DIR → ChromaVectorKnowledgeBase
    KNOWLEDGE_BACKEND=memory → VectorKnowledgeBase（内存 TF-IDF）
    """
    backend = os.getenv("KNOWLEDGE_BACKEND", "")
    use_chroma = (
        backend == "chroma"
        or os.getenv("CHROMA_HOST")
        or os.getenv("CHROMA_PERSIST_DIR")
    )

    if use_chroma:
        try:
            from core.knowledge.chroma_kb import ChromaVectorKnowledgeBase
            return ChromaVectorKnowledgeBase(collection_name)
        except ImportError:
            _logger.warning("chromadb 不可用，回退到 VectorKnowledgeBase")
        except Exception as e:
            _logger.warning("ChromaVectorKnowledgeBase 初始化失败，回退到 VectorKnowledgeBase: %s", e)

    return VectorKnowledgeBase(collection_name)


# ============================================================
# 批量导入辅助
# ============================================================

async def bulk_store(kb: VectorKnowledgeBase, items: List[KnowledgeItem]) -> int:
    """批量导入知识条目，返回成功数

    若配置了 embedding API，会分批调 embed（每批最多 64 条）以减少请求次数。
    """
    if not items:
        return 0
    # 批量 embed（若可用）
    if kb._embedding_provider.enabled:
        texts = [i.content for i in items]
        for i in range(0, len(texts), 64):
            batch = texts[i: i + 64]
            vecs = await kb._embedding_provider.embed(batch)
            if vecs:
                for j, v in enumerate(vecs):
                    if v:
                        items[i + j].embedding = v
    count = 0
    for item in items:
        if not item.id:
            item.id = _hash_id(item.content)
        await kb.store(item)
        count += 1
    _logger.info("批量导入完成: %s 条 → domain=%s", count, kb._domain_id)
    return count
