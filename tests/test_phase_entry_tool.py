"""phase_entry 确定性工具触发：execute_named 契约"""
from pathlib import Path

import pytest

from core.agent.tools import ToolExecutor, ToolRegistry


@pytest.mark.asyncio
async def test_execute_named_invokes_file_read(tmp_path, monkeypatch):
    monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", Path(tmp_path).resolve())
    (Path(tmp_path) / "a.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(output_dir="")
    out = await ex.execute_named("file_read", {"path": "a.txt", "max_chars": 100})
    assert out is not None
    assert "hello" in out


def test_validate_path_rejects_outputs_prefix_sibling(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    evil = tmp_path / "outputs_evil"
    out.mkdir()
    evil.mkdir()
    (evil / "x.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", out.resolve())
    with pytest.raises(ValueError, match="越权"):
        ToolRegistry._validate_path(str(evil / "x.txt"))
