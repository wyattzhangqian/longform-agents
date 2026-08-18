"""预算化节点超时回归测试（2026-08-08）

resolve_agent_timeout 从工具循环预算推导节点超时：
    base + max_iters×(LLM_TIMEOUT + 工具预算) + 终稿预算，硬上限封顶。

背景：旧实现是固定 AGENT_TIMEOUT_SECONDS（300/600），与工具循环预算
（max_tool_iterations 轮 × 每轮 LLM+工具）不对齐——8 轮正常执行上界
（含偶发 50s+ LLM 调用）就可能超过固定值，保护机制误杀正常执行。
预算化让"正常+偶发慢"永不误杀，只有真卡死才被掐。

覆盖：默认值、轮数影响、显式节点覆盖优先、硬上限、环境变量可配。
"""

import types

import pytest

import core.run  # noqa: F401 — 先初始化 run 包，规避 core.agent.base 直接导入时的循环依赖


def _node(metadata=None):
    return types.SimpleNamespace(metadata=metadata or {})


def test_default_budget_8_rounds():
    """默认 8 轮：300 + 8×(120+60) + 180 = 1920s（正常 run ~5min，永不误杀）"""
    from core.graph.runtime import resolve_agent_timeout

    assert resolve_agent_timeout({}, _node()) == 1920.0


def test_rounds_scale_budget():
    """轮数越多预算越大（少轮次 Agent 不背长预算）"""
    from core.graph.runtime import resolve_agent_timeout

    budget_3 = resolve_agent_timeout({"max_tool_iterations": 3}, _node())
    budget_8 = resolve_agent_timeout({"max_tool_iterations": 8}, _node())
    assert budget_3 == 300 + 3 * (120 + 60) + 180
    assert budget_3 < budget_8


def test_node_metadata_override_priority():
    """node.metadata.timeout_seconds 显式配置最优先，不受预算影响"""
    from core.graph.runtime import resolve_agent_timeout

    assert resolve_agent_timeout({}, _node({"timeout_seconds": 45})) == 45.0
    # 即使轮数很多，显式配置仍生效
    assert resolve_agent_timeout({"max_tool_iterations": 50}, _node({"timeout_seconds": 10})) == 10.0


def test_hard_cap_prevents_runaway():
    """极端轮数 → 预算被硬上限 AGENT_TIMEOUT_CAP_SECONDS(3600) 封顶"""
    from core.graph.runtime import resolve_agent_timeout

    assert resolve_agent_timeout({"max_tool_iterations": 100}, _node()) == 3600.0


def test_env_tunable(monkeypatch):
    """环境变量可调：LLM/工具/终稿/保底 都参与预算"""
    from core.graph.runtime import resolve_agent_timeout

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("PER_ROUND_TOOL_BUDGET_SECONDS", "20")
    monkeypatch.setenv("FINAL_GEN_BUDGET_SECONDS", "60")
    monkeypatch.setenv("AGENT_TIMEOUT_SECONDS", "100")
    # 100 + 8×(30+20) + 60 = 560
    assert resolve_agent_timeout({}, _node()) == 560.0


def test_zero_metadata_no_crash():
    """空 metadata / 无 max_tool_iterations 不崩溃（用默认轮数）"""
    from core.graph.runtime import resolve_agent_timeout

    assert resolve_agent_timeout({}, types.SimpleNamespace(metadata={})) == 1920.0
    assert resolve_agent_timeout(None, types.SimpleNamespace(metadata={})) == 1920.0
