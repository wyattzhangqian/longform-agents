"""ask_peer 子图：必须传入 RunContext，并读取 result 键"""
import pytest

from core.run.run_context import RunContext


@pytest.mark.asyncio
async def test_real_execute_subgraph_contract(monkeypatch):
    """真实 execute_subgraph：patch BaseAgent/registry，校验 run_context + result 键。"""
    ctx = RunContext(run_id="r2", project_id="p2")
    captured = {}

    class FakeDef:
        pass

    class FakeAgent:
        def __init__(self, definition=None):
            pass

        async def execute(self, input_data=None):
            captured["input"] = input_data
            return {"result": "peer-says-hi"}

    class Reg:
        async def get(self, aid):
            return FakeDef()

    import core.agent.base as base_mod
    import core.agent.registry as reg_mod

    monkeypatch.setattr(base_mod, "BaseAgent", FakeAgent)
    monkeypatch.setattr(reg_mod, "get_registry", lambda: Reg())

    result = await ctx.execute_subgraph("peer_x", "q?", timeout=5)
    assert result.success is True, getattr(result, "error", None)
    assert result.output == "peer-says-hi"
    assert isinstance(captured["input"].get("run_context"), RunContext)


def test_save_checkpoint_else_does_not_assign_logger():
    import inspect
    from core.graph import runtime as rt

    src = inspect.getsource(rt.GraphRuntime._save_checkpoint)
    assert "_logger = __import__" not in src


def test_verify_token_false_when_api_key_empty(monkeypatch):
    monkeypatch.setattr("core.auth.API_KEY", "")
    from core.auth import _verify_token

    assert _verify_token("anything") is False
