"""领域识别——LLM + 关键词，自包含于引擎层。

从 api/routes/plan.py 下沉：引擎层（open-core）需要独立做领域识别，
不能依赖 api/（产品层）。API 层只做薄封装，行为保持一致。

对外函数：
    match_domain_llm(task) → (domain_id, domain_name, confidence, reasoning)
    match_domain(task)     → (domain_id, domain_name, confidence, reasoning) 关键词兜底
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, Tuple

from core.logging import get_logger

_logger = get_logger("domain_matcher")

# ── 关键词兜底（LLM 不可用时）──
_DOMAIN_KEYWORDS_FALLBACK = {
    "platform:comic_drama": ["漫剧", "条漫", "分镜", "短漫", "动态漫画"],
    "platform:comic_static": ["漫画", "manga", "页漫", "四格", "单幅"],
    "platform:music_production": ["歌曲", "音乐", "作词", "作曲", "BGM", "编曲", "混音", "民谣", "旋律", "歌词", "配乐"],
    "platform:web_novel": ["小说", "网文", "章节", "起点", "番茄", "连载", "仙侠", "都市"],
    "platform:research_report": ["报告", "调研", "研究", "分析", "竞品", "行业"],
}

# 领域描述（给 LLM 识别用—优先从 quality_domains 表加载）
_DOMAIN_DESCRIPTIONS_FALLBACK = {
    "platform:web_novel": "网络小说/网文创作：写小说、章节、世界观、人物设定、情节构思、仙侠/都市/玄幻等",
    "platform:comic_static": "静态漫画/页漫创作：画漫画、分镜、线稿、上色、角色设计、页面排版等",
    "platform:comic_drama": "漫剧/动态漫画/条漫：制作视频漫画、分镜动画、配音、后期合成等",
    "platform:music_production": "音乐制作：作词、作曲、编曲、混音、母带、歌曲分析、旋律创作、配器、民谣/流行/古典等",
    "platform:research_report": "研究报告/行业分析：撰写报告、调研、数据分析、竞品分析、图表制作等",
}

# 运行时缓存
_domain_keywords_cache: dict = {}
_domain_descriptions_cache: dict = {}


async def load_domain_keywords() -> dict:
    """从 quality_domains 表加载领域关键词（DB 驱动，新增领域无需改代码）"""
    global _domain_keywords_cache
    if _domain_keywords_cache:
        return _domain_keywords_cache
    try:
        from core.storage.database import get_read_db
        db = await get_read_db()
        cursor = await db.execute(
            "SELECT id, metadata FROM quality_domains WHERE is_archived = 0"
        )
        rows = await cursor.fetchall()
        for r in rows:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
            keywords = meta.get("keywords", [])
            if keywords:
                _domain_keywords_cache[r["id"]] = keywords
    except Exception:
        pass
    if not _domain_keywords_cache:
        _domain_keywords_cache = dict(_DOMAIN_KEYWORDS_FALLBACK)
    return _domain_keywords_cache


async def load_domain_descriptions() -> dict:
    """从 quality_domains 表加载领域描述"""
    global _domain_descriptions_cache
    if _domain_descriptions_cache:
        return _domain_descriptions_cache
    try:
        from core.storage.database import get_read_db
        db = await get_read_db()
        cursor = await db.execute(
            "SELECT id, name, description FROM quality_domains WHERE is_archived = 0"
        )
        rows = await cursor.fetchall()
        for r in rows:
            desc = r["description"] or ""
            if desc:
                _domain_descriptions_cache[r["id"]] = f"{r['name']}：{desc}"
    except Exception:
        pass
    if not _domain_descriptions_cache:
        _domain_descriptions_cache = dict(_DOMAIN_DESCRIPTIONS_FALLBACK)
    return _domain_descriptions_cache


def match_domain_keywords(task: str) -> tuple:
    """纯关键词匹配 — 不依赖数据库，不回退失败。

    Returns:
        (domain_id, domain_name, confidence)
    """
    matches = []
    for domain_id, keywords in _DOMAIN_KEYWORDS_FALLBACK.items():
        score = sum(1 for kw in keywords if kw in task) / max(len(keywords), 1)
        if score > 0:
            name = _DOMAIN_DESCRIPTIONS_FALLBACK.get(domain_id, domain_id)
            if "：" in name:
                name = name.split("：")[0]
            elif "/" in name:
                name = name.split("/")[0]
            matches.append((domain_id, name, score))

    if not matches:
        return None, None, 0.0

    matches.sort(key=lambda x: -x[2])
    best_id, best_name, best_score = matches[0]
    return best_id, best_name, min(best_score, 0.95)


async def match_domain(task: str) -> tuple:
    """通过关键词匹配推荐最匹配的领域（DB 版本）。

    Returns:
        (domain_id, domain_name, confidence, reasoning) — 无匹配时返回 (None, None, 0.0, "")
    """
    from core.gateway.domain_registry import DomainRegistry

    try:
        domains = await DomainRegistry.list_domains(source="platform")
    except Exception:
        did, dname, dconf = match_domain_keywords(task)
        return did, dname, dconf, ""

    domain_map = {d["id"]: d for d in domains}
    matches = []
    for domain_id, keywords in _DOMAIN_KEYWORDS_FALLBACK.items():
        if domain_id not in domain_map:
            continue
        score = sum(1 for kw in keywords if kw in task) / max(len(keywords), 1)
        if score > 0:
            matches.append((domain_map[domain_id], score))

    if not matches:
        return None, None, 0.0, ""

    matches.sort(key=lambda x: -x[1])
    best, conf = matches[0]
    return best["id"], best["name"], min(conf, 0.95), ""


async def match_domain_llm(task: str) -> tuple:
    """LLM 领域识别——分析任务文本，返回最匹配的领域。

    比关键词匹配更准确，能处理隐式表达（如"分析《中华民谣》"→音乐）。
    超时或失败时 fallback 到关键词匹配。

    Returns:
        (domain_id, domain_name, confidence, reasoning) — 无匹配时返回 (None, None, 0.0, "")
    """
    try:
        from tools.llm_client import LLMClient

        # 领域识别用推理模型（需要理解隐式表达）
        try:
            from config import get_model_for_scope
            model_name = get_model_for_scope("domain_match")
        except Exception:
            model_name = "deepseek-v4-pro"
        client = LLMClient({"model": model_name})

        dd = await load_domain_descriptions()
        domain_list = "\n".join(
            "- `{}`: {}".format(did, desc) for did, desc in dd.items()
        )
        prompt = (
            f"分析以下任务属于哪个领域。只返回 JSON，格式: `{{\"domain\": \"<domain_id>\", \"confidence\": <0.0-1.0>}}`\n\n"
            f"可用领域：\n{domain_list}\n\n"
            f"任务：{task}\n\n"
            f"如果任务明显不属于以上任何领域，返回 {{\"domain\": null, \"confidence\": 0}}。"
        )

        # 用短超时避免拖慢启动；同时捕获推理模型的思维链
        messages = [{"role": "user", "content": prompt}]
        response, reasoning = await asyncio.wait_for(
            client.chat_messages_with_reasoning(messages), timeout=15.0
        )
        response = response.strip()

        # 提取 JSON（处理 ```json 包裹）
        if "```" in response:
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
            response = response.strip()

        data = json.loads(response)
        domain_id = data.get("domain")
        confidence = float(data.get("confidence", 0.0))

        if not domain_id or confidence < 0.3:
            return None, None, 0.0, reasoning

        # 查 domain name
        from core.gateway.domain_registry import DomainRegistry
        try:
            domains = await DomainRegistry.list_domains(source="platform")
            domain_map = {d["id"]: d for d in domains}
            domain_name = domain_map.get(domain_id, {}).get("name", domain_id)
        except Exception:
            desc = (await load_domain_descriptions()).get(domain_id, "")
            if "：" in desc:
                domain_name = desc.split("：")[0]
            elif "/" in desc:
                domain_name = desc.split("/")[0]
            else:
                domain_name = domain_id

        _logger.info(f"LLM 领域识别: task={task[:60]!r} → {domain_id} ({confidence:.2f})")
        return domain_id, domain_name, min(confidence, 0.98), reasoning

    except Exception as e:
        _logger.warning("LLM 领域识别失败，回退关键词: %s", e)
        return await match_domain(task)
