"""ModelRegistry / CapabilityResolver 架构测试"""

import pytest

from core.models.types import AgentModelProfile, ModelBinding, PLATFORM_DEFAULT
from core.models.registry import get_model_registry
from core.models.router import ModelRouter, resolve_llm_config


class TestModelRouter:
    def test_platform_default_resolves(self):
        cfg = resolve_llm_config(model=PLATFORM_DEFAULT)
        assert cfg.get("model")
        assert cfg.get("temperature") is not None

    def test_agent_override(self):
        from core.agent.types import AgentDefinition

        agent = AgentDefinition(
            id="a1",
            name="test",
            model_profile=AgentModelProfile(
                llm=ModelBinding(model_id="deepseek-v4-pro", temperature=0.2, max_tokens=2048),
            ),
        )
        cfg = ModelRouter.resolve_agent(agent, "llm")
        assert cfg["model"] == "deepseek-v4-pro"
        assert cfg["temperature"] == 0.2

    def test_image_requires_binding(self):
        from core.agent.types import AgentDefinition

        agent = AgentDefinition(id="a1", name="t")
        assert agent.model_profile.image is None


class TestModelRegistry:
    def test_catalog_has_modalities(self):
        reg = get_model_registry()
        cat = reg.catalog_for_api()
        assert len(cat["llm"]) >= 2
        assert len(cat["image"]) >= 1
        assert len(cat["video"]) >= 1

    def test_apply_settings(self):
        reg = get_model_registry()
        before = reg.get_default_id("llm")
        reg.apply_settings({"llm_model": "deepseek-v4-pro"})
        assert reg.get_default_id("llm") == "deepseek-v4-pro"
        reg.apply_settings({"llm_model": before})


@pytest.mark.asyncio
class TestCapabilityResolver:
    async def test_agent_tool_ids_merged(self):
        from core.agent.types import AgentDefinition
        from core.capabilities.resolver import CapabilityResolver

        agent = AgentDefinition(
            id="a1",
            name="t",
            tool_ids=["file_read", "web_search"],
        )
        ctx = await CapabilityResolver.expand(agent)
        assert "file_read" in ctx["tools"]
        assert "web_search" in ctx["tools"]

    async def test_instruction_skill_no_tools_without_flag(self):
        from core.agent.types import AgentDefinition
        from core.capabilities.resolver import CapabilityResolver
        from core.skills.models import SkillDefinition, SkillManifest

        # 仅 mock expand path — 无 skill in DB 时 skill_ids 空
        agent = AgentDefinition(id="a1", name="t", skill_ids=[], tool_ids=["file_write"])
        ctx = await CapabilityResolver.expand(agent)
        assert ctx["tools"] == ["file_write"]
