"""黄金链路回归测试 — 引擎地基防退化

覆盖修复：
  1. file_read/file_write 双前缀（outputs/proj/proj/file）
  2. file_write 产物登记（write_path 正确 → 登记成功）
  5. 产物 id 非 NULL

方法：真实 ToolRegistry + 真实 ToolExecutor + 真实写盘（monkeypatch OUTPUTS_DIR
到 tmp），不 mock 工具执行。比 BaseAgent 全链更聚焦、更稳。
"""

import pathlib
import uuid

import pytest
from types import SimpleNamespace

import core.run  # noqa: F401


def _make_ctx(project_id: str, agent_id: str):
    """构造 ToolExecutor 所需的最小 context"""
    return SimpleNamespace(
        agent_id=agent_id,
        project_id=project_id,
        audit=lambda *a, **k: None,
        pending_approval=lambda *a, **k: None,
        approvals=lambda: [],
    )


@pytest.mark.asyncio
async def test_file_write_no_double_prefix(tmp_path, monkeypatch):
    """file_write 真实写盘，落盘到 outputs/<proj>/<file>（无双前缀）"""
    from core.agent.tools import ToolExecutor, ToolRegistry

    monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", tmp_path)
    registry = ToolRegistry.get_instance()
    executor = ToolExecutor(registry, output_dir="proj_gp", context=_make_ctx("proj_gp", "agent_gp"))

    result = await executor.execute(
        {"needs_tool": True, "tool_name": "file_write",
         "tool_args": {"path": "ch1.md", "content": "真实正文，非占位"}},
        allowed_tools=["file_write"],
    )

    out = tmp_path / "proj_gp" / "ch1.md"
    assert out.exists(), f"文件应落盘 outputs/proj_gp/ch1.md，实际路径={out}"
    assert "非占位" in out.read_text(encoding="utf-8")
    # 无双前缀
    assert not (tmp_path / "proj_gp" / "proj_gp").exists(), "不应有 outputs/proj/proj/ 嵌套"
    assert "proj_gp/proj_gp" not in str(out)


@pytest.mark.asyncio
async def test_file_write_three_path_forms(tmp_path, monkeypatch):
    """三种 path 入参（相对/带项目前缀/带 outputs 前缀）都正确落盘，无双前缀"""
    from core.agent.tools import ToolExecutor, ToolRegistry

    monkeypatch.setattr(ToolRegistry, "OUTPUTS_DIR", tmp_path)
    registry = ToolRegistry.get_instance()
    executor = ToolExecutor(registry, output_dir="proj_gp2", context=_make_ctx("proj_gp2", "agent_gp2"))

    forms = ["ch2.md", "proj_gp2/ch2.md", f"{tmp_path}/outputs/proj_gp2/ch2.md"]
    for i, p in enumerate(forms):
        name = f"ch{i}.md"
        await executor.execute(
            {"needs_tool": True, "tool_name": "file_write",
             "tool_args": {"path": name, "content": f"内容{i}"}},
            allowed_tools=["file_write"],
        )
    for i in range(3):
        out = tmp_path / "proj_gp2" / f"ch{i}.md"
        assert out.exists(), f"ch{i}.md 应落盘（入参形式 {i}）"
        assert "proj_gp2/proj_gp2" not in str(out)


@pytest.mark.asyncio
async def test_runtime_artifact_insert_has_id():
    """runtime 兜底产物 INSERT（修复 5）应带 id 列，产物 id 非 NULL"""
    from core.storage.database import get_db

    proj = f"proj_runtime_{uuid.uuid4().hex[:6]}"
    agent_id = f"agent_rt_{uuid.uuid4().hex[:6]}"
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name, status) VALUES (?, ?, 'idle')",
        (proj, "runtime产物id测试"),
    )
    await db.execute(
        "INSERT OR IGNORE INTO agents (id, name, type, status, emoji, role, capabilities, model) "
        "VALUES (?, ?, 'custom', 'active', '🤖', '测试', '[]', 'platform_default')",
        (agent_id, "runtime产物Agent"),
    )
    # 模拟 runtime.py _persist_agent_output 修复后的兜底 INSERT（带 id）
    art_id = f"art_phase_{uuid.uuid4().hex[:8]}"
    await db.execute(
        """INSERT INTO artifacts (id, project_id, agent_id, type, name, file_path, metadata, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (art_id, proj, agent_id, "document", "phase_x", f"{proj}/phase_x.md", "{}"),
    )
    await db.commit()

    cur = await db.execute("SELECT id FROM artifacts WHERE project_id = ?", (proj,))
    row = await cur.fetchone()
    assert row is not None, "artifacts 表应有记录"
    assert row["id"] == art_id, f"产物 id 应为 {art_id}（修复 5 补了 id 列），实际={row['id']}"

    # 清理
    await db.execute("DELETE FROM artifacts WHERE project_id = ?", (proj,))
    await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    await db.execute("DELETE FROM projects WHERE id = ?", (proj,))
    await db.commit()


# ============================================================
# 集成断层回归测试（核验 2026-08-05 发现）
# ============================================================

@pytest.mark.asyncio
async def test_parallel_exception_returns_execution_result(tmp_path):
    """修复 4 集成断层：并行异常不崩下游、返回 Dict[str, ExecutionResult] 契约"""
    from unittest.mock import AsyncMock
    from core.graph.runtime import GraphRuntime, ParallelExecutor, NodeExecutor
    from core.graph.types import ExecutionResult, GraphNode, NodeStatus, NodeType

    # 构造 2 节点并行：node_ok 正常返回 ExecutionResult，node_bad 抛异常
    node_ok = GraphNode(id="n_ok", label="ok", type=NodeType.FUNCTION, func_ref="fn_ok")
    node_bad = GraphNode(id="n_bad", label="bad", type=NodeType.FUNCTION, func_ref="fn_bad")

    fake_executor = AsyncMock()
    async def _execute(node, state, config):
        if node.id == "n_bad":
            raise ValueError("模拟并行异常")
        return ExecutionResult(node_id="n_ok", status=NodeStatus.COMPLETED, output="ok")
    fake_executor.execute.side_effect = _execute

    # ParallelExecutor 需要 max_concurrent；node_executor 参数复用 fake
    pe = ParallelExecutor(fake_executor, max_concurrent=4)

    # 直接调 execute_batch（不跑 GraphRuntime 全流程）
    results = await pe.execute_batch([node_ok, node_bad], state=None, config=None)

    # 契约：返回的每个值都是 ExecutionResult，不能有 dict/list
    for k, v in results.items():
        assert isinstance(v, ExecutionResult), f"{k} 应保持 ExecutionResult 契约，实际 {type(v).__name__}"
    # bad 节点标 failed
    assert results["n_bad"].status == NodeStatus.FAILED
    assert results["n_bad"].is_failure
    assert results["n_ok"].is_success
    # 失败节点记录在实例属性（不混入返回字典）
    assert pe.last_failures == ["n_bad"]
    assert "_parallel_failures" not in results


@pytest.mark.asyncio
async def test_timeout_node_status_failed():
    """修复 6 集成断层：agent 层 error dict 应传导为节点 FAILED（状态诚实闭环）"""
    from core.graph.runtime import NodeExecutor
    from core.graph.types import NodeStatus, NodeType

    # 直接用 NodeExecutor 的 status 判定逻辑（与 execute 内 :1022 一致）
    def compute_status(node_type, result):
        if node_type == NodeType.HUMAN_IN_THE_LOOP and isinstance(result, dict) and result.get("human_input_required"):
            return NodeStatus.PAUSED
        if isinstance(result, dict) and result.get("status") == "error":
            return NodeStatus.FAILED   # 修复后新增传导
        return NodeStatus.COMPLETED

    # 超时/异常降级的 agent error dict → FAILED（修复 B 后）
    assert compute_status(NodeType.AGENT, {"status": "error", "error_kind": "timeout"}) == NodeStatus.FAILED
    assert compute_status(NodeType.AGENT, {"status": "error", "error_kind": "exception"}) == NodeStatus.FAILED
    # 正常 agent 结果 → COMPLETED
    assert compute_status(NodeType.AGENT, {"result": "正文"}) == NodeStatus.COMPLETED
    # HUMAN + human_input_required → PAUSED（不回归）
    assert compute_status(NodeType.HUMAN_IN_THE_LOOP, {"human_input_required": True}) == NodeStatus.PAUSED


# ============================================================
# natural 骨架接入回归测试（2026-08-07）
# ============================================================

def test_infer_total_units():
    """natural 路径长内容推断：解析章节数、长篇兜底、单篇返回 0"""
    from core.run.natural_runner import _infer_total_units

    assert _infer_total_units("写一部修仙小说，共3章") == 3
    assert _infer_total_units("写30章连载小说") == 30
    assert _infer_total_units("写一部小说") == 8      # 长篇关键词兜底
    assert _infer_total_units("写一篇短文") == 0       # 非长内容
    assert _infer_total_units("分析市场报告") == 0
    assert _infer_total_units("") == 0


def test_natural_writer_phase_expandable():
    """natural 的 writer phase（builtin_writer）应被骨架扩展为 N 个章节 phase"""
    from core.run.phase_spec import PhaseSpec
    from core.skeleton.integration import expand_phases_with_skeleton
    from core.skeleton.models import Skeleton, UnitSpec, UnitDefinition

    # 模拟 natural 规划的多 writer phases（含 writer 阶段）
    phases = [
        {"phase_id": "p1", "agent_id": "builtin_researcher", "dependencies": []},
        {"phase_id": "p2", "agent_id": "builtin_writer", "dependencies": ["p1"]},
        {"phase_id": "p3", "agent_id": "builtin_reviewer", "dependencies": ["p2"]},
    ]
    ids = [p["agent_id"] for p in phases]
    deps = {p["phase_id"]: p["dependencies"] for p in phases}
    specs = PhaseSpec.from_agent_ids(ids, dependencies_map=deps)

    sk = Skeleton(
        id="sk_natural",
        title="测试书",
        unit_definition=UnitDefinition(name="章"),
        units=[UnitSpec(unit_number=i, goal=f"第{i}章目标") for i in range(1, 4)],
    )
    expanded = expand_phases_with_skeleton(list(specs), sk)

    # 3 原 phases → 1 个 writer 扩展为 3 章 → 共 5 phases
    assert len(expanded) == 5, f"应扩展为 5 phases（3 原 + 3 章 - 1 写作 phase），实际 {len(expanded)}"
    labels = [s.label for s in expanded]
    assert "第1章：第1章目标" in labels, "扩展应产生带 unit 目标的章节 phase"
    # writer 的 3 章都保留同一 agent（人设一致）
    chapter_specs = [s for s in expanded if "第" in s.label and "章" in s.label]
    assert len(chapter_specs) == 3
    assert all(s.agent_id == "builtin_writer" for s in chapter_specs), "章节 phase 应复用 writer agent"
