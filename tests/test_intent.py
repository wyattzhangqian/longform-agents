"""PR-6 意图理解器测试 — core/orchestration/intent.py。

intent 输出 goal/task_type/constraints/open_questions，只在「答案会改变计划结构」时
触发澄清（needs_clarification）。
"""
import json
import pytest

from core.orchestration.intent import (
    IntentResult,
    ClarifyQuestion,
    build_intent_prompt,
    parse_intent_response,
    analyze_intent,
)


def test_build_intent_prompt_contains_task():
    prompt = build_intent_prompt("写一份竞品分析报告")
    assert "写一份竞品分析报告" in prompt
    assert "open_questions" in prompt
    assert "task_type" in prompt


def test_parse_intent_response_full():
    resp = json.dumps({
        "goal": "写竞品分析报告",
        "task_type": "analysis",
        "hard_constraints": ["不超预算"],
        "soft_preferences": ["优先中文"],
        "assumptions": ["有数据源"],
        "open_questions": [
            {"id": "output_format", "question": "输出格式？", "options": ["Markdown", "PPT"]},
        ],
        "deliverables": [{"id": "d1", "type": "report", "format": "markdown"}],
        "confidence": 0.86,
    })
    intent = parse_intent_response(resp)
    assert intent.goal == "写竞品分析报告"
    assert intent.task_type == "analysis"
    assert intent.hard_constraints == ["不超预算"]
    assert len(intent.open_questions) == 1
    assert intent.open_questions[0].id == "output_format"
    assert intent.open_questions[0].options == ["Markdown", "PPT"]
    assert intent.deliverables[0].type == "report"
    assert intent.confidence == pytest.approx(0.86)
    assert intent.needs_clarification is True


def test_parse_intent_response_no_questions():
    resp = json.dumps({"goal": "写文章", "task_type": "creative", "open_questions": []})
    intent = parse_intent_response(resp)
    assert intent.needs_clarification is False


def test_parse_intent_response_unknown_task_type_coerced():
    resp = json.dumps({"goal": "x", "task_type": "bogus"})
    intent = parse_intent_response(resp)
    assert intent.task_type == "general"


def test_parse_intent_response_invalid_json_returns_empty():
    intent = parse_intent_response("not json")
    assert intent.goal == ""
    assert intent.needs_clarification is False


@pytest.mark.asyncio
async def test_analyze_intent_failure_returns_empty():
    class Boom:
        async def chat(self, *a, **k):
            raise RuntimeError("boom")

    intent = await analyze_intent("任务", Boom())
    assert intent.goal == ""
    assert intent.needs_clarification is False


@pytest.mark.asyncio
async def test_analyze_intent_returns_result():
    class FakeLLM:
        async def chat(self, *a, **k):
            return json.dumps({"goal": "g", "task_type": "research", "open_questions": [
                {"id": "q1", "question": "范围？", "options": ["A", "B"]},
            ]})

    intent = await analyze_intent("调研", FakeLLM())
    assert intent.goal == "g"
    assert intent.needs_clarification is True
