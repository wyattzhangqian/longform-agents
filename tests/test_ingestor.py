"""Phase 4 测试 — Ingestor 续写/仿写/改写/扩写"""
import asyncio
import json
import pytest

from core.ingestor.base import Ingestor, IngestorInput, ExistingUnit, ReferenceWork


class MockLLMClient:
    def __init__(self, response):
        self._response = response

    async def chat(self, prompt, system_prompt="", **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_continue_mode_extracts_settings():
    """续写模式应从已有内容提取全局设定"""
    analysis_json = json.dumps({
        "global_settings": "末世废土世界",
        "characters": [{"id": "c1", "name": "主角", "core_identity": "幸存者", "speech_style": "简短"}],
        "continuity_threads": [{"id": "ct1", "content": "神秘信号", "introduce_at": 1, "status": "introduced"}],
        "current_atmosphere": {"characters": {}, "scene": {"current_tone": "紧张"}},
        "summary": "主角在废土中生存",
    })
    mock = MockLLMClient(analysis_json)
    ingestor = Ingestor(llm_client=mock)
    output = await ingestor.ingest(IngestorInput(
        mode="continue",
        intent="续写故事",
        content_type="novel",
        total_units=10,
        existing_units=[
            ExistingUnit(unit_number=1, content="第一章内容" * 100, title="第一章"),
            ExistingUnit(unit_number=2, content="第二章内容" * 100, title="第二章"),
        ],
        continue_from=3,
    ))

    assert output.memory_preset is not None
    assert "末世废土世界" in output.memory_preset.get("L4", "")
    assert output.start_unit == 3
    assert output.continuity_preset is not None
    assert len(output.continuity_preset) >= 1


@pytest.mark.asyncio
async def test_continue_mode_preserves_existing():
    """续写模式应保留已有单元在 L2 滑动窗口"""
    mock = MockLLMClient(json.dumps({"global_settings": "test", "characters": [], "continuity_threads": [], "summary": ""}))
    ingestor = Ingestor(llm_client=mock)
    output = await ingestor.ingest(IngestorInput(
        mode="continue",
        intent="续写",
        existing_units=[
            ExistingUnit(unit_number=1, content="内容1" * 100),
            ExistingUnit(unit_number=2, content="内容2" * 100),
        ],
        continue_from=3,
    ))

    # L2 应有 2 条摘要
    mem = output.memory_preset
    assert len(mem["L2"]) == 2
    assert mem["L2"][0]["unit_number"] == 1


@pytest.mark.asyncio
async def test_reference_mode_injects_knowledge():
    """仿写模式应产出 knowledge_entries_to_inject"""
    rules_json = json.dumps([
        {"category": "style", "title": "冷峻叙事", "content": "使用短句和冷色调描写"},
        {"category": "structure", "title": "三幕结构", "content": "按三幕剧结构组织"},
    ])
    mock = MockLLMClient(rules_json)
    ingestor = Ingestor(llm_client=mock)
    output = await ingestor.ingest(IngestorInput(
        mode="reference",
        intent="仿写",
        reference_works=[
            ReferenceWork(title="参考作品", content="参考内容" * 100, aspects=["style", "structure"]),
        ],
    ))

    assert len(output.knowledge_entries_to_inject) == 2
    assert output.knowledge_entries_to_inject[0]["title"] == "冷峻叙事"


@pytest.mark.asyncio
async def test_rewrite_mode_sets_start_unit():
    """改写模式 start_unit 应为最小的重写单元号"""
    ingestor = Ingestor(llm_client=None)
    output = await ingestor.ingest(IngestorInput(
        mode="rewrite",
        rewrite_units=[3, 5, 7],
        rewrite_feedback="节奏太慢",
    ))
    assert output.start_unit == 3
    assert output.skeleton_planner_params.get("mode") == "rewrite"
    assert output.skeleton_planner_params.get("rewrite_units") == [3, 5, 7]


@pytest.mark.asyncio
async def test_fallback_on_llm_failure():
    """LLM 不可用时不应报错，返回最小化结果"""
    ingestor = Ingestor(llm_client=None)
    output = await ingestor.ingest(IngestorInput(
        mode="continue",
        intent="续写",
        existing_units=[
            ExistingUnit(unit_number=1, content="内容" * 100),
        ],
        continue_from=2,
    ))

    # 无 LLM 时仍应返回有效结果
    assert output.memory_preset is not None
    assert len(output.memory_preset["L2"]) == 1  # 至少有截取的摘要
    assert output.start_unit == 2


@pytest.mark.asyncio
async def test_create_mode_returns_empty():
    """create 模式直接返回空 output"""
    ingestor = Ingestor(llm_client=None)
    output = await ingestor.ingest(IngestorInput(mode="create"))
    assert output.start_unit == 1
    assert output.memory_preset is None


@pytest.mark.asyncio
async def test_reference_mode_fallback_without_llm():
    """仿写模式无 LLM 时返回 fallback 规则"""
    ingestor = Ingestor(llm_client=None)
    output = await ingestor.ingest(IngestorInput(
        mode="reference",
        reference_works=[
            ReferenceWork(title="参考", content="内容", aspects=["style"]),
        ],
    ))
    assert len(output.knowledge_entries_to_inject) >= 1
    assert "参考" in output.knowledge_entries_to_inject[0]["title"]
