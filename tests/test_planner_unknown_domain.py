"""P0-1 修复测试：未知领域不再静默路由到 web_novel，走通用 Agent 能力组建。

回归保护：
- 未知/空领域 → 跳过内容模板匹配器，走 _compose_generic_team
- 已知内容领域 → 仍走模板匹配（不破坏现有行为）
- platform: 前缀的未知领域同样走通用组建
"""
import pytest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import core.run  # noqa: F401 — 预热模块依赖，打破 planner 冷导入的循环引用（见 CLAUDE.md Known Issues）

from core.orchestration.planner import (
    plan_agent_order,
    _PLATFORM_CONTENT_DOMAINS,
    _UnknownDomainRouting,
)


def _fake_agents():
    """混合通用 Agent 与内容模板 Agent 的假 agents 池。"""
    return [
        SimpleNamespace(id="builtin_researcher"),
        SimpleNamespace(id="builtin_writer"),
        SimpleNamespace(id="builtin_reviewer"),
        SimpleNamespace(id="builtin_general"),
        SimpleNamespace(id="agent_tpl_web_novel_world_builder"),
        SimpleNamespace(id="agent_tpl_web_novel_chapter_writer"),
    ]


def _fake_plan():
    return SimpleNamespace(
        plan_id="plan_p01_test",
        reasoning="",
        sub_tasks=[
            SimpleNamespace(
                id="st_1",
                title="收集资料",
                description="收集公开资料",
                required_capabilities=["research", "analysis"],
                assigned_agent=None,
            ),
            SimpleNamespace(
                id="st_2",
                title="撰写报告",
                description="根据资料撰写报告",
                required_capabilities=["writing"],
                assigned_agent=None,
            ),
        ],
        execution_order=[["st_1"], ["st_2"]],
    )


@contextmanager
def _patched_planner_deps():
    """统一进入 planner 的 DB/LLM 重依赖 mock，产出已进入的 mock 对象列表。

    被 mock：ensure_generic_agents / _filter_agents_by_domain /
    _gather_capabilities / TaskDecomposer / LLMClient。
    """
    with ExitStack() as stack:
        yield [
            stack.enter_context(p)
            for p in (
                patch(
                    "core.orchestration.planner.ensure_generic_agents",
                    new=AsyncMock(return_value=_fake_agents()),
                ),
                patch(
                    "core.orchestration.planner._filter_agents_by_domain",
                    new=AsyncMock(side_effect=lambda agents, domain_id: agents),
                ),
                patch(
                    "core.orchestration.planner._gather_capabilities",
                    new=AsyncMock(return_value=["research", "analysis", "writing"]),
                ),
                patch("core.orchestration.planner.TaskDecomposer"),
            )
        ]


@pytest.mark.asyncio
async def test_unknown_domain_skips_template_matcher_and_uses_generic():
    """未知领域：不得调用内容模板匹配器，走 _compose_generic_team，结果不含 web_novel 模板 Agent。"""
    fake_plan = _fake_plan()
    with patch("core.orchestration.template_matcher.get_template_matcher") as m_matcher, \
         patch(
             "core.orchestration.planner._compose_generic_team",
             new=AsyncMock(return_value=SimpleNamespace(assignments=[])),
         ) as m_compose:
        with _patched_planner_deps() as patches:
            m_decomp = patches[3]
            m_decomp.return_value.decompose = AsyncMock(return_value=fake_plan)
            _, _, agent_order, _, _, _ = await plan_agent_order(
                "分析这家公司的财务报告", use_llm=False, domain_id=""
            )

    # 路由断言：不走模板匹配器，走通用组建
    m_matcher.assert_not_called()
    m_compose.assert_awaited_once()

    # 结果断言：不出现 web_novel 内容模板 Agent
    assert agent_order
    assert not any("web_novel" in aid for aid in agent_order)


@pytest.mark.asyncio
async def test_unknown_platform_domain_also_routes_generic():
    """platform: 前缀的未知领域同样走通用组建（如 platform:legal_review）。"""
    fake_plan = _fake_plan()
    with patch("core.orchestration.template_matcher.get_template_matcher") as m_matcher, \
         patch(
             "core.orchestration.planner._compose_generic_team",
             new=AsyncMock(return_value=SimpleNamespace(assignments=[])),
         ) as m_compose:
        with _patched_planner_deps() as patches:
            m_decomp = patches[3]
            m_decomp.return_value.decompose = AsyncMock(return_value=fake_plan)
            _, _, agent_order, _, _, _ = await plan_agent_order(
                "法务合同审查", use_llm=False, domain_id="platform:legal_review"
            )

    m_matcher.assert_not_called()
    m_compose.assert_awaited_once()
    assert agent_order
    assert not any("web_novel" in aid for aid in agent_order)


@pytest.mark.asyncio
async def test_known_domain_still_uses_template_matcher():
    """已知内容领域（web_novel）：仍走模板匹配器，不触发通用组建（无回归）。"""
    fake_plan = _fake_plan()
    fake_matcher = AsyncMock()
    fake_matcher.match = AsyncMock(return_value=[])
    with patch(
        "core.orchestration.template_matcher.get_template_matcher",
        return_value=fake_matcher,
    ) as m_matcher, \
         patch(
             "core.orchestration.planner._compose_generic_team",
             new=AsyncMock(return_value=SimpleNamespace(assignments=[])),
         ) as m_compose:
        with _patched_planner_deps() as patches:
            m_decomp = patches[3]
            m_decomp.return_value.decompose = AsyncMock(return_value=fake_plan)
            await plan_agent_order(
                "写一部修仙小说第一章", use_llm=False, domain_id="platform:web_novel"
            )

    m_matcher.assert_called_once()
    fake_matcher.match.assert_awaited_once()
    m_compose.assert_not_called()


def test_platform_content_domains_covers_five_domains():
    """常量覆盖 5 个内容领域；未知领域不在其中。"""
    assert set(_PLATFORM_CONTENT_DOMAINS) == {
        "web_novel", "comic_static", "comic_drama", "music_production", "research_report",
    }
    assert "legal_review" not in _PLATFORM_CONTENT_DOMAINS
    # 异常类存在（路由信号）
    assert issubclass(_UnknownDomainRouting, Exception)


@pytest.mark.asyncio
async def test_all_subtasks_get_real_agent():
    """P1-10补：plan_agent_order 后所有子任务都有真实 agent（非 sub_* 子任务 id）。

    修复前：LLM 偶发弱输出时 matcher 留空 assigned_agent，PhaseSpec.from_plan
    fallback 到 task.id（sub_*）→ set_agents 插 project_agents 触发 FK 崩溃。
    """
    fake_plan = _fake_plan()
    fake_matcher = AsyncMock()
    fake_matcher.match = AsyncMock(return_value=[SimpleNamespace(
        subtask_title="收集资料", matched_template_id=None, matched_template_name=None,
        score=10.0, level="none", fallback_type="skipped", suggestion=None,
    )])

    with patch("core.orchestration.template_matcher.get_template_matcher",
               return_value=fake_matcher), \
         patch("core.orchestration.planner._compose_generic_team",
               new=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(assignments=[]))):
        with _patched_planner_deps() as patches:
            m_decomp = patches[3]
            m_decomp.return_value.decompose = AsyncMock(return_value=fake_plan)
            _, _, agent_order, _, _, _ = await plan_agent_order(
                "分析财报", use_llm=False, domain_id=""
            )

    # 所有子任务都有真实 agent，且不是 sub_* 子任务 id
    assert agent_order
    for sub in fake_plan.sub_tasks:
        assert getattr(sub, "assigned_agent", ""), f"子任务 {sub.id} 未分配 agent"
        assert not sub.assigned_agent.startswith("sub_"), f"子任务 {sub.id} 分到子任务id: {sub.assigned_agent}"
