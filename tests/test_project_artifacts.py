"""项目产物：期望文件名、中间态过滤、产物 Tab 可见性"""

import json
import pytest

from core.communication.content_format import (
    is_ephemeral_agent_output,
    extract_deliverable_for_path,
    extract_deliverable_body,
)
from core.vault.project_artifact_service import (
    ProjectArtifactService,
    expected_artifacts_for_phase,
    is_user_facing_artifact,
    resolve_phase_step,
)


class TestResolvePhaseStep:
    def test_explicit_step(self):
        assert resolve_phase_step("fixture_agent_any_script", {"step": "script"}) == "script"

    def test_no_step_returns_phase_id(self):
        # 领域中立：不再从 agent_id 推断特定领域 step
        assert resolve_phase_step("fixture_agent_comic_character", {}) == "fixture_agent_comic_character"

    def test_canonical_phase_id(self):
        assert resolve_phase_step("storyboard", {}) == "storyboard"


class TestExpectedArtifacts:
    def test_explicit_in_metadata(self):
        files = expected_artifacts_for_phase({"expected_artifacts": ["script.md"]}, "any_phase")
        assert files == ["script.md"]

    def test_no_explicit_returns_empty(self):
        # 领域中立：不再硬编码漫剧文件名
        files = expected_artifacts_for_phase({}, "fixture_agent_comic_visual_director")
        assert files == []

    def test_string_expected(self):
        files = expected_artifacts_for_phase({"expected_artifacts": "output.md"}, "phase1")
        assert files == ["output.md"]


class TestExtractFileWrite:
    LOOSE_SAMPLE = '''说明文字

```json
{
  "needs_tool": true,
  "tool_name": "file_write",
  "tool_args": {
    "path": "script.md",
    "content": "# 《细语无声》 第一话 脚本

## 分场1：雨夜
- 场景描述
"
  }
}
```'''

    def test_loose_multiline_file_write(self):
        body = extract_deliverable_for_path(self.LOOSE_SAMPLE, 'script.md')
        assert body.startswith('# 《细语无声》')
        assert '分场1' in body

    def test_fenced_markdown_block(self):
        sample = '前言\n\n```markdown\n# 风格圣经\n\n## 色彩\n- 主色\n```'
        body = extract_deliverable_body(sample, 'style-bible.md')
        assert body.startswith('# 风格圣经')

    def test_rejects_agent_thinking_preamble(self):
        from core.communication.content_format import (
            contains_agent_process_preamble,
            is_clean_deliverable_body,
        )
        thinking = (
            "好的，剧本师已就位！我先用 web_search 搜索灵感。\n\n"
            "**第一步** 搜索\n**第二步** file_write 写入 script.md\n\n"
            "# 漫剧剧本：《测试》\n\n## 分场\n- 场景一\n"
        )
        assert contains_agent_process_preamble(thinking)
        body = extract_deliverable_body(thinking, 'script.md')
        assert body.startswith('# 漫剧剧本')
        assert 'web_search' not in body
        assert is_clean_deliverable_body(body, 'script.md')

    def test_ask_peer_json(self):
        payload = {
            "needs_tool": True,
            "tool_name": "ask_peer",
            "tool_args": {"to": "script", "question": "where is script.md?"},
        }
        assert is_ephemeral_agent_output(payload)

    def test_tool_use_only(self):
        payload = {"tool_use": [{"tool_name": "file_read", "tool_args": {"path": "script.md"}}]}
        assert is_ephemeral_agent_output(payload)

    def test_file_write_is_deliverable(self):
        payload = {
            "tool_use": [{
                "tool_name": "file_write",
                "tool_args": {"path": "script.md", "content": "# Title\n\nBody"},
            }]
        }
        assert not is_ephemeral_agent_output(payload)

    def test_real_markdown(self):
        assert not is_ephemeral_agent_output("# Script\n\n## Scene 1")


class TestExtractResultText:
    def test_skips_ask_peer_dict(self):
        text = ProjectArtifactService.extract_result_text({
            "needs_tool": True,
            "tool_name": "ask_peer",
            "tool_args": {},
        })
        assert text == ""

    def test_keeps_markdown_result(self):
        text = ProjectArtifactService.extract_result_text({"result": "# Script\n\nHello"})
        assert "Script" in text


class TestUserFacingArtifact:
    def test_materialized_hidden(self):
        row = {
            "type": "materialized",
            "name": "fixture_agent_comic_script.md",
            "file_path": "outputs/proj_x/fixture_agent_comic_script.md",
            "metadata": {"source": "phase_materialize"},
        }
        assert not is_user_facing_artifact(row)

    def test_script_md_visible(self):
        row = {
            "type": "file",
            "name": "script.md",
            "file_path": "outputs/proj_x/script.md",
            "metadata": {},
        }
        # .md 文件且非 agent fallback → 可见
        assert is_user_facing_artifact(row)

    def test_agent_fallback_md_hidden(self):
        row = {
            "type": "file",
            "name": "fixture_agent_comic_char_ref.md",
            "file_path": "outputs/proj_x/fixture_agent_comic_char_ref.md",
            "metadata": {},
        }
        assert not is_user_facing_artifact(row)

    def test_image_visible(self):
        row = {
            "type": "file",
            "name": "frame_001.png",
            "file_path": "outputs/proj_x/frame_001.png",
            "metadata": {},
        }
        assert is_user_facing_artifact(row)


@pytest.mark.asyncio
async def test_materialize_never_writes_process_text(tmp_path, monkeypatch):
    import core.vault.project_artifact_service as pas

    out_root = tmp_path / "outputs"
    monkeypatch.setattr(pas, "OUTPUTS_ROOT", out_root)
    pid = "proj_test"
    thinking = (
        "剧本师已就位，先 web_search。\n\n**第一步** 搜索\n\n"
        "# 标题\n\n## 节\n- 内容"
    )
    refs = await ProjectArtifactService.materialize_phase_output(
        pid, "agent_a", "script", {"result": thinking}, expected_files=["script.md"],
    )
    # 能提取干净正文时落盘
    assert len(refs) == 1
    content = (out_root / pid / "script.md").read_text(encoding="utf-8")
    assert "web_search" not in content
    assert content.startswith("# 标题")

    pure_think = "好的，我先 web_search，第一步搜索，第二步 file_write，还没写正文。"
    refs2 = await ProjectArtifactService.materialize_phase_output(
        "proj_test2", "agent_b", "script", {"result": pure_think}, expected_files=["script.md"],
    )
    assert refs2 == []
    assert not (out_root / "proj_test2" / "script.md").exists()


@pytest.mark.asyncio
async def test_materialize_skips_ephemeral(tmp_path, monkeypatch):
    import core.vault.project_artifact_service as pas

    out_root = tmp_path / "outputs"
    monkeypatch.setattr(pas, "OUTPUTS_ROOT", out_root)
    pid = "proj_test"
    result = {
        "needs_tool": True,
        "tool_name": "ask_peer",
        "tool_args": {"question": "where?"},
    }
    refs = await ProjectArtifactService.materialize_phase_output(
        pid, "agent_a", "char_ref_images", result, expected_files=["char-refs.md"],
    )
    assert refs == []
    assert not (out_root / pid / "char-refs.md").exists()
