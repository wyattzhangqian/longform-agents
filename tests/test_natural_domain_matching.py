"""natural 路径领域能力匹配（2026-08-10 修复）— domain 专用 agent 能被 natural 选中。

修复前：dag_planner 硬编码 11 个通用能力，LLM 分解只输出通用能力 → domain 专用
agent（漫画/音乐等）永远匹配不到。修复后：识别领域 + 收集领域能力池 → LLM 可输出
领域能力 → _match_agents 同分时优先领域 agent_tpl_*（精确领域优先，同族 fallback）。
"""

import json

import pytest
from unittest.mock import patch

import core.run  # noqa: F401

# 测试专用 agent（避免依赖种子库状态）；领域映射用 mock，不碰 agent_templates 表
_AGENTS = [
    {"id": "builtin_writer", "name": "撰稿人", "type": "custom", "role": "writer",
     "capabilities": ["writing", "content_creation"], "model": "deepseek-v4-flash", "system_prompt": "写手"},
    {"id": "fixture_agent_writer", "name": "撰稿人 B", "type": "custom", "role": "writer",
     "capabilities": ["writing", "documentation"], "model": "deepseek-v4-flash", "system_prompt": "写手"},
    {"id": "agent_tpl_comic_storyboard_artist", "name": "漫画分镜师", "type": "custom", "role": "storyboard",
     "capabilities": ["storyboard", "layout"], "model": "deepseek-v4-flash", "system_prompt": "分镜"},
    {"id": "agent_tpl_drama_storyboard_artist", "name": "漫剧分镜师", "type": "custom", "role": "storyboard",
     "capabilities": ["storyboard", "animatic"], "model": "deepseek-v4-flash", "system_prompt": "分镜"},
    {"id": "agent_tpl_novel_general_assistant", "name": "小说助手", "type": "custom", "role": "assistant",
     "capabilities": ["writing", "planning"], "model": "deepseek-v4-flash", "system_prompt": "小说"},
]

_AGENT_DOMAINS = {
    "agent_tpl_comic_storyboard_artist": "comic_static",
    "agent_tpl_drama_storyboard_artist": "comic_drama",
    "agent_tpl_novel_general_assistant": "web_novel",
}


@pytest.fixture(autouse=True)
async def _seed_env():
    """种入测试 agent（cleanup 后删除）；领域映射用 mock。"""
    from core.storage.database import get_db
    db = await get_db()
    agent_ids = [a["id"] for a in _AGENTS]
    for a in _AGENTS:
        await db.execute(
            """INSERT OR REPLACE INTO agents
               (id, name, type, status, emoji, role, capabilities, model, system_prompt,
                temperature, max_tokens, memory_config, tools, tool_ids, skill_ids,
                model_profile, interaction_config, peers, evolution_history, performance_metrics,
                input_schema, output_schema, tool_namespaces, runtime_info, extra)
               VALUES (?,?,?, 'active', '🤖', ?, ?, ?, ?, 0.7, 4096, '{}', '[]', '[]', '[]',
                       '{}', '{}', '[]', '[]', '{}', '', '', '[]', '{}', '{}')""",
            (a["id"], a["name"], a["type"], a["role"], json.dumps(a["capabilities"]), a["model"], a["system_prompt"]),
        )
    await db.commit()
    yield
    ph = ",".join("?" * len(agent_ids))
    await db.execute(f"DELETE FROM agents WHERE id IN ({ph})", agent_ids)
    await db.commit()


async def _mock_domain(agent_id):
    return _AGENT_DOMAINS.get(agent_id, "")


def _plan(caps):
    return {"phases": [{"phase_id": "p1", "required_capabilities": caps}]}


@pytest.mark.asyncio
async def test_match_comic_storyboard():
    """storyboard + comic_static → 静态漫画分镜 agent（精确领域优先）。"""
    from core.run.natural_runner import _match_agents
    with patch("core.run.natural_runner._agent_domain_from_id_safe", side_effect=_mock_domain):
        r = await _match_agents(_plan(["storyboard"]), domain_id="platform:comic_static")
    assert r[0]["agent_id"] == "agent_tpl_comic_storyboard_artist"


@pytest.mark.asyncio
async def test_match_drama_storyboard():
    """storyboard + comic_drama → 漫剧分镜 agent（精确领域优先）。"""
    from core.run.natural_runner import _match_agents
    with patch("core.run.natural_runner._agent_domain_from_id_safe", side_effect=_mock_domain):
        r = await _match_agents(_plan(["storyboard"]), domain_id="platform:comic_drama")
    assert r[0]["agent_id"] == "agent_tpl_drama_storyboard_artist"


@pytest.mark.asyncio
async def test_match_generic_when_no_domain():
    """无领域 → 写作优先通用 builtin_/fixture_。"""
    from core.run.natural_runner import _match_agents
    with patch("core.run.natural_runner._agent_domain_from_id_safe", side_effect=_mock_domain):
        r = await _match_agents(_plan(["writing"]), domain_id="")
    assert r[0]["agent_id"].startswith(("builtin_", "fixture_"))


@pytest.mark.asyncio
async def test_match_novel_template_when_domain():
    """web_novel 领域 → 写作优先 novel 模板 agent（领域偏好胜 builtin）。"""
    from core.run.natural_runner import _match_agents
    with patch("core.run.natural_runner._agent_domain_from_id_safe", side_effect=_mock_domain):
        r = await _match_agents(_plan(["writing"]), domain_id="platform:web_novel")
    assert r[0]["agent_id"] == "agent_tpl_novel_general_assistant"
