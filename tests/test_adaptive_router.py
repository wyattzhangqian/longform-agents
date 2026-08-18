"""AdaptiveRouter 单元测试

覆盖场景：
1. 正常 CONTINUE 决策
2. RETRY 决策触发重试计数递增
3. 超过 max_retries 自动降级为 CONTINUE
4. ASK_HUMAN 决策
5. SKIP 决策
6. 超过 max_total_iterations 自动降级为 CONTINUE
7. LLM 响应解析（JSON 代码块 / 纯 JSON / 无效响应降级）
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.graph.types import GraphRunConfig
from core.run.adaptive_router import (
    AdaptiveRouter,
    RouterDecision,
    RouterResult,
    QualityCheckResult,
)


def _make_config(
    mode: str = "adaptive",
    max_retries_per_node: int = 3,
    max_total_iterations: int = 20,
) -> GraphRunConfig:
    return GraphRunConfig(
        mode=mode,
        max_retries_per_node=max_retries_per_node,
        max_total_iterations=max_total_iterations,
    )


class MockLLMClient:
    """模拟本项目 LLMClient.chat(prompt) -> str 接口"""

    def __init__(self, response: str):
        self._response = response
        self.chat = AsyncMock(return_value=response)


def _make_router(llm_response: str, config: GraphRunConfig) -> AdaptiveRouter:
    llm = MockLLMClient(llm_response)
    return AdaptiveRouter(llm, config)


# ============================================================
# 1. 正常 CONTINUE
# ============================================================

@pytest.mark.asyncio
async def test_continue_decision():
    """LLM 返回 continue → RouterResult.decision == CONTINUE"""
    llm_resp = json.dumps({
        "decision": "continue",
        "reasoning": "Output quality is good",
        "feedback": None,
    })
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate(
        node_id="phase_1",
        node_name="Writer",
        node_output="Good output",
        phase_index=0,
        total_phases=3,
    )
    assert result.decision == RouterDecision.CONTINUE
    assert result.reasoning == "Output quality is good"
    assert result.feedback is None
    assert result.retry_count == 0
    assert router.total_iterations == 1


# ============================================================
# 2. RETRY 决策触发重试计数递增
# ============================================================

@pytest.mark.asyncio
async def test_retry_increments_count():
    """LLM 返回 retry → retry_count 递增"""
    llm_resp = json.dumps({
        "decision": "retry",
        "reasoning": "Output too short",
        "feedback": "Please expand the conclusion section",
    })
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate(
        node_id="phase_1",
        node_name="Writer",
        node_output="Short output",
        phase_index=0,
        total_phases=3,
    )
    assert result.decision == RouterDecision.RETRY
    assert result.feedback == "Please expand the conclusion section"
    assert result.retry_count == 1

    # 第二次 retry
    result2 = await router.evaluate(
        node_id="phase_1",
        node_name="Writer",
        node_output="Still short",
        phase_index=0,
        total_phases=3,
    )
    assert result2.decision == RouterDecision.RETRY
    assert result2.retry_count == 2


# ============================================================
# 3. 超过 max_retries 自动降级为 CONTINUE
# ============================================================

@pytest.mark.asyncio
async def test_max_retries_forces_continue():
    """retry_count >= max_retries → 强制 CONTINUE"""
    llm_resp = json.dumps({
        "decision": "retry",
        "reasoning": "Still needs work",
        "feedback": "Try again",
    })
    config = _make_config(max_retries_per_node=2)
    router = _make_router(llm_resp, config)

    # 第一次 retry → retry_count=1
    await router.evaluate("phase_1", "Writer", "out1", 0, 3)
    # 第二次 retry → retry_count=2 (达到上限)
    await router.evaluate("phase_1", "Writer", "out2", 0, 3)

    # 第三次调用 → 硬约束触发，强制 CONTINUE（不会调用 LLM）
    result = await router.evaluate("phase_1", "Writer", "out3", 0, 3)
    assert result.decision == RouterDecision.CONTINUE
    assert "Max retries" in result.reasoning
    assert result.retry_count == 2


# ============================================================
# 4. ASK_HUMAN 决策
# ============================================================

@pytest.mark.asyncio
async def test_ask_human_decision():
    """LLM 返回 ask_human → RouterResult.decision == ASK_HUMAN"""
    llm_resp = json.dumps({
        "decision": "ask_human",
        "reasoning": "Ambiguous requirement, need user clarification on target audience",
        "feedback": None,
    })
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate(
        node_id="phase_1",
        node_name="Writer",
        node_output="Ambiguous output",
        phase_index=0,
        total_phases=3,
    )
    assert result.decision == RouterDecision.ASK_HUMAN
    assert "clarification" in result.reasoning
    assert result.retry_count == 0


# ============================================================
# 5. SKIP 决策
# ============================================================

@pytest.mark.asyncio
async def test_skip_decision():
    """LLM 返回 skip → RouterResult.decision == SKIP"""
    llm_resp = json.dumps({
        "decision": "skip",
        "reasoning": "Optional node, output unrecoverable",
        "feedback": None,
    })
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate(
        node_id="phase_optional",
        node_name="Optional Enhancer",
        node_output="Bad output",
        phase_index=2,
        total_phases=3,
    )
    assert result.decision == RouterDecision.SKIP
    assert result.retry_count == 0


# ============================================================
# 6. 超过 max_total_iterations 自动降级为 CONTINUE
# ============================================================

@pytest.mark.asyncio
async def test_max_total_iterations_forces_continue():
    """total_iterations >= max_total_iterations → 强制 CONTINUE"""
    llm_resp = json.dumps({
        "decision": "retry",
        "reasoning": "Needs work",
        "feedback": "Fix it",
    })
    config = _make_config(max_retries_per_node=100, max_total_iterations=3)
    router = _make_router(llm_resp, config)

    # 消耗 3 次迭代（达到上限）
    await router.evaluate("phase_1", "Writer", "out1", 0, 3)
    await router.evaluate("phase_2", "Editor", "out2", 1, 3)
    await router.evaluate("phase_3", "Reviewer", "out3", 2, 3)

    # 第 4 次调用 → 硬约束触发，强制 CONTINUE
    result = await router.evaluate("phase_1", "Writer", "out4", 0, 3)
    assert result.decision == RouterDecision.CONTINUE
    assert "Max total iterations" in result.reasoning


# ============================================================
# 7. LLM 响应解析（JSON 代码块 / 纯 JSON / 无效响应降级）
# ============================================================

@pytest.mark.asyncio
async def test_parse_json_code_block():
    """LLM 返回 ```json 代码块 → 正确解析"""
    llm_resp = '```json\n{"decision": "continue", "reasoning": "OK"}\n```'
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate("n1", "Agent", "out", 0, 1)
    assert result.decision == RouterDecision.CONTINUE
    assert result.reasoning == "OK"


@pytest.mark.asyncio
async def test_parse_pure_json():
    """LLM 返回纯 JSON → 正确解析"""
    llm_resp = '{"decision": "skip", "reasoning": "Done"}'
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate("n1", "Agent", "out", 0, 1)
    assert result.decision == RouterDecision.SKIP


@pytest.mark.asyncio
async def test_parse_invalid_response_degrades_to_continue():
    """LLM 返回无效文本 → 降级为 CONTINUE"""
    llm_resp = "I think the output is good, let's move on."
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate("n1", "Agent", "out", 0, 1)
    assert result.decision == RouterDecision.CONTINUE
    assert "Failed to parse" in result.reasoning


@pytest.mark.asyncio
async def test_parse_unknown_decision_degrades_to_continue():
    """LLM 返回未知 decision 值 → 降级为 CONTINUE"""
    llm_resp = json.dumps({"decision": "escalate", "reasoning": "unknown"})
    router = _make_router(llm_resp, _make_config())
    result = await router.evaluate("n1", "Agent", "out", 0, 1)
    assert result.decision == RouterDecision.CONTINUE


# ============================================================
# 8. quality_result 传入测试
# ============================================================

@pytest.mark.asyncio
async def test_quality_result_passed_to_prompt():
    """quality_result 正确传入 LLM prompt"""
    llm_resp = json.dumps({"decision": "continue", "reasoning": "OK"})
    llm = MockLLMClient(llm_resp)
    config = _make_config()
    router = AdaptiveRouter(llm, config)

    quality = QualityCheckResult(
        score=0.3,
        violations=[{"severity": "error", "message": "Too short"}],
        passed=False,
    )
    result = await router.evaluate(
        node_id="phase_1",
        node_name="Writer",
        node_output="Short",
        phase_index=0,
        total_phases=2,
        quality_result=quality,
    )
    # 验证 LLM 被调用
    assert llm.chat.called
    # 验证 prompt 中包含质量信息
    call_args = llm.chat.call_args
    prompt = call_args[0][0] if call_args[0] else call_args[1].get("prompt", "")
    assert "0.3" in prompt
    assert "Too short" in prompt
    assert result.decision == RouterDecision.CONTINUE


# ============================================================
# 9. reset 方法
# ============================================================

@pytest.mark.asyncio
async def test_reset_clears_state():
    """reset() 清空重试计数和迭代计数"""
    llm_resp = json.dumps({"decision": "retry", "reasoning": "retry", "feedback": "fix"})
    router = _make_router(llm_resp, _make_config())
    await router.evaluate("n1", "A", "out", 0, 1)
    assert router.total_iterations == 1
    assert router._retry_counts.get("n1") == 1

    router.reset()
    assert router.total_iterations == 0
    assert len(router._retry_counts) == 0
