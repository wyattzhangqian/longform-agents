"""平台内置知识库种子数据（数据在 fixtures/data/knowledge/*.json）

open-core 边界：目录里有什么文件就加载什么。缺失的领域知识文件（如漫画/音乐/
研报）不打 ERROR——那是正常的开源边界，只打 INFO。

执行逻辑（按文件名前缀自动分类，新增知识文件无需改代码）：
- base_*     → 基础数据（去重写入）
- remove_*   → 删除列表（remove_{domain}_titles.json）
- patch_*    → 替换补丁（先删后增）
- pro_*      → 专业知识（去重写入）
"""

import hashlib
import json
from pathlib import Path

from core.logging import get_logger
from core.storage.database import get_db

_logger = get_logger("knowledge.seed")

# 数据文件目录：core/knowledge/seed_knowledge.py → 项目根/fixtures/data/knowledge
_KNOWLEDGE_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "data" / "knowledge"


def _load_list(name: str) -> list:
    """从 fixtures/data/knowledge 加载一个列表；文件不存在打 INFO（open-core 边界正常）。"""
    try:
        with open(_KNOWLEDGE_DATA_DIR / f"{name}.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        _logger.info("知识种子数据文件不存在（open-core 边界，跳过）: %s.json", name)
        return []
    except Exception as e:
        _logger.warning("知识种子数据加载失败 %s: %s", name, e)
        return []


def _scan_knowledge():
    """扫描 fixtures/data/knowledge/*.json，按文件名前缀分类。"""
    base: list = []
    remove: list = []   # (domain_short, titles)
    patches: list = []  # 替换补丁 entries
    pro: list = []
    if not _KNOWLEDGE_DATA_DIR.exists():
        _logger.info("知识库数据目录不存在（open-core 边界）: %s", _KNOWLEDGE_DATA_DIR)
        return base, remove, patches, pro
    for f in sorted(_KNOWLEDGE_DATA_DIR.glob("*.json")):
        name = f.stem
        data = _load_list(name)
        if name.startswith("base_"):
            base.extend(data)
        elif name.startswith("pro_"):
            pro.extend(data)
        elif name.startswith("remove_"):
            # remove_{domain}_titles
            domain_short = name[len("remove_"):]
            if domain_short.endswith("_titles"):
                domain_short = domain_short[: -len("_titles")]
            remove.append((domain_short, data))
        elif name.startswith("patch_"):
            patches.extend(data)
        else:
            _logger.info("知识种子跳过未分类文件: %s.json", name)
    return base, remove, patches, pro


def _make_entry_id(domain: str, title: str) -> str:
    """生成稳定的条目ID（基于 domain+title 的 hash，保证幂等）"""
    raw = f"{domain}:{title}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"platform:{domain}:ext_{short_hash}"


def _domain_id(domain_short: str) -> str:
    """短域名 → 完整 domain_id"""
    return f"platform:{domain_short}"


async def _insert_entry(db, entry: dict) -> bool:
    """写入一条知识条目（幂等：domain_id+title 已存在则跳过）。返回是否新增。"""
    domain_short = entry["domain"]
    full_domain_id = _domain_id(domain_short)
    title = entry["title"]
    entry_id = _make_entry_id(domain_short, title)

    cursor = await db.execute(
        "SELECT id FROM knowledge_entries WHERE domain_id = ? AND title = ?",
        (full_domain_id, title),
    )
    if await cursor.fetchone():
        return False

    await db.execute(
        """INSERT INTO knowledge_entries
           (id, domain_id, phase_id, category, title, content, tags,
            priority, inject_mode, source, owner_id, char_count, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'platform', '', ?, 0)""",
        (
            entry_id,
            full_domain_id,
            entry.get("phase_id", ""),
            entry["category"],
            title,
            entry["content"],
            json.dumps(entry.get("tags", []), ensure_ascii=False),
            entry.get("priority", 50),
            entry.get("inject_mode", "always"),
            len(entry["content"]),
        ),
    )
    return True


async def seed_platform_knowledge():
    """初始化平台内置知识库（幂等，含基础数据、替换补丁与专业知识）"""
    db = await get_db()
    base, remove, patches, pro = _scan_knowledge()

    # ═══ 第一步：基础数据（去重写入）═══
    inserted_base = 0
    for entry in base:
        if await _insert_entry(db, entry):
            inserted_base += 1

    # ═══ 第二步：删除列表 ═══
    deleted = 0
    for domain_short, titles in remove:
        for title in titles:
            cursor = await db.execute(
                "DELETE FROM knowledge_entries WHERE domain_id = ? AND title = ?",
                (_domain_id(domain_short), title),
            )
            deleted += cursor.rowcount

    # ═══ 第三步：替换补丁（先删后增）═══
    replaced = 0
    for entry in patches:
        domain_short = entry["domain"]
        title = entry["title"]
        await db.execute(
            "DELETE FROM knowledge_entries WHERE domain_id = ? AND title = ?",
            (_domain_id(domain_short), title),
        )
        await _insert_entry(db, entry)
        replaced += 1

    # ═══ 第四步：专业知识（去重写入）═══
    inserted_patch = 0
    for entry in pro:
        if await _insert_entry(db, entry):
            inserted_patch += 1

    await db.commit()
    _logger.info(
        "知识库种子完成: base={}条写入, patch={}条新增, {}条替换, {}条删除",
        inserted_base, inserted_patch, replaced, deleted,
    )
