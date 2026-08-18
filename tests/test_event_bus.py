"""EventBus 测试 — 覆盖 subscribe/unsubscribe/broadcast/deliver/GC 安全"""

import asyncio
import pytest


class TestEventBusInit:
    """初始化"""

    def test_create(self, event_bus):
        assert event_bus._clients == {}
        assert event_bus._pending_tasks == set()

    def test_client_count_empty(self, event_bus):
        assert event_bus.client_count == 0

    def test_project_count_empty(self, event_bus):
        assert event_bus.project_count == 0


@pytest.mark.asyncio
class TestEventBusSubscribeUnsubscribe:
    """订阅/取消订阅"""

    async def test_subscribe_single(self, event_bus):
        q = asyncio.Queue()
        await event_bus.subscribe("proj_1", "client_a", q)
        assert event_bus.client_count == 1
        assert event_bus.project_count == 1

    async def test_subscribe_multiple_clients(self, event_bus):
        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        await event_bus.subscribe("proj_1", "c1", q1)
        await event_bus.subscribe("proj_1", "c2", q2)
        assert event_bus.client_count == 2

    async def test_subscribe_multiple_projects(self, event_bus):
        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        await event_bus.subscribe("proj_1", "c1", q1)
        await event_bus.subscribe("proj_2", "c1", q2)
        assert event_bus.project_count == 2

    async def test_unsubscribe(self, event_bus):
        q = asyncio.Queue()
        await event_bus.subscribe("proj_1", "c1", q)
        await event_bus.unsubscribe("proj_1", "c1")
        assert event_bus.client_count == 0
        assert event_bus.project_count == 0

    async def test_unsubscribe_nonexistent(self, event_bus):
        # 不应抛异常
        await event_bus.unsubscribe("no_proj", "no_client")

    async def test_unsubscribe_removes_project(self, event_bus):
        q = asyncio.Queue()
        await event_bus.subscribe("proj_1", "only_client", q)
        await event_bus.unsubscribe("proj_1", "only_client")
        assert "proj_1" not in event_bus._clients


@pytest.mark.asyncio
class TestEventBusDeliverLocal:
    """本地投递测试"""

    async def test_deliver_to_correct_project(self, event_bus):
        q = asyncio.Queue()
        await event_bus.subscribe("proj_target", "c1", q)
        event_bus.deliver_local("test_event", {"msg": "hello"}, project_id="proj_target")

        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert msg["event"] == "test_event"
        assert msg["data"]["msg"] == "hello"

    async def test_deliver_not_cross_project(self, event_bus):
        q = asyncio.Queue()
        await event_bus.subscribe("proj_a", "c1", q)
        event_bus.deliver_local("test", {}, project_id="proj_b")  # 投递到 proj_b
        assert q.empty()  # proj_a 的队列不应收到消息

    async def test_deliver_all_projects_none(self, event_bus):
        qa = asyncio.Queue()
        qb = asyncio.Queue()
        await event_bus.subscribe("proj_a", "c1", qa)
        await event_bus.subscribe("proj_b", "c1", qb)

        event_bus.deliver_local("broadcast", {"all": True}, project_id=None)

        msg_a = await asyncio.wait_for(qa.get(), timeout=1.0)
        msg_b = await asyncio.wait_for(qb.get(), timeout=1.0)
        assert msg_a["event"] == "broadcast"
        assert msg_b["event"] == "broadcast"

    async def test_deliver_to_unknown_project_silent(self, event_bus):
        # 不应抛异常
        event_bus.deliver_local("test", {}, project_id="nonexistent")


@pytest.mark.asyncio
class TestEventBusBroadcast:
    """broadcast 方法（持久化 + 本地投递）测试"""

    async def test_broadcast_basic(self, event_bus):
        q = asyncio.Queue()
        await event_bus.subscribe("p1", "c1", q)
        event_bus.broadcast("phase_start", {"phase": "writer"}, project_id="p1")

        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert msg["event"] == "phase_start"
        assert msg["data"]["phase"] == "writer"

    async def test_broadcast_all(self, event_bus):
        qa = asyncio.Queue()
        qb = asyncio.Queue()
        await event_bus.subscribe("p1", "c1", qa)
        await event_bus.subscribe("p2", "c1", qb)

        event_bus.broadcast_all("system", {"msg": "restart"})

        msg_a = await asyncio.wait_for(qa.get(), timeout=1.0)
        msg_b = await asyncio.wait_for(qb.get(), timeout=1.0)
        assert msg_a["data"]["msg"] == "restart"


class TestEventBusGCSafety:
    """GC 安全性：asyncio.create_task 引用保存测试"""

    def test_pending_tasks_set_exists(self, event_bus):
        assert hasattr(event_bus, '_pending_tasks')
        assert isinstance(event_bus._pending_tasks, set)


@pytest.mark.asyncio
class TestEventBusQueueFull:
    """队列满时的处理：丢弃溢出事件（背压），但保留订阅（慢消费者不被踢出）"""

    async def test_full_queue_drops_event_keeps_client(self, event_bus):
        tiny_q = asyncio.Queue(maxsize=1)
        await event_bus.subscribe("p1", "full_client", tiny_q)

        # 填满队列
        tiny_q.put_nowait({"event": "fill"})
        assert tiny_q.full()

        # 再投递一个：溢出事件被丢弃，不抛异常
        event_bus.deliver_local("overflow", {}, project_id="p1")
        await asyncio.sleep(0.1)

        # 客户端仍保留订阅，队列中仍是原事件
        assert "full_client" in event_bus._clients.get("p1", {})
        assert tiny_q.qsize() == 1
        assert tiny_q.get_nowait()["event"] == "fill"

    async def test_broken_queue_unsubscribes_client(self, event_bus):
        """投递抛非 QueueFull 异常（损坏的客户端）→ 自动取消订阅"""

        class BrokenQueue(asyncio.Queue):
            def put_nowait(self, item):
                raise RuntimeError("broken client")

        await event_bus.subscribe("p1", "broken_client", BrokenQueue())
        event_bus.deliver_local("ping", {}, project_id="p1")
        await asyncio.sleep(0.2)
        assert "broken_client" not in event_bus._clients.get("p1", {})
