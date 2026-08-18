"""工具 handler 双签名兼容测试（function calling 路径修复）。

修复前 _run_handler 恒用 handler(**tool_args)，LLM 原生 function calling 触发
4 个 (args, context) 旧签名 handler 时抛 "unexpected keyword argument"。
"""
import pytest
from core.agent.tools import ToolRegistry, ToolDefinition, ToolExecutor


def _make_executor():
    ex = ToolExecutor()
    ex.context = type("Ctx", (), {"audit": lambda *a, **k: None})()
    return ex


@pytest.mark.asyncio
async def test_args_context_handler_works_with_function_calling():
    """(args, context) 旧签名 handler 被 function calling 裸参数调用时正常。"""
    async def old_style(args, context):
        return f"ok:query={args.get('query')}"

    tool = ToolDefinition(
        name="test_old", description="d",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=old_style,
    )
    ex = _make_executor()
    result = await ex._run_handler(tool, {"query": "特斯拉"})
    assert result == "ok:query=特斯拉"


@pytest.mark.asyncio
async def test_kwargs_handler_works():
    """**kwargs 新签名 handler 正常。"""
    async def new_style(**kwargs):
        return f"ok:{kwargs.get('query')}"

    tool = ToolDefinition(
        name="test_new", description="d",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=new_style,
    )
    ex = _make_executor()
    result = await ex._run_handler(tool, {"query": "财报"})
    assert result == "ok:财报"


@pytest.mark.asyncio
async def test_real_knowledge_query_works_with_function_calling():
    """真实 _knowledge_query 被裸参数调用不再炸。"""
    tool = ToolRegistry()
    # 直接取注册的 knowledge_query 工具
    td = tool.get("knowledge_query")
    assert td is not None, "knowledge_query 未注册"
    ex = _make_executor()
    # 注入真实 context
    from core.agent.tool_execution import ToolExecutionContext
    ex.context = ToolExecutionContext(project_id="t", agent_id="a", trace_id="", emit=lambda *a, **k: None)
    result = await ex._run_handler(td, {"query": "毛利率", "top_k": 3})
    assert isinstance(result, str)
