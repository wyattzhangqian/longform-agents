"""tool_use / handoff 解析与交接摘要"""

from core.communication.content_format import iter_tool_intents_from_text
from core.communication.handoff import build_handoff_payload


def test_iter_tool_intents_tool_use_array():
    text = (
        "先读上游。\n\n```json\n"
        '{"tool_use": [{"tool_name": "file_read", "tool_args": {"path": "script.md"}}]}\n'
        "```"
    )
    intents = list(iter_tool_intents_from_text(text))
    assert len(intents) == 1
    assert intents[0]["needs_tool"] is True
    assert intents[0]["tool_name"] == "file_read"
    assert intents[0]["tool_args"]["path"] == "script.md"


def test_iter_tool_intents_openai_tool_calls():
    text = (
        "```json\n"
        '{"tool_calls": [{"type": "function", "function": {"name": "file_read", '
        '"arguments": "{\\"path\\": \\"style-bible.md\\"}"}}]}\n'
        "```"
    )
    intents = list(iter_tool_intents_from_text(text))
    assert len(intents) == 1
    assert intents[0]["tool_name"] == "file_read"
    assert intents[0]["tool_args"]["path"] == "style-bible.md"


def test_handoff_summary_uses_artifact_keys_not_thinking():
    thinking = (
        "好的，角色设定师已就位！我将读取 script.md 并 file_write characters.json。"
    )
    payload = build_handoff_payload(
        "fixture_agent_comic_character",
        "fixture_agent_comic_char_ref",
        {"result": thinking},
        from_name="角色配置",
        phase="characters",
        artifact_keys=["characters.json"],
    )
    assert "characters.json" in payload.summary
    assert "角色设定师" not in payload.summary
    assert "角色设定师" not in payload.to_context_text()
    assert payload.artifact_keys == ["characters.json"]


def test_handoff_filters_result_as_artifact_key():
    payload = build_handoff_payload(
        "a", "b", {"result": "x"}, artifact_keys=["result", "characters.json"],
    )
    assert payload.artifact_keys == ["characters.json"]
