"""LLM 共识检测 — 圆桌讨论语义级共识判断

优先使用 LLM 评估多方观点是否达成一致，失败时回退到 Jaccard。
内置内容哈希缓存（TTL 300s）和 Jaccard 快速路径前置，减少 LLM 调用。
"""

from __future__ import annotations
import hashlib
import json
import time
from typing import Dict, Optional, Tuple
from collections import OrderedDict
from core.logging import get_logger

_logger = get_logger("consensus")

# 缓存配置
_CACHE_MAX_SIZE = 100
_CACHE_TTL = 300  # 秒
_JACCARD_THRESHOLD = 0.85  # Jaccard 快速路径阈值


class _ConsensusCache:
    """LRU 共识缓存"""

    def __init__(self, max_size: int = _CACHE_MAX_SIZE, ttl: int = _CACHE_TTL):
        self._cache: OrderedDict[str, Tuple[dict, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl

    def get(self, key: str) -> Optional[dict]:
        if key in self._cache:
            result, ts = self._cache[key]
            if time.time() - ts < self._ttl:
                self._cache.move_to_end(key)
                return result
            else:
                del self._cache[key]
        return None

    def set(self, key: str, value: dict):
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[key] = (value, time.time())


_cache = _ConsensusCache()


def _content_hash(topic: str, statements: Dict[str, str]) -> str:
    """基于内容生成缓存键"""
    raw = json.dumps({"topic": topic, "statements": sorted(statements.items())}, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """计算两段文本的 Jaccard 相似度（基于字符级 set）"""
    set_a = set(text_a)
    set_b = set(text_b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _check_jaccard_fast(
    topic: str,
    statements: Dict[str, str],
    threshold: float,
) -> Optional[dict]:
    """Jaccard 快速路径：所有发言对相似度超过阈值则直接判定共识"""
    ids = list(statements.keys())
    if len(ids) < 2:
        return None

    min_similarity = 1.0
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sim = _jaccard_similarity(statements[ids[i]], statements[ids[j]])
            min_similarity = min(min_similarity, sim)

    if min_similarity >= _JACCARD_THRESHOLD:
        return {
            "reached": True,
            "confidence": round(min(min_similarity, threshold + 0.1), 2),
            "consensus": "各方观点高度一致",
            "disagreements": [],
            "method": "jaccard_fast",
        }
    return None


async def check_consensus_llm(
    topic: str,
    statements: Dict[str, str],
    threshold: float = 0.7,
    agent_names: Dict[str, str] = None,
) -> Optional[dict]:
    """使用 LLM 判断多方发言是否达成共识

    Args:
        topic: 讨论议题
        statements: agent_id → 最新发言
        threshold: 共识置信度阈值 0-1
        agent_names: agent_id → 显示名

    Returns:
        {reached, confidence, consensus, method, disagreements?} 或 None（LLM 不可用）
    """
    if len(statements) < 2:
        return {"reached": False, "confidence": 0.0, "consensus": None, "method": "llm"}

    # 1. 查缓存
    cache_key = _content_hash(topic, statements)
    cached = _cache.get(cache_key)
    if cached is not None:
        cached["method"] = f"{cached.get('method', 'cached')}_cache"
        return cached

    # 2. Jaccard 快速路径
    fast_result = _check_jaccard_fast(topic, statements, threshold)
    if fast_result is not None:
        _cache.set(cache_key, fast_result)
        return fast_result

    # 3. LLM 语义判断
    names = agent_names or {}
    lines = []
    for aid, content in statements.items():
        label = names.get(aid, aid)
        lines.append(f"【{label}】\n{content[:800]}")

    prompt = f"""你是多方讨论的主持人。请评估以下 Agent 对议题是否达成共识。

议题：{topic}

各方最新观点：
{chr(10).join(lines)}

请返回 JSON（不要其他文字）：
{{
  "reached": true/false,
  "confidence": 0.0-1.0,
  "consensus_summary": "若达成共识，用1-3句话总结共识内容；否则为空",
  "disagreements": ["主要分歧点1", "主要分歧点2"]
}}

评判标准：
- confidence >= {threshold} 且 reached=true 表示达成共识
- 关注语义一致性，而非措辞相同
- 若存在不可调和的方向性分歧，reached=false"""

    try:
        from tools.llm_client import LLMClient
        client = LLMClient()
        raw = await client.chat_json(
            prompt,
            system_prompt="你是专业的讨论协调员，擅长识别多方观点的一致性与分歧。",
        )
        if not raw or not isinstance(raw, dict):
            return None

        confidence = float(raw.get("confidence", 0))
        reached = bool(raw.get("reached", False)) and confidence >= threshold
        summary = raw.get("consensus_summary") or raw.get("consensus") or ""

        result = {
            "reached": reached,
            "confidence": round(min(1.0, max(0.0, confidence)), 2),
            "consensus": summary if reached else None,
            "disagreements": raw.get("disagreements") or [],
            "method": "llm",
        }

        # 写入缓存
        _cache.set(cache_key, result)

        return result
    except Exception as e:
        _logger.warning("LLM 共识检测失败: %s", e)
        return None
