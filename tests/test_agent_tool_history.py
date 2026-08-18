"""Agent function-calling 工具消息历史回归测试（P1-8 修复）。

修复前：工具结果以 user 文本块塞回，LLM 收到非 OpenAI 规范格式（缺 role:"tool"+
tool_call_id），输出"我将继续"叙事而非合成交付。
修复后：维护 tool_history，按规范回灌 assistant.tool_calls + role:"tool" 结果，
LLM 收到正确格式 → 合成最终交付。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import core.run  # noqa: F401 — 预热模块依赖
from core.run.run_context import RunContext


@pytest.fixture
def fc_agent_definition():
    defn = MagicMock()
    defn.id = "test_agent"
    defn.name = "测试员"
    defn.display_name = "测试员"
    defn.system_prompt = "你是测试员"
    defn.model_profile = {"model": "deepseek-chat"}
    defn.tool_ids = ["web_search"]
    defn.skill_ids = []
    defn.memory_config = None
    defn.extra = {}
    return defn


@pytest.mark.asyncio
async def test_function_calling_feeds_tool_results_as_role_tool(fc_agent_definition):
    """function calling 工具轮后，下一轮 LLM 收到 role:'tool' 回灌，并合成最终交付。"""
    from core.agent.base import BaseAgent

    agent = BaseAgent(definition=fc_agent_definition)
    agent.config = {"use_tools": True, "max_tool_iterations": 3}
    agent.bus = None
    agent.output_dir = "/tmp"

    run_ctx = RunContext(project_id="test_proj", run_id="run_1", task="测试任务")

    captured_messages = []

    async def fake_llm(messages, tools=None, tool_calls_sink=None):
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            # 第一轮：返回结构化工具调用 web_search
            if tool_calls_sink is not None:
                tool_calls_sink.append(
                    {"id": "call_1", "name": "web_search", "args": {"query": "特斯拉"}}
                )
            return ""
        # 第二轮：基于工具结果合成最终交付
        return "## 财务分析\n特斯拉毛利率 25.6%，经营现金流改善。"

    mock_registry = MagicMock()
    mock_registry.get.return_value = MagicMock(
        name="web_search",
        description="网络搜索",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    mock_registry.get_instance.return_value = mock_registry

    with patch("core.agent.base.ContextBuilder.build",
               return_value=[{"role": "user", "content": "基础上下文"}]), \
         patch.object(agent, "_llm_call_messages", side_effect=fake_llm), \
         patch("core.agent.base.ToolRegistry", mock_registry), \
         patch("core.agent.base.ToolExecutor") as m_executor_cls, \
         patch.object(agent, "_resolve_capabilities",
                      new=AsyncMock(return_value={"tools": ["web_search"], "namespaces": [], "prompt_snippets": []})), \
         patch.object(agent, "_load_knowledge_context", new=AsyncMock(return_value="")), \
         patch("core.agent.base.MemoryManager") as m_memory:
        m_memory.recall = AsyncMock(return_value=[])
        m_memory.format_for_context = lambda *a, **k: ""
        m_memory.store_execution_summary = AsyncMock()

        m_executor = m_executor_cls.return_value
        m_executor.execute = AsyncMock(return_value="[搜索结果] 特斯拉 Q3 毛利率 25.6%")

        result = await agent.execute({"run_context": run_ctx, "project_id": "test_proj"})

    # 断言：LLM 被调用了 2 次（工具轮 + 合成轮）—— 修复前只调 1 次就断
    assert len(captured_messages) == 2, f"应 2 轮 LLM 调用，实际 {len(captured_messages)}"

    # 第二轮消息含 role:"tool" 回灌
    second_msgs = captured_messages[1]
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert tool_msgs, "第二轮应包含 role:'tool' 回灌消息"
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "搜索结果" in tool_msgs[0]["content"]

    # 结果应为合成交付（非"我将继续"叙事）
    assert "财务分析" in result.get("result", "")


@pytest.mark.asyncio
async def test_final_round_forces_synthesis(fc_agent_definition):
    """最后一轮注入'必须合成'指令，防止 agent 一直研究撞上限被截断为空壳。"""
    from core.agent.base import BaseAgent

    agent = BaseAgent(definition=fc_agent_definition)
    agent.config = {"use_tools": True, "max_tool_iterations": 1}  # 仅 1 轮
    agent.bus = None
    agent.output_dir = "/tmp"

    run_ctx = RunContext(project_id="test_proj", run_id="run_1", task="测试")

    captured_messages = []

    async def fake_llm(messages, tools=None, tool_calls_sink=None):
        captured_messages.append(messages)
        return "我将继续研究..."  # 若不强制，这就是最终结果

    with patch("core.agent.base.ContextBuilder.build") as m_build, \
         patch.object(agent, "_llm_call_messages", side_effect=fake_llm), \
         patch.object(agent, "_resolve_capabilities",
                      new=AsyncMock(return_value={"tools": ["web_search"], "namespaces": [], "prompt_snippets": []})), \
         patch.object(agent, "_load_knowledge_context", new=AsyncMock(return_value="")), \
         patch("core.agent.base.MemoryManager") as m_memory:
        m_memory.recall = AsyncMock(return_value=[])
        m_memory.format_for_context = lambda *a, **k: ""
        m_memory.store_execution_summary = AsyncMock()
        # build 返回含 extra_instructions 的消息
        def fake_build(self, run_ctx, tool_results=None, extra_instructions="", **kw):
            return [{"role": "user", "content": f"base\n{extra_instructions}"}]
        m_build.side_effect = fake_build

        result = await agent.execute({"run_context": run_ctx, "project_id": "test_proj"})

    # 最后一轮（max_iters=1）应注入"最后一轮必须合成"指令
    first = captured_messages[0][0]["content"]
    assert "最后一轮" in first, f"应注入最后轮指令: {first[:100]}"
    assert "产出完整、干净的最终交付" in first
