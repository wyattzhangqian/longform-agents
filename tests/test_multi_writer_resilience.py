"""多 Writer 异常路径 8 场景验收（2026-08-10 P0-5）。

覆盖：并行产物+run_id、超时/无产出/异常标 FAILED、空产出重试、Run 恢复、归属校验。
"""

import asyncio
import types

import pytest
from unittest.mock import AsyncMock, patch

from core.graph.types import GraphRunConfig, GraphState, NodeType
from core.run.run_context import RunContext


def _fake_graph():
    g = types.SimpleNamespace(graph_id="g1", nodes=[], edges=[])
    g.get_node = lambda nid: None
    g.get_predecessors = lambda nid: []
    g.get_successors = lambda nid: []
    g.get_edge = lambda a, b: None
    return g


def _definition(wid="w"):
    return types.SimpleNamespace(
        id=wid, name=f"写手{wid}", emoji="✍️", role="writer",
        model="deepseek-v4-flash", temperature=0.7, max_tokens=1000,
        system_prompt="写手", tool_ids=["file_write"], skill_ids=[],
        tools=[], model_profile=types.SimpleNamespace(image=None, video=None, music=None),
        extra={},
    )


def _node(nid="unit_1", aid="w", fail_fast=True):
    return types.SimpleNamespace(
        id=nid, type=NodeType.AGENT, agent_id=aid,
        label=nid, metadata={"phase_id": nid, "fail_fast": fail_fast},
    )


def _make_executor(run_ctx):
    from core.graph.runtime import NodeExecutor
    ex = NodeExecutor(graph=_fake_graph())
    ex.set_live_run_ctx(run_ctx)
    return ex


def _base_patches(ex):
    return patch.object(ex, "_build_agent_input", new=AsyncMock(return_value={})), \
           patch.object(ex, "_persist_agent_output", new=AsyncMock(return_value=None))


def _new_agent(defn):
    from core.agent.base import BaseAgent
    return BaseAgent(definition=defn)


# ── 场景 1/2：并行 Writer 各自产出，run_id 归属一致 ──

@pytest.mark.asyncio
async def test_parallel_writers_own_run_id():
    """2 个 writer 并行执行，各登记产物，run_id 归属当前 run（无串写）。"""
    from core.storage.database import get_db
    from core.vault.project_artifact_service import ProjectArtifactService

    db = await get_db()
    await db.execute("INSERT OR IGNORE INTO projects (id, name, status) VALUES (?,?, 'idle')", ("p_par", "并行"))
    await db.execute("DELETE FROM artifacts WHERE project_id='p_par'")

    async def _writer(i):
        aid = f"writer_{i}"
        await db.execute(
            "INSERT OR IGNORE INTO agents (id, name, type, status, emoji, role, capabilities, model) "
            "VALUES (?,?, 'custom','active','🤖','writer','[]','platform_default')",
            (aid, aid),
        )
        path = ProjectArtifactService.resolve_project_path("p_par", f"{aid}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#{aid} 正文", encoding="utf-8")
        await ProjectArtifactService.register_file(
            project_id="p_par", agent_id=aid, run_id="run_par",
            resolved_path=path, name=f"{aid}.md",
        )
        return aid

    results = await asyncio.gather(*[_writer(i) for i in (1, 2)])
    assert len(results) == 2

    rows = await (await db.execute(
        "SELECT agent_id, run_id FROM artifacts WHERE project_id='p_par' AND type='file'"
    )).fetchall()
    assert len(rows) == 2
    assert all(r["run_id"] == "run_par" for r in rows), f"run_id 归属应一致: {rows}"
    assert {r["agent_id"] for r in rows} == {"writer_1", "writer_2"}, "无串写"

    await db.execute("DELETE FROM artifacts WHERE project_id='p_par'")
    await db.commit()


# ── 场景 3：Writer 超时 → FAILED ──

@pytest.mark.asyncio
async def test_writer_timeout_failed():
    run_ctx = RunContext(run_id="r_t", project_id="p_t", task="t")
    ex = _make_executor(run_ctx)
    node = types.SimpleNamespace(
        id="unit_1", type=NodeType.AGENT, agent_id="w", label="u",
        metadata={"phase_id": "unit_1", "timeout_seconds": 0.1, "fail_fast": False},
    )
    defn = _definition("w")
    agent = _new_agent(defn)

    async def _hang(*a, **k):
        await asyncio.Event().wait()
    agent.execute = _hang
    ex._agent_cache["w"] = agent

    state = GraphState(values={"project_id": "p_t"})
    config = GraphRunConfig(project_id="p_t", phase_specs=[])
    with patch("core.agent.registry.get_registry") as m_reg, \
             patch.object(ex, "_build_agent_input", new=AsyncMock(return_value={})), \
             patch.object(ex, "_persist_agent_output", new=AsyncMock(return_value=None)):
        m_reg.return_value.get = AsyncMock(return_value=defn)
        result = await ex._execute_agent(node, state, config)

    assert result.get("status") == "error", f"超时应标 FAILED，实际 {result}"
    assert result.get("error_kind") == "timeout"


# ── 场景 4：Writer 无产出 → FAILED(no_output) ──

@pytest.mark.asyncio
async def test_writer_no_output_failed():
    from core.vault.project_artifact_service import ArtifactCheckResult
    run_ctx = RunContext(run_id="r_no", project_id="p_no", task="t")
    ex = _make_executor(run_ctx)
    defn = _definition("w")
    agent = _new_agent(defn)
    agent.execute = AsyncMock(return_value={"result": "", "raw": ""})
    ex._agent_cache["w"] = agent
    state = GraphState(values={"project_id": "p_no"})
    config = GraphRunConfig(project_id="p_no", phase_specs=[])
    with patch("core.agent.registry.get_registry") as m_reg, \
         patch("core.vault.project_artifact_service.ProjectArtifactService.check_phase_artifact",
               new=AsyncMock(return_value=ArtifactCheckResult.MISSING)), \
         patch.object(ex, "_build_agent_input", new=AsyncMock(return_value={})), \
         patch.object(ex, "_persist_agent_output", new=AsyncMock(return_value=None)):
        m_reg.return_value.get = AsyncMock(return_value=defn)
        result = await ex._execute_agent(_node(), state, config)

    assert result.get("status") == "error"
    assert result.get("error_kind") == "no_output"
    assert run_ctx.diagnostics["has_critical"] is True


# ── 场景 5：Writer 异常 → FAILED ──

@pytest.mark.asyncio
async def test_writer_exception_failed():
    run_ctx = RunContext(run_id="r_e", project_id="p_e", task="t")
    ex = _make_executor(run_ctx)
    defn = _definition("w")
    agent = _new_agent(defn)

    async def _boom(*a, **k):
        raise RuntimeError("LLM 挂了")
    agent.execute = _boom
    ex._agent_cache["w"] = agent
    state = GraphState(values={"project_id": "p_e"})
    config = GraphRunConfig(project_id="p_e", phase_specs=[])
    with patch("core.agent.registry.get_registry") as m_reg, \
             patch.object(ex, "_build_agent_input", new=AsyncMock(return_value={})), \
             patch.object(ex, "_persist_agent_output", new=AsyncMock(return_value=None)):
        m_reg.return_value.get = AsyncMock(return_value=defn)
        result = await ex._execute_agent(_node(fail_fast=False), state, config)

    assert result.get("status") == "error"
    assert result.get("error_kind") == "exception"


# ── 场景 6：空产出重试 → 第二次成功 ──

@pytest.mark.asyncio
async def test_writer_empty_retry_succeeds():
    from core.vault.project_artifact_service import ArtifactCheckResult
    run_ctx = RunContext(run_id="r_retry", project_id="p_r", task="t")
    ex = _make_executor(run_ctx)
    defn = _definition("w")
    agent = _new_agent(defn)
    calls = {"n": 0}

    async def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": "", "raw": ""}
        return {"result": "第一章正文……", "raw": ""}
    agent.execute = _flaky
    ex._agent_cache["w"] = agent
    state = GraphState(values={"project_id": "p_r"})
    config = GraphRunConfig(project_id="p_r", phase_specs=[])
    with patch("core.agent.registry.get_registry") as m_reg, \
         patch("core.vault.project_artifact_service.ProjectArtifactService.check_phase_artifact",
               new=AsyncMock(return_value=ArtifactCheckResult.PRESENT)), \
         patch.object(ex, "_build_agent_input", new=AsyncMock(return_value={})), \
         patch.object(ex, "_persist_agent_output", new=AsyncMock(return_value=None)):
        m_reg.return_value.get = AsyncMock(return_value=defn)
        result = await ex._execute_agent(_node(), state, config)

    assert calls["n"] >= 2, f"空产出应重试，实际 {calls['n']} 次"
    assert result.get("status") != "error", "重试成功后不应标 FAILED"
    assert "正文" in (result.get("result") or "")


# ── 场景 7：Run 恢复 — checkpoint 序列化保留状态 ──

@pytest.mark.asyncio
async def test_run_checkpoint_recovery_preserves_state():
    """checkpoint 依赖的序列化往返：to_workflow_state → from_workflow_state 保留状态。"""
    ctx = RunContext(run_id="run_cp", project_id="p_cp", task="t", total_units=3)
    ctx.skeleton = {"total_units": 3, "title": "测试"}
    ctx.completed_phase_ids = ["unit_1", "unit_2"]

    ws = ctx.to_workflow_state()
    restored = RunContext.from_workflow_state(ws)

    assert restored is not None
    assert restored.run_id == "run_cp"
    assert restored.total_units == 3
    assert "unit_2" in restored.completed_phase_ids
    assert (restored.skeleton or {}).get("total_units") == 3
    assert restored.project_id == "p_cp"


# ── 场景 8：旧 Run 产物不污染新 Run ──

@pytest.mark.asyncio
async def test_old_run_artifacts_do_not_pollute_new_run():
    from core.storage.database import get_db
    from core.vault.project_artifact_service import ProjectArtifactService

    db = await get_db()
    await db.execute("INSERT OR IGNORE INTO projects (id, name, status) VALUES (?,?, 'idle')", ("p_own2", "归属"))
    await db.execute("INSERT OR IGNORE INTO agents (id, name, type, status, emoji, role, capabilities, model) "
                     "VALUES (?,?, 'custom','active','🤖','writer','[]','platform_default')", ("w_own", "写"))
    await db.execute("DELETE FROM artifacts WHERE project_id='p_own2'")
    await db.execute(
        """INSERT INTO artifacts (id, project_id, agent_id, type, name, file_path, metadata, run_id)
           VALUES (?,?,?, 'file', 'old.md', 'p_own2/old.md', '{}', ?)""",
        ("art_old2", "p_own2", "w_own", "run_old2"),
    )
    await db.commit()

    has_new = await ProjectArtifactService.has_phase_artifact(
        "p_own2", "w_own", "unit_1", run_id="run_new2",
    )
    assert has_new is False, "旧 Run 产物不应被新 Run 匹配"
    has_old = await ProjectArtifactService.has_phase_artifact(
        "p_own2", "w_own", "unit_1", run_id="run_old2",
    )
    assert has_old is True

    await db.execute("DELETE FROM artifacts WHERE project_id='p_own2'")
    await db.commit()
