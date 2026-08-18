"""Phase 3 测试 — Agent 自评"""
import asyncio
import json
import pytest
from core.agent.self_review import self_review


class MockLLMClient:
    def __init__(self, response):
        self._response = response

    async def chat(self, prompt, system_prompt="", **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_self_review_pass():
    """质量达标时应返回 pass=True"""
    mock = MockLLMClient(json.dumps({"pass": True, "score": 0.9, "issues": []}))
    passed, score, issues = await self_review(
        llm_client=mock,
        output_text="一段高质量的创作内容...",
        unit_brief="写一个紧张的追逐场景",
        threshold=0.7,
    )
    assert passed is True
    assert score == 0.9


@pytest.mark.asyncio
async def test_self_review_retry_once():
    """不通过时应返回 pass=False"""
    mock = MockLLMClient(json.dumps({"pass": False, "score": 0.3, "issues": ["缺少追逐紧张感"]}))
    passed, score, issues = await self_review(
        llm_client=mock,
        output_text="他跑了起来...",
        unit_brief="写一个紧张的追逐场景",
        threshold=0.7,
    )
    assert passed is False
    assert score == 0.3
    assert "缺少追逐紧张感" in issues


@pytest.mark.asyncio
async def test_self_review_fallback_on_llm_failure():
    """LLM 失败时默认 pass"""
    class FailingLLM:
        async def chat(self, *a, **kw):
            raise Exception("LLM unavailable")

    passed, score, issues = await self_review(
        llm_client=FailingLLM(),
        output_text="内容",
        unit_brief="brief",
        threshold=0.7,
    )
    assert passed is True  # 失败时默认 pass


@pytest.mark.asyncio
async def test_self_review_no_brief_passes():
    """无 unit_brief 时直接 pass"""
    passed, score, issues = await self_review(
        llm_client=None,
        output_text="内容",
        unit_brief="",
    )
    assert passed is True


@pytest.mark.asyncio
async def test_self_review_markdown_code_block():
    """LLM 返回 markdown code block 时能正确解析"""
    mock = MockLLMClient('```json\n{"pass": true, "score": 0.85, "issues": []}\n```')
    passed, score, issues = await self_review(
        llm_client=mock,
        output_text="内容",
        unit_brief="brief",
    )
    assert passed is True
    assert score == 0.85
