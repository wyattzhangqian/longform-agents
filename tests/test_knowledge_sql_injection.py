"""P1-5 修复测试：resolve_knowledge_text 主路径走 SQL 结构化知识（KnowledgeInjector）。

修复前走 TF-IDF 向量检索（VectorKnowledgeBase），结构化 knowledge_entries 入库却
不参与 Agent 执行。修复后：SQL 知识为主路径，空时回退 TF-IDF 保底。
"""
import pytest
from unittest.mock import AsyncMock, patch

from core.run.knowledge_inject import resolve_knowledge_text
from core.run.phase_spec import KnowledgeProfile, PhaseSpec
from core.run.run_context import RunContext


def _make_phase(packs):
    return PhaseSpec(
        id="phase_1",
        agent_id="agent_x",
        knowledge_profile=KnowledgeProfile(packs=packs),
    )


@pytest.mark.asyncio
async def test_sql_knowledge_primary_path():
    """packs 有 SQL 知识时走 KnowledgeInjector（含 platform: 前缀规范化），不回退 TF-IDF。"""
    phase = _make_phase(["web_novel"])
    ctx = RunContext(project_id="p15_proj", task="写一章小说")

    with patch("core.knowledge.injector.KnowledgeInjector") as m_injector_cls, \
         patch("core.knowledge.get_knowledge_manager") as m_km:
        m_injector = m_injector_cls.return_value
        m_injector.build_knowledge_context = AsyncMock(return_value="**世界设定**\n魔法体系")

        text = await resolve_knowledge_text(phase, ctx, "agent_x")

    assert "领域知识" in text
    m_injector.build_knowledge_context.assert_awaited_once_with(
        domain_id="platform:web_novel",
        # 2026-08-09：run phase(phase_1) 非领域 phase → 注入领域全部写作规范（不过滤）
        phase_id="",
        task="写一章小说",
        agent_role="agent_x",
    )
    m_km.assert_not_called()  # SQL 命中则不回退


@pytest.mark.asyncio
async def test_fallback_to_tfidf_when_sql_empty():
    """SQL 无命中时回退 TF-IDF 向量检索保底。"""
    phase = _make_phase(["web_novel"])
    ctx = RunContext(project_id="p15_proj", task="写一章小说")

    with patch("core.knowledge.injector.KnowledgeInjector") as m_injector_cls, \
         patch("core.knowledge.get_knowledge_manager") as m_km:
        m_injector = m_injector_cls.return_value
        m_injector.build_knowledge_context = AsyncMock(return_value="")

        fake_ak = AsyncMock()
        fake_ak.build_context = AsyncMock(return_value="TF-IDF 兜底知识块")
        km = m_km.return_value
        km.get_agent_knowledge.return_value = fake_ak

        text = await resolve_knowledge_text(phase, ctx, "agent_x")

    assert "TF-IDF 兜底知识块" in text
    m_km.assert_called_once()
