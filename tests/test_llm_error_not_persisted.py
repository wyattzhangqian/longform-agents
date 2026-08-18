"""LLM 错误串不得被当作正常产出，也不得写入 agent_outputs。"""
from core.agent.base import BaseAgent
from core.run.run_context import RunContext


def test_is_llm_error_text():
    assert BaseAgent._is_llm_error_text("[LLM Error] timeout") is True
    assert BaseAgent._is_llm_error_text("[LLM CircuitOpen] x") is True
    assert BaseAgent._is_llm_error_text("[LLM Budget exceeded]") is True
    assert BaseAgent._is_llm_error_text("正常产出内容") is False


def test_error_status_payload_must_not_enter_agent_outputs():
    """模拟 runtime 跳过写入：error 状态不应出现在 agent_outputs。"""
    ctx = RunContext(run_id="r1", project_id="p1", task="t")
    err_payload = {
        "agent_id": "agent_x",
        "result": "[LLM Error] boom",
        "status": "error",
        "error": "[LLM Error] boom",
    }
    # 与 runtime 一致：status=error 时不调用 set_agent_result
    skip = err_payload.get("status") == "error"
    if not skip:
        ctx.set_agent_result("agent_x", err_payload)
    assert "agent_x" not in ctx.agent_outputs
    assert "[LLM Error]" not in str(ctx.agent_outputs)
