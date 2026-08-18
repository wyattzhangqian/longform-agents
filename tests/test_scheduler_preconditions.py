"""Phase 5 测试 — Scheduler 约束检查"""
import pytest
from core.graph.runtime import Scheduler, ScheduleResult
from core.run.run_context import RunContext


class MockNode:
    """简化的图节点"""
    def __init__(self, node_id, metadata=None):
        self.id = node_id
        self.metadata = metadata or {}


class MockGraph:
    """简化的图——只有 nodes 列表和 get_successors"""
    def __init__(self, nodes, edges=None):
        self.nodes = nodes
        self._edges = edges or []
        self._successors = {}
        for from_n, to_n in self._edges:
            self._successors.setdefault(from_n, []).append(to_n)

    def get_successors(self, node_id):
        return self._successors.get(node_id, [])


def _make_scheduler(nodes, ctx=None):
    """构造一个初始化好的 Scheduler"""
    graph = MockGraph(nodes)
    scheduler = Scheduler(graph)
    if ctx:
        scheduler.set_run_context(ctx)
    # 手动设置 ready 状态
    scheduler.ready = {n.id for n in nodes}
    scheduler.running = set()
    scheduler.completed = set()
    scheduler.failed = set()
    scheduler._initialized = True
    return scheduler


def test_no_precondition_defaults_to_pass():
    """无约束的节点行为不变"""
    nodes = [MockNode("a"), MockNode("b")]
    scheduler = _make_scheduler(nodes)
    result = scheduler.next()
    assert result is not None
    assert len(result.nodes) >= 1
    assert result.nodes[0] in {"a", "b"}


def test_precondition_blocks_node():
    """有未满足约束的节点不应出现在 next() 结果中"""
    # node_a 有 unit_completed 约束（要求 unit 3 已完成），但 current_unit=2
    nodes = [
        MockNode("blocked", metadata={"preconditions": [{"type": "unit_completed", "unit_number": 3}]}),
        MockNode("free"),  # 无约束
    ]
    ctx = RunContext()
    ctx.current_unit = 2
    scheduler = _make_scheduler(nodes, ctx=ctx)

    result = scheduler.next()
    assert result is not None
    # 应该只调度 "free" 节点，不调度 "blocked"
    assert "free" in result.nodes
    assert "blocked" not in result.nodes


def test_precondition_passes_when_met():
    """约束满足后节点应正常调度"""
    nodes = [
        MockNode("blocked", metadata={"preconditions": [{"type": "unit_completed", "unit_number": 3}]}),
    ]
    ctx = RunContext()
    ctx.current_unit = 5  # 已超过 unit 3
    scheduler = _make_scheduler(nodes, ctx=ctx)

    result = scheduler.next()
    assert result is not None
    assert "blocked" in result.nodes


def test_waiting_preconditions_not_deadlock():
    """所有 ready 节点都不满足约束 → 返回 waiting 而非 None（不是死锁）"""
    nodes = [
        MockNode("blocked1", metadata={"preconditions": [{"type": "unit_completed", "unit_number": 5}]}),
    ]
    ctx = RunContext()
    ctx.current_unit = 2
    scheduler = _make_scheduler(nodes, ctx=ctx)
    # 设置 running 为空，这样不会因为"等待 running"返回
    scheduler.running = set()

    result = scheduler.next()
    # 不应返回 None（那表示完成/死锁）
    assert result is not None
    # 应返回空节点列表 + waiting 原因
    assert len(result.nodes) == 0
    assert "precondition" in result.reason.lower()


def test_quality_passed_precondition():
    """quality_passed 约束：某节点的质量检查必须通过"""
    from core.run.run_context import QualityRecord
    nodes = [
        MockNode("dependent", metadata={"preconditions": [{"type": "quality_passed", "node_id": "phase_1"}]}),
    ]
    ctx = RunContext()
    ctx.quality_records = []  # 没有通过的质量记录
    scheduler = _make_scheduler(nodes, ctx=ctx)

    result = scheduler.next()
    assert result is not None
    assert len(result.nodes) == 0  # 被阻塞
    assert "precondition" in result.reason.lower()

    # 现在添加通过的质量记录
    ctx.quality_records = [QualityRecord(phase_id="phase_1", agent_id="a1", passed=True, score=0.9)]
    scheduler2 = _make_scheduler(nodes, ctx=ctx)
    result2 = scheduler2.next()
    assert result2 is not None
    assert "dependent" in result2.nodes  # 约束满足，正常调度


def test_set_run_context():
    """set_run_context 应正确设置 _run_context"""
    scheduler = _make_scheduler([MockNode("a")])
    assert scheduler._run_context is None

    ctx = RunContext()
    scheduler.set_run_context(ctx)
    assert scheduler._run_context is ctx
