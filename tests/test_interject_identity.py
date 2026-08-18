"""P0-2 修复测试：interject A/B 身份断裂根治。

验证：
- CollaborationGraph.run_context(A) 与 GraphRuntime._live_run_ctx(B) 指向同一对象
- inject_interjection 追加对运行中 agent 读取的 live ctx 立即可见
- resume（_init_live_run_ctx 重跑）不丢失插话、不复裂
- checkpoint 序列化兼容：config.model_dump() 中 run_context 仍为 dict
"""
import pytest

from core.graph.runtime import GraphRuntime
from core.graph.types import WorkflowGraph
from core.run.collaboration_graph import CollaborationGraph
from core.run.run_context import HumanInterjection, RunContext


def _make_collab():
    """构造最小 CollaborationGraph + GraphRuntime 并完成 _init_live_run_ctx。"""
    graph = WorkflowGraph(graph_id="p02_test")
    ctx = RunContext(project_id="p02_proj", task="测试任务")
    collab = CollaborationGraph(graph, [], ctx, mode="sequential")
    runtime = GraphRuntime(graph)
    runtime.config = collab._build_run_config()
    runtime._init_live_run_ctx()
    collab.runtime = runtime
    return collab, runtime


def _make_interjection(content: str = "请注意这个设定"):
    return HumanInterjection(id="ij_p02", content=content)


def test_runtime_live_ctx_is_same_object_as_collab_run_context():
    """A/B 合一：runtime._live_run_ctx 与 collab.run_context 是同一对象。"""
    collab, runtime = _make_collab()
    assert runtime._live_run_ctx is collab.run_context


def test_inject_interjection_visible_to_running_agent():
    """注入插话后，运行中 agent 读取的 live ctx 立即可见（同一对象、同一条目）。"""
    collab, runtime = _make_collab()
    ij = _make_interjection()
    collab.inject_interjection(ij)

    assert ij in collab.run_context.human_interjections
    assert ij in runtime._live_run_ctx.human_interjections
    # 同一对象 → 同一列表（不是两份拷贝，不会出现"写 A 读 B 不可见"）
    assert runtime._live_run_ctx.human_interjections is collab.run_context.human_interjections


def test_resume_reuses_object_and_keeps_interjection():
    """resume（_init_live_run_ctx 重跑）复用同一对象，不丢插话。"""
    collab, runtime = _make_collab()
    ij = _make_interjection()
    collab.inject_interjection(ij)

    # 模拟暂停后 resume：运行时重新初始化 live ctx
    runtime._init_live_run_ctx()

    assert runtime._live_run_ctx is collab.run_context
    assert ij in runtime._live_run_ctx.human_interjections


def test_serialization_roundtrip_keeps_dict_contract():
    """checkpoint 序列化兼容：config.model_dump() 中 run_context 仍输出 dict。"""
    collab, runtime = _make_collab()
    dumped = runtime.config.model_dump()
    assert isinstance(dumped["run_context"], dict)
