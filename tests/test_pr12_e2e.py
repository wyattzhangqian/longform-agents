"""PR-12 端到端接线 smoke 测试 — Planner 主链路走通（validate → critique → refine）。

验证 5 个缺口接上后，plan_agent_order 全链路能走通不崩：
- decompose → validate → critique → refine → match → 返回
- critic/refine 被调用（接线正确）
"""
import pytest
from unittest.mock import patch, AsyncMock

import core.run  # noqa: F401 — 预热模块依赖，打破 planner 冷导入的循环引用（见 CLAUDE.md Known Issues）

from core.orchestration.decomposer import DecompositionPlan, SubTask


def _fake_plan():
    return DecompositionPlan(
        plan_id="p", original_task="写一份报告",
        sub_tasks=[
            SubTask(id="sub_a", title="资料调研", dependencies=[], assigned_agent="builtin_researcher"),
            SubTask(id="sub_b", title="撰写报告", dependencies=["sub_a"], assigned_agent="builtin_writer"),
        ],
        execution_order=[["sub_a"], ["sub_b"]],
    )


@pytest.mark.asyncio
async def test_plan_agent_order_runs_critic_and_refine():
    """plan_agent_order 接入后走 validate → critique → refine（接线 smoke）。"""
    from core.orchestration import planner
    from core.orchestration.plan_critic import PlanCritique, CritiqueIssue

    critique = PlanCritique(issues=[CritiqueIssue(phase_id="sub_b", kind="missing_step", message="缺审核")])

    with patch.object(planner.TaskDecomposer, "decompose", new=AsyncMock(return_value=_fake_plan())):
        with patch("core.orchestration.planner.ensure_generic_agents", new=AsyncMock(return_value=[])):
            with patch("core.orchestration.planner._gather_capabilities", new=AsyncMock(return_value=[])):
                with patch("core.orchestration.planner._generate_project_name", new=AsyncMock(return_value="报告")):
                    with patch("core.orchestration.plan_critic.critique_plan", new=AsyncMock(return_value=critique)) as mock_critic:
                        with patch("core.orchestration.plan_refiner.refine_plan", new=AsyncMock(return_value=None)) as mock_refine:
                            with patch("core.orchestration.template_matcher.get_template_matcher") as mock_get_matcher:
                                mock_matcher = AsyncMock()
                                mock_matcher.match = AsyncMock(return_value=[])
                                mock_get_matcher.return_value = mock_matcher

                                result = await planner.plan_agent_order(
                                    task="写一份报告", use_llm=True, domain_id="platform:web_novel",
                                )

    # 6 元组
    assert len(result) == 6
    plan, composition, agent_order, match_details, reasoning, execution_layers = result
    assert plan is not None
    # critic 被调用（接线正确）
    mock_critic.assert_called_once()
    # refine 被调用（因为 critic 有 issue）
    mock_refine.assert_called_once()


@pytest.mark.asyncio
async def test_plan_agent_order_no_critic_without_llm():
    """use_llm=False 时不走 critic/refine（不误调用）。"""
    from core.orchestration import planner

    with patch.object(planner.TaskDecomposer, "decompose", new=AsyncMock(return_value=_fake_plan())):
        with patch("core.orchestration.planner.ensure_generic_agents", new=AsyncMock(return_value=[])):
            with patch("core.orchestration.planner._gather_capabilities", new=AsyncMock(return_value=[])):
                with patch("core.orchestration.planner._generate_project_name", new=AsyncMock(return_value="报告")):
                    with patch("core.orchestration.plan_critic.critique_plan", new=AsyncMock()) as mock_critic:
                        with patch("core.orchestration.template_matcher.get_template_matcher") as mock_get_matcher:
                            mock_matcher = AsyncMock()
                            mock_matcher.match = AsyncMock(return_value=[])
                            mock_get_matcher.return_value = mock_matcher

                            await planner.plan_agent_order(
                                task="写一份报告", use_llm=False, domain_id="platform:web_novel",
                            )

    mock_critic.assert_not_called()
