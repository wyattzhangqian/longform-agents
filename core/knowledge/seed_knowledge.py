"""平台内置知识库种子数据（数据在 fixtures/data/knowledge/*.json，P1-11 下沉）

执行逻辑：
1. 写入基础数据（按 domain+title 去重）
2. 应用补丁——删除重复条目
3. 应用补丁——替换条目
4. 应用补丁——新增条目

P1-11: 种子数据已从 fixtures/knowledge/*.py（3073 行）下沉到
fixtures/data/knowledge/*.json，本模块只负责加载，不再 import fixtures。
"""

import hashlib
import json
from pathlib import Path

from core.storage.database import get_db
from core.logging import get_logger

_logger = get_logger("knowledge.seed")

# 数据文件目录：core/knowledge/seed_knowledge.py → 项目根/fixtures/data/knowledge
_KNOWLEDGE_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "data" / "knowledge"


def _load_list(name: str) -> list:
    """从 fixtures/data/knowledge 加载一个列表常量；加载失败回退空列表并明确报错。"""
    try:
        with open(_KNOWLEDGE_DATA_DIR / f"{name}.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _logger.error("知识种子数据加载失败 {}（路径 {}）: {} —— 该组知识将缺失", name, _KNOWLEDGE_DATA_DIR, e)
        return []


# ═══ 基础数据 ═══
ENTRIES_COMIC_DRAMA = _load_list("base_comic_drama")
ENTRIES_MUSIC_PRODUCTION = _load_list("base_music_production")
ENTRIES_COMIC_STATIC = _load_list("base_comic_static")
ENTRIES_WEB_NOVEL = _load_list("base_web_novel")
ENTRIES_RESEARCH_REPORT = _load_list("base_research_report")
ENTRIES_GENERAL = _load_list("base_general")

# ═══ 补丁数据（新增 + 删除/替换指令） ═══
PATCH_COMIC_DRAMA_STORY = _load_list("patch_comic_drama_story")
PATCH_MUSIC_SUBGENRES = _load_list("patch_music_subgenres")
PATCH_WEB_NOVEL_EXAMPLES = _load_list("patch_web_novel_examples")
PATCH_GENERAL_AGENT_BEHAVIOR = _load_list("patch_general_agent_behavior")
PATCH_RESEARCH_CHINA = _load_list("patch_research_china")
REMOVE_COMIC_STATIC_TITLES = _load_list("remove_comic_static_titles")
PATCH_COMIC_STATIC_UNIQUE = _load_list("patch_comic_static_unique")
ENTRIES_MUSIC_PRO = _load_list("pro_music")
ENTRIES_COMIC_PRO = _load_list("pro_comic")
ENTRIES_DRAMA_PRO = _load_list("pro_drama")
ENTRIES_NOVEL_PRO = _load_list("pro_novel")
ENTRIES_RESEARCH_PRO = _load_list("pro_research")


def _make_entry_id(domain: str, title: str) -> str:
    """生成稳定的条目ID（基于 domain+title 的 hash，保证幂等）"""
    raw = f"{domain}:{title}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"platform:{domain}:ext_{short_hash}"


def _domain_id(domain_short: str) -> str:
    """短域名 → 完整 domain_id"""
    return f"platform:{domain_short}"


async def seed_platform_knowledge():
    """初始化平台内置知识库（幂等，含扩展数据和补丁）"""
    db = await get_db()

    # ═══════════════════════════════════════════════════
    # 第一步：写入基础数据（160条）
    # ═══════════════════════════════════════════════════

    all_base_entries = (
        ENTRIES_COMIC_DRAMA +
        ENTRIES_MUSIC_PRODUCTION +
        ENTRIES_COMIC_STATIC +
        ENTRIES_WEB_NOVEL +
        ENTRIES_RESEARCH_REPORT +
        ENTRIES_GENERAL
    )

    inserted_base = 0
    for entry in all_base_entries:
        domain_short = entry["domain"]
        full_domain_id = _domain_id(domain_short)
        title = entry["title"]
        entry_id = _make_entry_id(domain_short, title)

        # 幂等：按 domain_id + title 检查是否已存在
        cursor = await db.execute(
            "SELECT id FROM knowledge_entries WHERE domain_id = ? AND title = ?",
            (full_domain_id, title),
        )
        if await cursor.fetchone():
            continue  # 已存在，跳过

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
        inserted_base += 1

    # ═══════════════════════════════════════════════════
    # 第二步：应用补丁——删除重复条目
    # ═══════════════════════════════════════════════════

    deleted = 0
    for title in REMOVE_COMIC_STATIC_TITLES:
        cursor = await db.execute(
            "DELETE FROM knowledge_entries WHERE domain_id = ? AND title = ?",
            (_domain_id("comic_static"), title),
        )
        deleted += cursor.rowcount

    # ═══════════════════════════════════════════════════
    # 第三步：应用补丁——替换条目（先删后增）
    # ═══════════════════════════════════════════════════

    replaced = 0
    for entry in PATCH_WEB_NOVEL_EXAMPLES:
        title = entry["title"]
        full_domain_id = _domain_id("web_novel")

        await db.execute(
            "DELETE FROM knowledge_entries WHERE domain_id = ? AND title = ?",
            (full_domain_id, title),
        )

        entry_id = _make_entry_id("web_novel", title)
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
        replaced += 1

    # ═══════════════════════════════════════════════════
    # 第四步：应用补丁——新增条目
    # ═══════════════════════════════════════════════════

    all_patch_additions = (
        PATCH_COMIC_DRAMA_STORY +       # +8 漫剧故事
        PATCH_MUSIC_SUBGENRES +          # +8 音乐子风格
        PATCH_GENERAL_AGENT_BEHAVIOR +   # +5 通用Agent行为
        PATCH_RESEARCH_CHINA +           # +4 研报中国特色
        PATCH_COMIC_STATIC_UNIQUE +      # +5 漫画翻页独有
        ENTRIES_MUSIC_PRO +              # +13 音乐制作专业知识（领域1）
        ENTRIES_COMIC_PRO +              # +8 漫画静态专业知识（领域2）
        ENTRIES_DRAMA_PRO +              # +6 漫剧动态专业知识（领域3）
        ENTRIES_NOVEL_PRO +              # +8 网文小说专业知识（领域4）
        ENTRIES_RESEARCH_PRO             # +6 研究报告专业知识（领域5）
    )

    inserted_patch = 0
    for entry in all_patch_additions:
        domain_short = entry["domain"]
        full_domain_id = _domain_id(domain_short)
        title = entry["title"]
        entry_id = _make_entry_id(domain_short, title)

        cursor = await db.execute(
            "SELECT id FROM knowledge_entries WHERE domain_id = ? AND title = ?",
            (full_domain_id, title),
        )
        if await cursor.fetchone():
            continue

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
        inserted_patch += 1

    await db.commit()

    _logger.info(
        "知识库种子完成: base=%d条写入, patch=%d条新增, %d条替换, %d条删除",
        inserted_base, inserted_patch, replaced, deleted,
    )
