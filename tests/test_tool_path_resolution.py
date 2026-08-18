"""工具路径解析 + 文件工具自我纠正提示测试（修复 Agent 猜文件名 / 双前缀问题）"""

import pytest

import core.run  # noqa: F401 — 先初始化 run 包，规避 core.agent.base 直接导入时的循环依赖
from core.agent.tools import ToolRegistry, ToolExecutor


class TestResolveToolPath:
    def test_plain_filename_joined_with_output_dir(self):
        assert ToolRegistry.resolve_tool_path("proj_x", "script.md") == "proj_x/script.md"

    def test_strips_outputs_prefix(self):
        assert ToolRegistry.resolve_tool_path("proj_x", "outputs/script.md") == "proj_x/script.md"

    def test_no_double_project_prefix(self):
        """Agent 自带项目目录前缀时不再重复拼接"""
        assert ToolRegistry.resolve_tool_path("proj_x", "proj_x/script.md") == "proj_x/script.md"

    def test_no_double_prefix_with_outputs(self):
        assert ToolRegistry.resolve_tool_path("proj_x", "outputs/proj_x/script.md") == "proj_x/script.md"

    def test_repeated_prefix_collapsed(self):
        assert ToolRegistry.resolve_tool_path("proj_x", "proj_x/proj_x/a.md") == "proj_x/a.md"

    def test_no_output_dir(self):
        assert ToolRegistry.resolve_tool_path("", "a.md") == "a.md"

    def test_similar_dir_name_not_stripped(self):
        """前缀必须整段匹配，proj_xy 不应被 proj_x 误剥离"""
        assert ToolRegistry.resolve_tool_path("proj_x", "proj_xy/a.md") == "proj_x/proj_xy/a.md"


@pytest.mark.asyncio
class TestFileToolsSelfCorrection:
    async def test_file_read_not_found_lists_dir(self, tmp_path, monkeypatch):
        """文件不存在时报错附带目录真实文件清单，Agent 可下一轮自我纠正"""
        monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", tmp_path)
        proj = tmp_path / "proj_a"
        proj.mkdir()
        (proj / "real_output.md").write_text("内容", encoding="utf-8")

        result = await ToolRegistry._file_read("proj_a/guessed_name.md")
        assert "[错误] 文件不存在" in result
        assert "real_output.md" in result

    async def test_file_read_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", tmp_path)
        proj = tmp_path / "proj_a"
        proj.mkdir()
        (proj / "data.md").write_text("hello", encoding="utf-8")
        assert await ToolRegistry._file_read("proj_a/data.md") == "hello"

    async def test_missing_required_arg_includes_usage(self):
        """缺参报错附带必填参数提示"""
        executor = ToolExecutor(ToolRegistry.get_instance(), output_dir="proj_a")
        result = await executor.execute(
            {"needs_tool": True, "tool_name": "file_read", "tool_args": {}},
        )
        assert "缺少必填参数: path" in result
        assert "必填参数" in result


class TestWorkspaceManifest:
    def test_manifest_lists_files(self, tmp_path, monkeypatch):
        from core.agent.base import BaseAgent
        from core.agent.types import AgentDefinition

        monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", tmp_path)
        proj = tmp_path / "proj_m"
        proj.mkdir()
        (proj / "upstream_script.md").write_text("剧本", encoding="utf-8")

        agent = BaseAgent(AgentDefinition(id="a1", name="测试", emoji="🤖"))
        agent.plug(bus=None, output_dir=str(proj))
        manifest = agent._workspace_file_manifest()
        assert "工作区文件清单" in manifest
        assert "upstream_script.md" in manifest

    def test_manifest_empty_workspace(self, tmp_path, monkeypatch):
        from core.agent.base import BaseAgent
        from core.agent.types import AgentDefinition

        monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", tmp_path)
        proj = tmp_path / "proj_empty"
        proj.mkdir()

        agent = BaseAgent(AgentDefinition(id="a1", name="测试", emoji="🤖"))
        agent.plug(bus=None, output_dir=str(proj))
        manifest = agent._workspace_file_manifest()
        assert "工作区为空" in manifest
