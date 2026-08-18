"""P2-13 测试：领域通用模板数据化查找（替代硬编码 DOMAIN_GENERAL_MAP）。

验证：新增领域只需提供一个带 general_* capability tag 的通用助手模板
（fixtures/data/agent_templates.json），兜底2 即可命中，无需改代码。
"""
import pytest

from core.orchestration.template_matcher import TemplateMatcher


@pytest.mark.asyncio
async def test_find_domain_general_template_known_domain():
    """已知领域从 agent_templates 数据中按 general_* 标记找到通用助手模板。"""
    matcher = TemplateMatcher()
    tmpl = await matcher._find_domain_general_template("web_novel")
    assert tmpl is not None
    assert tmpl.id == "tpl_novel_general_assistant"
    assert any(str(t).startswith("general_") for t in (tmpl.capability_tags or []))


@pytest.mark.asyncio
async def test_find_domain_general_template_other_domains():
    """其余内容领域同样命中各自 general_* 标记模板。"""
    matcher = TemplateMatcher()
    expected = {
        "comic_static": "tpl_comic_general_assistant",
        "comic_drama": "tpl_drama_general_assistant",
        "music_production": "tpl_music_general_assistant",
        "research_report": "tpl_research_general_assistant",
    }
    for domain, tpl_id in expected.items():
        tmpl = await matcher._find_domain_general_template(domain)
        assert tmpl is not None, f"{domain} 应命中通用模板"
        assert tmpl.id == tpl_id


@pytest.mark.asyncio
async def test_find_domain_general_template_unknown_domain():
    """未知领域返回 None（降级到兜底3 跳过+告警，不崩溃）。"""
    matcher = TemplateMatcher()
    assert await matcher._find_domain_general_template("legal_review") is None
