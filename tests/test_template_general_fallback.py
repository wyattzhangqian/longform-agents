"""P2-13 测试：领域通用模板数据化查找（替代硬编码 DOMAIN_GENERAL_MAP）。

验证：新增领域只需提供一个带 general_* capability tag 的通用助手模板，
兜底2 即可命中，无需改代码。模板数据自包含 seed（fixtures 数据不在公开仓库）。
"""
import pytest

from core.agent.template import AgentTemplate, get_template_registry
from core.orchestration.template_matcher import TemplateMatcher


async def _seed_general_templates() -> None:
    """seed 最小通用模板集，覆盖测试断言需要的 domain + general_* 标记。"""
    registry = get_template_registry()
    specs = [
        ("tpl_novel_general_assistant", "web_novel", ["general_writing"]),
        ("tpl_comic_general_assistant", "comic_static", ["general_comic"]),
        ("tpl_drama_general_assistant", "comic_drama", ["general_drama"]),
        ("tpl_music_general_assistant", "music_production", ["general_music"]),
        ("tpl_research_general_assistant", "research_report", ["general_research"]),
    ]
    for tid, domain, tags in specs:
        await registry.register(
            AgentTemplate(id=tid, name=tid, domain=domain, capability_tags=tags, source="platform")
        )


@pytest.mark.asyncio
async def test_find_domain_general_template_known_domain():
    """已知领域从 agent_templates 数据中按 general_* 标记找到通用助手模板。"""
    await _seed_general_templates()
    matcher = TemplateMatcher()
    tmpl = await matcher._find_domain_general_template("web_novel")
    assert tmpl is not None
    assert tmpl.id == "tpl_novel_general_assistant"
    assert any(str(t).startswith("general_") for t in (tmpl.capability_tags or []))


@pytest.mark.asyncio
async def test_find_domain_general_template_other_domains():
    """其余内容领域同样命中各自 general_* 标记模板。"""
    await _seed_general_templates()
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
