"""拓扑推断：agent_ids 无依赖时应默认 pipeline，而非 parallel fork。"""
from __future__ import annotations

import pytest

from core.run.phase_spec import PhaseSpec
from core.run.template_compiler import TemplateCompiler, _infer_topology_from_specs


def test_from_agent_ids_injects_linear_dependencies():
    specs = PhaseSpec.from_agent_ids(["a", "b", "c"])
    assert specs[0].metadata.get("dependencies", []) == []
    assert specs[1].metadata.get("dependencies") == ["a"]
    assert specs[2].metadata.get("dependencies") == ["b"]
    assert _infer_topology_from_specs(specs) == "sequential"


def test_from_agent_ids_respects_explicit_dependencies_map():
    specs = PhaseSpec.from_agent_ids(
        ["a", "b", "c"],
        dependencies_map={"a": [], "b": ["a"], "c": ["a", "b"]},
    )
    assert specs[2].metadata["dependencies"] == ["a", "b"]
    assert _infer_topology_from_specs(specs) == "dag"


def test_compile_agent_ids_is_pipeline_not_fork():
    graph, specs = TemplateCompiler.compile(
        agent_ids=["agent_x", "agent_y", "agent_z"],
        graph_id="topo_pipeline_test",
    )
    node_ids = [n.id for n in graph.nodes]
    assert "fork_start" not in node_ids
    assert "agent_x" in node_ids
    assert "agent_y" in node_ids
    assert any(n.id.startswith("ckpt_") for n in graph.nodes)


def test_compile_mode_parallel_still_forks():
    graph, _ = TemplateCompiler.compile(
        agent_ids=["p1", "p2"],
        mode="parallel",
        graph_id="topo_parallel_test",
    )
    node_ids = [n.id for n in graph.nodes]
    assert "fork_start" in node_ids


def test_compile_preserves_passed_specs_dependencies():
    specs = PhaseSpec.from_agent_ids(
        ["s1", "s2"],
        dependencies_map={"s1": [], "s2": ["s1"]},
    )
    graph, out = TemplateCompiler.compile(specs=specs, graph_id="topo_specs_test")
    assert out[1].metadata.get("dependencies") == ["s1"]
    assert "fork_start" not in [n.id for n in graph.nodes]


def test_compile_cache_key_differs_for_different_specs_deps():
    """不同 dependencies 的 specs 不得撞同一编译缓存。"""
    from core.run.template_compiler import invalidate_compile_cache, _make_cache_key, _COMPILE_CACHE

    invalidate_compile_cache()
    sequential = PhaseSpec.from_agent_ids(
        ["a", "b", "c"],
        dependencies_map={"a": [], "b": ["a"], "c": ["b"]},
    )
    dag = PhaseSpec.from_agent_ids(
        ["a", "b", "c"],
        dependencies_map={"a": [], "b": ["a"], "c": ["a", "b"]},
    )
    k1 = _make_cache_key(None, None, "auto", False, None, None, "", specs=sequential)
    k2 = _make_cache_key(None, None, "auto", False, None, None, "", specs=dag)
    assert k1 != k2

    _, out1 = TemplateCompiler.compile(specs=sequential, graph_id="cache_seq")
    cache_size_after_first = len(_COMPILE_CACHE)
    _, out2 = TemplateCompiler.compile(specs=dag, graph_id="cache_dag")
    assert len(_COMPILE_CACHE) == cache_size_after_first + 1
    assert out1[2].metadata.get("dependencies") == ["b"]
    assert out2[2].metadata.get("dependencies") == ["a", "b"]
    # 连续两次不同 plan：不得因缓存串成同一 deps 输出
    assert out1[2].metadata.get("dependencies") != out2[2].metadata.get("dependencies")
