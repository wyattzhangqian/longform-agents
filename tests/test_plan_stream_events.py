"""plan_agent_order_stream SSE 事件核验 — 前端断链回归测试（2026-08-16）。

背景：后端 PR-4/PR-8/PR-10 落地后，前端曾静默丢弃 domain_prior/critic/refine 事件，
且 risk_level 字段在 SSE plan 事件 → 前端保存链路中断（确认门形同虚设）。

本测试锁定 3 个契约：
- domain_prior 事件必须产出（PR-4 领域先验）
- critic / refine 事件必须产出（PR-10 语义审查 + 自动修订）
- plan 事件 pipeline 元素必须含 risk_level（P0 确认门的数据源，前端保存不丢）
"""
import json
import pytest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

import core.run  # noqa: F401 — 预热模块依赖，打破 planner 冷导入的循环引用

from core.orchestration.decomposer import DecompositionPlan, SubTask
from core.orchestration.plan_critic import PlanCritique, CritiqueIssue


def _fake_plan():
    return DecompositionPlan(
        plan_id="p",
        original_task="写一份报告",
        sub_tasks=[
            SubTask(id="sub_a", title="资料调研", dependencies=[], assigned_agent="builtin_researcher", risk_level="medium"),
            SubTask(id="sub_b", title="撰写报告", dependencies=["sub_a"], assigned_agent="builtin_writer", risk_level="high"),
        ],
        execution_order=[["sub_a"], ["sub_b"]],
    )


class _FakeLLM:
    """chat_messages_stream 直接抛异常 → 流式分解/推理走回退路径（不真调 LLM）。"""

    def __init__(self, *a, **kw):
        pass

    async def chat_messages_stream(self, messages):
        raise RuntimeError("fake llm down")
        yield  # pragma: no cover


class _NoClarify:
    needs_clarification = False
    open_questions = []


_FakeDomainPrior = SimpleNamespace(
    phases=[SimpleNamespace(model_dump=lambda: {"phase_id": "dp1", "label": "大纲", "description": ""})],
    typical_mode="pipeline",
    to_prompt_context=lambda: "领域最佳实践：大纲 → 初稿 → 审校",
)


def _parse(ev: str):
    """解析 _sse_event 输出 → (event_type, data_dict)"""
    event = None
    data = None
    for line in ev.strip().split("\n"):
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return event, data


@pytest.mark.asyncio
async def test_stream_emits_domain_prior():
    """PR-4：已知领域时 domain_prior 事件必须产出。"""
    from core.orchestration import planner

    critique = PlanCritique(issues=[])
    with patch("core.orchestration.planner.ensure_generic_agents", new=AsyncMock(return_value=[])):
        with patch("core.orchestration.planner._gather_capabilities", new=AsyncMock(return_value=[])):
            with patch("core.orchestration.planner._generate_project_name", new=AsyncMock(return_value="报告")):
                with patch.object(planner.TaskDecomposer, "decompose", new=AsyncMock(return_value=_fake_plan())):
                    with patch("core.orchestration.planner.LLMClient", new=_FakeLLM):
                        with patch("core.orchestration.intent.analyze_intent", new=AsyncMock(return_value=_NoClarify())):
                            with patch("core.orchestration.domain_prior.load_domain_prior", new=AsyncMock(return_value=_FakeDomainPrior)):
                                with patch("core.orchestration.plan_critic.critique_plan", new=AsyncMock(return_value=critique)):
                                    with patch("core.orchestration.template_matcher.get_template_matcher") as mock_get_matcher:
                                        mock_matcher = AsyncMock()
                                        mock_matcher.match = AsyncMock(return_value=[])
                                        mock_get_matcher.return_value = mock_matcher
                                        events = []
                                        async for ev in planner.plan_agent_order_stream(task="写一份报告", domain_id="platform:web_novel", strategy="pipeline"):
                                            events.append(ev)

    kinds = [_parse(e)[0] for e in events]
    assert "domain_prior" in kinds
    ev_type, data = _parse(next(e for e in events if e.startswith("event: domain_prior")))
    assert data["typical_mode"] == "pipeline"
    assert data["phases"][0]["phase_id"] == "dp1"


@pytest.mark.asyncio
async def test_stream_emits_critic_and_refine():
    """PR-10：critic/refine 事件必须产出（有 issue 时 refine 被调用）。"""
    from core.orchestration import planner

    critique = PlanCritique(
        issues=[CritiqueIssue(phase_id="sub_b", kind="missing_step", message="缺人工审核")],
        overall="计划基本合理，但缺人工审核环节",
    )
    with patch("core.orchestration.planner.ensure_generic_agents", new=AsyncMock(return_value=[])):
        with patch("core.orchestration.planner._gather_capabilities", new=AsyncMock(return_value=[])):
            with patch("core.orchestration.planner._generate_project_name", new=AsyncMock(return_value="报告")):
                with patch.object(planner.TaskDecomposer, "decompose", new=AsyncMock(return_value=_fake_plan())):
                    with patch("core.orchestration.planner.LLMClient", new=_FakeLLM):
                        with patch("core.orchestration.intent.analyze_intent", new=AsyncMock(return_value=_NoClarify())):
                            with patch("core.orchestration.domain_prior.load_domain_prior", new=AsyncMock(return_value=_FakeDomainPrior)):
                                with patch("core.orchestration.plan_critic.critique_plan", new=AsyncMock(return_value=critique)) as mock_critic:
                                    with patch("core.orchestration.plan_refiner.refine_plan", new=AsyncMock(return_value=None)) as mock_refine:
                                        with patch("core.orchestration.template_matcher.get_template_matcher") as mock_get_matcher:
                                            mock_matcher = AsyncMock()
                                            mock_matcher.match = AsyncMock(return_value=[])
                                            mock_get_matcher.return_value = mock_matcher
                                            events = []
                                            async for ev in planner.plan_agent_order_stream(task="写一份报告", domain_id="platform:web_novel", strategy="pipeline"):
                                                events.append(ev)

    kinds = [_parse(e)[0] for e in events]
    assert "critic" in kinds
    assert "refine" in kinds
    mock_critic.assert_called_once()
    mock_refine.assert_called_once()
    # critic done 事件带 issues + overall
    ev_type, data = _parse(next(e for e in events if e.startswith("event: critic") and '"done"' in e))
    assert data["issues"][0]["phase_id"] == "sub_b"
    assert data["overall"]


@pytest.mark.asyncio
async def test_plan_event_preserves_risk_level():
    """P0：plan 事件 pipeline 必须含 risk_level（确认门数据源，前端保存不丢）。"""
    from core.orchestration import planner

    critique = PlanCritique(issues=[])
    with patch("core.orchestration.planner.ensure_generic_agents", new=AsyncMock(return_value=[])):
        with patch("core.orchestration.planner._gather_capabilities", new=AsyncMock(return_value=[])):
            with patch("core.orchestration.planner._generate_project_name", new=AsyncMock(return_value="报告")):
                with patch.object(planner.TaskDecomposer, "decompose", new=AsyncMock(return_value=_fake_plan())):
                    with patch("core.orchestration.planner.LLMClient", new=_FakeLLM):
                        with patch("core.orchestration.intent.analyze_intent", new=AsyncMock(return_value=_NoClarify())):
                            with patch("core.orchestration.domain_prior.load_domain_prior", new=AsyncMock(return_value=_FakeDomainPrior)):
                                with patch("core.orchestration.plan_critic.critique_plan", new=AsyncMock(return_value=critique)):
                                    with patch("core.orchestration.template_matcher.get_template_matcher") as mock_get_matcher:
                                        mock_matcher = AsyncMock()
                                        mock_matcher.match = AsyncMock(return_value=[])
                                        mock_get_matcher.return_value = mock_matcher
                                        events = []
                                        async for ev in planner.plan_agent_order_stream(task="写一份报告", domain_id="platform:web_novel", strategy="pipeline"):
                                            events.append(ev)

    plan_evs = [e for e in events if e.startswith("event: plan")]
    assert plan_evs, "必须有 plan 事件"
    _, data = _parse(plan_evs[-1])
    by_id = {p["phase_id"]: p for p in data["pipeline"]}
    assert by_id["sub_a"]["risk_level"] == "medium"
    assert by_id["sub_b"]["risk_level"] == "high"
