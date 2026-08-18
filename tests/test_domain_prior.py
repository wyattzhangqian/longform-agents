"""PR-4 领域先验测试 — core/orchestration/domain_prior.py。

domain_prior 只做领域先验读取 + 标准化（phase_definitions/agent_role/artifact_files/
typical_agents/mode/example_prompt），不执行 Agent、不改变 GraphRuntime。
"""
import pytest

from core.orchestration.domain_prior import (
    DomainPrior,
    PhasePrior,
    normalize_domain_prior,
    load_domain_prior,
)


def test_normalize_domain_prior_from_dict():
    domain = {
        "id": "platform:web_novel",
        "name": "网文小说",
        "phase_definitions": [
            {"id": "outline", "label": "大纲", "description": "故事骨架", "agent_role": "策划", "artifact_files": ["outline.md"]},
            {"id": "draft", "label": "初稿", "description": "写正文", "agent_role": "撰稿", "artifact_files": ["chapter.md"]},
        ],
        "metadata": {"typical_agents": 3, "typical_mode": "adaptive", "example_prompt": "写仙侠小说"},
    }
    prior = normalize_domain_prior(domain)
    assert prior.domain_id == "platform:web_novel"
    assert prior.name == "网文小说"
    assert len(prior.phases) == 2
    assert prior.phases[0].agent_role == "策划"
    assert prior.phases[0].artifact_files == ["outline.md"]
    assert prior.typical_agents == 3
    assert prior.typical_mode == "adaptive"
    assert prior.example_prompt == "写仙侠小说"


def test_normalize_domain_prior_with_json_strings():
    """phase_definitions/metadata 是 JSON 字符串时也能解析。"""
    import json
    domain = {
        "id": "d",
        "phase_definitions": json.dumps([{"id": "p1", "label": "阶段1"}]),
        "metadata": json.dumps({"typical_mode": "pipeline"}),
    }
    prior = normalize_domain_prior(domain)
    assert prior.phases[0].id == "p1"
    assert prior.typical_mode == "pipeline"


def test_normalize_domain_prior_missing_fields():
    """缺 phase_definitions/metadata 时返回空先验（不崩）。"""
    prior = normalize_domain_prior({"id": "d", "name": "空领域"})
    assert prior.phases == []
    assert prior.typical_mode == ""


def test_to_prompt_context_includes_skeleton():
    prior = DomainPrior(
        domain_id="platform:web_novel",
        name="网文小说",
        phases=[PhasePrior(id="outline", label="大纲", agent_role="策划", artifact_files=["outline.md"])],
        typical_mode="adaptive",
        example_prompt="写仙侠小说",
    )
    ctx = prior.to_prompt_context()
    assert "网文小说" in ctx
    assert "大纲" in ctx
    assert "outline.md" in ctx
    assert "adaptive" in ctx


@pytest.mark.asyncio
async def test_load_domain_prior_returns_prior():
    """读平台 seed 领域（conftest 已 init_db + seed domains）。"""
    prior = await load_domain_prior("platform:web_novel")
    assert prior is not None
    assert prior.domain_id == "platform:web_novel"
    # web_novel 有 4 个阶段
    assert len(prior.phases) == 4
    assert all(p.id for p in prior.phases)


@pytest.mark.asyncio
async def test_load_domain_prior_unknown_returns_none():
    assert await load_domain_prior("nonexistent_domain") is None
    assert await load_domain_prior("") is None
