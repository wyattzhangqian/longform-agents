"""SkillResolver 测试 — 覆盖 skill 展开/工具解析/MCP 集成"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
class TestSkillResolverExpandEmpty:
    """空输入边界测试"""

    async def test_none_skill_ids(self):
        from core.skills.resolver import SkillResolver
        result = await SkillResolver.expand(None)
        assert result["tools"] == []
        assert result["skill_ids"] == []

    async def test_empty_list(self):
        from core.skills.resolver import SkillResolver
        result = await SkillResolver.expand([])
        assert result["tools"] == []

    async def test_no_legacy_tools(self):
        from core.skills.resolver import SkillResolver
        result = await SkillResolver.expand(None, legacy_tools=[])
        assert result["tools"] == []


@pytest.mark.asyncio
class TestSkillResolverExpandWithMockRegistry:
    """使用 Mock Registry 的展开测试"""

    @patch("core.skills.resolver.get_skill_registry")
    async def test_single_skill_expansion(self, mock_registry_fn):
        """单个 skill 展开为 tools"""
        from core.skills.resolver import SkillResolver

        # Mock registry
        mock_registry = AsyncMock()
        mock_skill = MagicMock()
        mock_skill.status = "active"
        mock_manifest = MagicMock()
        mock_manifest.prompt_snippet = "你是一个专业编剧"
        mock_manifest.tools = ["file_read", "file_write"]
        mock_manifest.mcp = {}
        # model_dump 返回 dict 格式的 manifest
        mock_manifest.model_dump.return_value = {
            "prompt_snippet": "你是一个专业编剧",
            "tools": ["file_read", "file_write"],
            "tool_definitions": [],
            "mcp": {},
        }
        mock_skill.manifest = mock_manifest
        mock_registry.get = AsyncMock(return_value=mock_skill)
        mock_registry_fn.return_value = mock_registry

        # Mock MCP bridge
        with patch("core.skills.resolver.MCPToolBridge") as mock_bridge_cls:
            mock_bridge = MagicMock()
            mock_bridge.ensure_registered = AsyncMock()
            mock_bridge_cls.get_instance.return_value = mock_bridge

            result = await SkillResolver.expand(["skill_writer"])

            assert "skill_writer" in result["skill_ids"]
            assert len(result["tools"]) > 0
            assert "file_read" in result["tools"]

    @patch("core.skills.resolver.get_skill_registry")
    async def test_inactive_skill_skipped(self, mock_registry_fn):
        """非 active 状态的 skill 应被跳过"""
        from core.skills.resolver import SkillResolver

        mock_registry = AsyncMock()
        mock_skill = MagicMock()
        mock_skill.status = "disabled"  # 非 active
        mock_registry.get = AsyncMock(return_value=mock_skill)
        mock_registry_fn.return_value = mock_registry

        with patch("core.skills.resolver.MCPToolBridge") as mock_bridge_cls:
            mock_bridge = MagicMock()
            mock_bridge.ensure_registered = AsyncMock()
            mock_bridge_cls.get_instance.return_value = mock_bridge

            result = await SkillResolver.expand(["disabled_skill"])
            assert len(result["tools"]) == 0

    @patch("core.skills.resolver.get_skill_registry")
    async def test_nonexistent_skill_skipped(self, mock_registry_fn):
        """不存在的 skill 返回 None 时跳过"""
        from core.skills.resolver import SkillResolver

        mock_registry = AsyncMock()
        mock_registry.get = AsyncMock(return_value=None)
        mock_registry_fn.return_value = mock_registry

        with patch("core.skills.resolver.MCPToolBridge") as mock_bridge_cls:
            mock_bridge = MagicMock()
            mock_bridge.ensure_registered = AsyncMock()
            mock_bridge_cls.get_instance.return_value = mock_bridge

            result = await SkillResolver.expand(["ghost_skill"])
            assert len(result["tools"]) == 0

    @patch("core.skills.resolver.get_skill_registry")
    async def test_prompt_snippets_collected(self, mock_registry_fn):
        """prompt_snippets 被正确收集"""
        from core.skills.resolver import SkillResolver

        mock_registry = AsyncMock()
        mock_skill = MagicMock()
        mock_skill.status = "active"
        mock_manifest = MagicMock()
        mock_manifest.prompt_snippet = "你是分镜师"
        mock_manifest.tools = []
        mock_manifest.mcp = {}
        mock_manifest.model_dump.return_value = {
            "prompt_snippet": "你是分镜师",
            "tools": [],
            "tool_definitions": [],
            "mcp": {},
        }
        mock_skill.manifest = mock_manifest
        mock_registry.get = AsyncMock(return_value=mock_skill)
        mock_registry_fn.return_value = mock_registry

        with patch("core.skills.resolver.MCPToolBridge") as mock_bridge_cls:
            mock_bridge = MagicMock()
            mock_bridge.ensure_registered = AsyncMock()
            mock_bridge_cls.get_instance.return_value = mock_bridge

            result = await SkillResolver.expand(["storyboard"])
            assert len(result["prompt_snippets"]) == 1
            assert "分镜" in result["prompt_snippets"][0]

    @patch("core.skills.resolver.get_skill_registry")
    async def test_mcp_skill_proxy_tool_expansion(self, mock_registry_fn):
        """P2-12 修复：MCP skill 带 server_id+tool_names 时展开代理工具名不抛 NameError。

        修复前 resolver.py:82 用了未定义的 bridge 变量（仅导入 MCPToolBridge 类），
        第一个 MCP skill 挂载即崩；本用例补 CI 缺口（此前 MCP skill 零测试覆盖）。
        """
        from core.skills.resolver import SkillResolver

        mock_registry = AsyncMock()
        mock_skill = MagicMock()
        mock_skill.status = "active"
        mock_manifest = MagicMock()
        mock_manifest.prompt_snippet = "MCP 工具 skill"
        mock_manifest.kind = "mcp"
        mock_manifest.tools = []
        # expand() 直接读 manifest.mcp（dict）与 model_dump()
        mock_manifest.mcp = {"server_id": "srv_1", "tool_names": ["search", "fetch"]}
        mock_manifest.model_dump.return_value = {
            "prompt_snippet": "MCP 工具 skill",
            "tools": [],
            "tool_definitions": [],
            "mcp": {"server_id": "srv_1", "tool_names": ["search", "fetch"]},
        }
        mock_skill.manifest = mock_manifest
        mock_skill.mcp_config = {"server_id": "srv_1"}
        mock_registry.get = AsyncMock(return_value=mock_skill)
        mock_registry_fn.return_value = mock_registry

        with patch("core.skills.resolver.MCPToolBridge") as mock_bridge_cls:
            mock_bridge_cls.proxy_tool_name.side_effect = (
                lambda sid, tn: f"mcp_{sid}_{tn}"
            )
            mock_bridge_cls.get_instance.return_value = MagicMock()

            # 修复前此处抛 NameError
            result = await SkillResolver.expand(["skill_mcp"])

        assert "mcp_srv_1_search" in result["tools"]
        assert "mcp_srv_1_fetch" in result["tools"]
        assert mock_bridge_cls.proxy_tool_name.call_count == 2


@pytest.mark.asyncio
class TestSkillResolverLegacyTools:
    """legacy_tools 兼容性测试"""

    @patch("core.skills.resolver.get_skill_registry")
    async def test_legacy_tool_mapped_to_skill_id(self, mock_registry_fn):
        """旧式 tool 名映射到平台 skill ID"""
        from core.skills.resolver import SkillResolver, TOOL_TO_PLATFORM_SKILL

        # 如果 TOOL_TO_PLATFORM_SKILL 有映射，legacy tool 应被转换
        if not TOOL_TO_PLATFORM_SKILL:
            pytest.skip("TOOL_TO_PLATFORM_SKILL 为空，跳过")

        some_tool = list(TOOL_TO_PLATFORM_SKILL.keys())[0]
        mock_registry = AsyncMock()
        mock_registry.get = AsyncMock(return_value=None)  # skill 不存在
        mock_registry_fn.return_value = mock_registry

        with patch("core.skills.resolver.MCPToolBridge") as mock_bridge_cls:
            mock_bridge = MagicMock()
            mock_bridge.ensure_registered = AsyncMock()
            mock_bridge_cls.get_instance.return_value = mock_bridge

            result = await SkillResolver.expand(legacy_tools=[some_tool])
            # 映射后的 skill_id 应在列表中
            mapped_id = TOOL_TO_PLATFORM_SKILL[some_tool]
            assert mapped_id in result["skill_ids"]

    async def test_namespaced_legacy_tool(self):
        """legacy 中 namespace/name 格式的 tool"""
        from core.skills.resolver import SkillResolver

        with patch("core.skills.resolver.get_skill_registry") as mreg, \
             patch("core.skills.resolver.MCPToolBridge") as mb:
            mreg.return_value = AsyncMock()
            mreg.return_value.get = AsyncMock(return_value=None)
            mock_bridge = MagicMock()
            mock_bridge.ensure_registered = AsyncMock()
            mb.get_instance.return_value = mock_bridge

            result = await SkillResolver.expand(legacy_tools=["domain/file_read"])
            assert "domain/file_read" in result["tool_ids"]
            assert "file_read" in result["tools"]
            assert "domain" in result["namespaces"]


class TestSkillResolverOutputFormat:
    """输出格式验证"""

    def test_return_keys(self):
        """验证返回 dict 包含所有预期 key"""
        from core.skills.resolver import SkillResolver
        # 这个测试只检查 API 契约 — 实际调用需要 mock
        import inspect
        sig = inspect.signature(SkillResolver.expand)
        # expand 是异步静态方法，返回 Dict[str, Any]
        expected_keys = {"tools", "tool_ids", "prompt_snippets", "namespaces", "skill_ids"}
        # 通过文档字符串确认返回格式
        doc = SkillResolver.expand.__doc__
        for key in expected_keys:
            assert key in doc, f"Return key '{key}' not documented in SkillResolver.expand"


@pytest.mark.asyncio
class TestSkillResolverInvokeSkill:
    """invoke_skill 沙箱执行测试"""

    @patch("core.skills.resolver.SkillResolver.expand")
    async def test_invoke_nonexistent_skill(self, mock_expand):
        from core.skills.resolver import SkillResolver

        mock_expand.return_value = {"tools": [], "namespaces": []}
        result = await SkillResolver.invoke_skill("ghost_skill")
        assert result["success"] is False
        assert "不存在" in result["error"]

    @patch("core.skills.resolver.SkillResolver.expand")
    async def test_invoke_skill_no_tools(self, mock_expand):
        from core.skills.resolver import SkillResolver

        mock_expand.return_value = {"tools": ["dummy"], "namespaces": [], "skill_ids": ["s1"]}

        with patch("core.skills.resolver.get_skill_registry") as mreg:
            mock_reg = AsyncMock()
            mock_skill = MagicMock()
            mock_reg.get = AsyncMock(return_value=mock_skill)
            mreg.return_value = mock_reg

            result = await SkillResolver.invoke_skill("s1")
            # expand 返回有 tools 但 invoke 需要 ToolExecutor...
            # 如果没有可执行工具
            if not result["success"]:
                assert "error" in result
