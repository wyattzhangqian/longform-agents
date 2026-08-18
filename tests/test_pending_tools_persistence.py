"""待审批工具暂存测试 — 内存 API + SQLite 持久化恢复（P0 运行态持久化）"""

import asyncio
import pytest

from core.gateway import pending_tools


@pytest.fixture(autouse=True)
def _clean():
    pending_tools.clear_all()
    yield
    pending_tools.clear_all()


@pytest.mark.asyncio
class TestPendingToolsMemory:
    async def test_register_and_get(self):
        pending_tools.register_pending(
            "tc_1",
            project_id="test_proj_001",
            agent_id="a1",
            tool_name="file_write",
            tool_args={"path": "out.txt"},
            reason="需要审批",
        )
        record = pending_tools.get_pending("tc_1")
        assert record is not None
        assert record["tool_name"] == "file_write"
        assert record["tool_args"]["path"] == "out.txt"

    async def test_list_by_project(self):
        pending_tools.register_pending(
            "tc_a", project_id="test_proj_001", agent_id="a", tool_name="t", tool_args={},
        )
        pending_tools.register_pending(
            "tc_b", project_id="test_proj_002", agent_id="a", tool_name="t", tool_args={},
        )
        assert len(pending_tools.list_pending("test_proj_001")) == 1
        assert len(pending_tools.list_pending("test_proj_002")) == 1

    async def test_pop_removes(self):
        pending_tools.register_pending(
            "tc_pop", project_id="test_proj_001", agent_id="a", tool_name="t", tool_args={},
        )
        popped = pending_tools.pop_pending("tc_pop")
        assert popped is not None
        assert pending_tools.get_pending("tc_pop") is None
        assert pending_tools.pop_pending("tc_pop") is None


@pytest.mark.asyncio
class TestPendingToolsPersistence:
    async def test_register_persists_and_reloads(self):
        """注册 → 落库 → 清空内存 → 从 DB 恢复（模拟进程重启）"""
        pending_tools.register_pending(
            "tc_persist",
            project_id="test_proj_001",
            agent_id="a1",
            tool_name="shell_exec",
            tool_args={"cmd": "ls"},
            reason="高危操作",
        )
        # 等待 write-behind 任务完成
        await asyncio.sleep(0.1)

        # 模拟进程重启：内存清空
        pending_tools.clear_all()
        assert pending_tools.get_pending("tc_persist") is None

        restored = await pending_tools.load_pending_from_db()
        assert restored >= 1
        record = pending_tools.get_pending("tc_persist")
        assert record is not None
        assert record["tool_name"] == "shell_exec"
        assert record["reason"] == "高危操作"

        # 清理 DB
        pending_tools.pop_pending("tc_persist")
        await asyncio.sleep(0.1)

    async def test_pop_removes_from_db(self):
        pending_tools.register_pending(
            "tc_gone", project_id="test_proj_001", agent_id="a", tool_name="t", tool_args={},
        )
        await asyncio.sleep(0.1)
        pending_tools.pop_pending("tc_gone")
        await asyncio.sleep(0.1)

        pending_tools.clear_all()
        await pending_tools.load_pending_from_db()
        assert pending_tools.get_pending("tc_gone") is None
