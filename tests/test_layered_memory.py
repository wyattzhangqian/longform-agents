"""Phase 2 测试 — 分层记忆 + 连续性追踪 + MemoryCurator"""
import asyncio
import pytest

from core.memory.layered_memory import LayeredMemory
from core.memory.continuity_tracker import ContinuityTracker
from core.memory.curator import MemoryCurator


# ── LayeredMemory 测试 ──

def test_L4_build_from_skeleton():
    """L4 应包含 premise, theme, characters"""
    skeleton_dict = {
        "premise": "一个悬疑故事",
        "theme": "真相与谎言",
        "content_type": "novel",
        "total_units": 10,
        "arcs": [
            {"character_arcs": [{"character": "张三"}, {"character": "李四"}]}
        ],
    }
    mem = LayeredMemory()
    mem.build_L4(skeleton_dict)
    assert "一个悬疑故事" in mem.L4
    assert "真相与谎言" in mem.L4
    assert "张三" in mem.L4
    assert "李四" in mem.L4


def test_L2_sliding_window_cap():
    """超过 WINDOW_SIZE 时最早的摘要应被移除"""
    mem = LayeredMemory()
    for i in range(1, 8):  # 推入 7 条，WINDOW_SIZE=5
        mem.push_to_L2(i, f"summary {i}")
    assert len(mem.L2) == LayeredMemory.WINDOW_SIZE
    # 最早的两条应被移除
    assert mem.L2[0]["unit_number"] == 3
    assert mem.L2[-1]["unit_number"] == 7


def test_L2_format_output():
    """格式化输出应包含所有窗口内的摘要"""
    mem = LayeredMemory()
    mem.push_to_L2(1, "第一单元摘要")
    mem.push_to_L2(2, "第二单元摘要")
    text = mem.format_L2()
    assert "第一单元摘要" in text
    assert "第二单元摘要" in text
    assert "第 1 单元" in text or "第 1" in text


def test_layered_memory_serialization():
    """to_dict / from_run_context 互为逆操作"""
    mem = LayeredMemory()
    mem.build_L4({"premise": "test", "theme": "t", "content_type": "novel", "total_units": 5, "arcs": []})
    mem.push_to_L2(1, "summary 1")
    mem.push_to_L2(2, "summary 2")
    data = mem.to_dict()
    mem2 = LayeredMemory.from_run_context(data)
    assert mem2.L4 == mem.L4
    assert len(mem2.L2) == 2
    assert mem2.L2[0]["unit_number"] == 1


# ── ContinuityTracker 测试 ──

def test_update_from_declaration_introduced():
    """声明 introduced 时状态应从 planned → introduced"""
    threads = [
        {"id": "ct1", "status": "planned", "introduce_at": 1, "reference_at": [], "resolve_at": 5}
    ]
    tracker = ContinuityTracker(threads=threads)
    tracker.update_from_declaration(1, {"introduced": ["ct1"]})
    assert tracker.threads[0]["status"] == "introduced"


def test_check_overdue():
    """超过 resolve_at 未 resolve 的线索应标记为 overdue"""
    threads = [
        {"id": "ct1", "status": "introduced", "introduce_at": 1, "reference_at": [], "resolve_at": 3}
    ]
    tracker = ContinuityTracker(threads=threads)
    tracker.check_overdue(5)  # 当前单元 5 > resolve_at 3
    assert tracker.threads[0]["status"] == "overdue"


def test_format_for_context():
    """应正确格式化本单元的义务"""
    threads = [
        {"id": "ct1", "status": "planned", "introduce_at": 1, "reference_at": [], "resolve_at": 5, "content": "伏笔A", "significance": "重要"}
    ]
    tracker = ContinuityTracker(threads=threads)
    text = tracker.format_for_context(1)
    assert "ct1" in text
    assert "伏笔A" in text


# ── MemoryCurator 测试 ──

@pytest.mark.asyncio
async def test_process_unit_completion_updates_window():
    """完成单元后滑动窗口应新增一条"""
    ctx_dict = {
        "layered_memory": LayeredMemory().to_dict(),
        "continuity_state": [],
        "current_unit": 1,
    }
    curator = MemoryCurator(llm_client=None)
    updated = await curator.process_unit_completion(
        run_context_dict=ctx_dict,
        unit_number=1,
        agent_output={"raw_text": "这是一段创作内容" * 50},
    )
    mem = LayeredMemory.from_run_context(updated["layered_memory"])
    assert len(mem.L2) == 1
    assert mem.L2[0]["unit_number"] == 1
    assert updated["current_unit"] == 2


@pytest.mark.asyncio
async def test_process_unit_completion_updates_continuity():
    """声明 resolved 后连续性状态应更新"""
    threads = [
        {"id": "ct1", "status": "introduced", "introduce_at": 1, "reference_at": [], "resolve_at": 2, "content": "伏笔"}
    ]
    ctx_dict = {
        "layered_memory": LayeredMemory().to_dict(),
        "continuity_state": threads,
        "current_unit": 2,
    }
    curator = MemoryCurator(llm_client=None)
    updated = await curator.process_unit_completion(
        run_context_dict=ctx_dict,
        unit_number=2,
        agent_output={
            "result": {"continuity_declaration": {"resolved": ["ct1"]}},
            "raw_text": "内容",
        },
    )
    tracker = ContinuityTracker.from_state(updated["continuity_state"])
    assert tracker.threads[0]["status"] == "resolved"


@pytest.mark.asyncio
async def test_fallback_summary_without_llm():
    """无 LLM 时应截取前 300 字作为摘要"""
    ctx_dict = {
        "layered_memory": LayeredMemory().to_dict(),
        "continuity_state": None,
        "current_unit": 1,
    }
    long_content = "A" * 500
    curator = MemoryCurator(llm_client=None)
    updated = await curator.process_unit_completion(
        run_context_dict=ctx_dict,
        unit_number=1,
        agent_output={"raw_text": long_content},
    )
    mem = LayeredMemory.from_run_context(updated["layered_memory"])
    summary = mem.L2[0]["summary"]
    assert len(summary) <= 303  # 300 + "..."
    assert "..." in summary
