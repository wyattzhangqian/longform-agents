"""Autofix 失败语义（2026-08-10 P0-2）— 超时/异常/max_retries 必须 passed=False。

生产级原则：Autofix 失败不能伪装成通过（passed=True）。
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.run.run_context import RunContext


def _ctx() -> RunContext:
    return RunContext(run_id="r1", project_id="p1", task="t")


def _agent(output: str):
    a = MagicMock()
    a.execute = AsyncMock(return_value=output)
    return a


def _always_fail_gateway(violations):
    """mock QualityGateway：check 恒返回 violations（不过）"""
    g = MagicMock()
    g.check.return_value = {"violations": violations}
    return g


@pytest.mark.asyncio
async def test_autofix_timeout_is_failure():
    """Autofix 超时 → passed=False + failure_code=autofix_timeout + CRITICAL 降级。"""
    import core.gateway.coach as coach_mod
    from core.gateway.coach import quality_gate_with_autofix

    ctx = _ctx()
    a = MagicMock()

    async def _hang(*args, **kwargs):
        await asyncio.Event().wait()

    a.execute = _hang
    with patch("core.gateway.QualityGateway",
               return_value=_always_fail_gateway([{"severity": "error", "message": "x"}])), \
         patch.object(coach_mod, "_AUTOFIX_AGENT_TIMEOUT", 0.1):
        res = await quality_gate_with_autofix(a, "正文", {}, ctx, "p1")

    assert res.passed is False
    assert res.status == "timeout"
    assert res.failure_code == "autofix_timeout"
    assert ctx.diagnostics is not None
    assert ctx.diagnostics["has_critical"] is True
    assert any(e["component"] == "quality_autofix" for e in ctx.diagnostics["entries"])


@pytest.mark.asyncio
async def test_autofix_exception_is_failure():
    """Autofix 异常 → passed=False + failure_code=autofix_error。"""
    from core.gateway.coach import quality_gate_with_autofix

    ctx = _ctx()
    a = MagicMock()

    async def _boom(*args, **kwargs):
        raise RuntimeError("LLM 挂了")

    a.execute = _boom
    with patch("core.gateway.QualityGateway",
               return_value=_always_fail_gateway([{"severity": "error", "message": "x"}])):
        res = await quality_gate_with_autofix(a, "正文", {}, ctx, "p1")

    assert res.passed is False
    assert res.status == "failed"
    assert res.failure_code == "autofix_error"
    assert ctx.diagnostics["has_critical"] is True


@pytest.mark.asyncio
async def test_autofix_max_retries_is_failure():
    """Autofix max_retries 仍不通过 → passed=False + failure_code=max_retries_exceeded。"""
    from core.gateway.coach import quality_gate_with_autofix

    ctx = _ctx()
    a = _agent("修复后正文")
    with patch("core.gateway.QualityGateway",
               return_value=_always_fail_gateway([{"severity": "error", "message": "仍违规"}])):
        res = await quality_gate_with_autofix(a, "正文", {}, ctx, "p1")

    assert res.passed is False
    assert res.status == "failed"
    assert res.failure_code == "max_retries_exceeded"
    assert ctx.diagnostics["has_critical"] is True
