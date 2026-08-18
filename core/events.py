"""EventBus — 全局事件总线

解决两个核心问题：
1. 层次倒挂：之前 templates 层从 api.routes 导入 _broadcast，模板层不该知道 API 层
2. 多项目隔离：按 project_id 分发 SSE 事件，避免用户间消息泄漏

用法：
    from core.events import event_bus

    # 广播事件（发送到所有 SSE 客户端）
    await event_bus.broadcast(project_id="proj_xxx", event="agent_state", data={"state": "running"})

    # 注册 SSE 客户端
    queue = asyncio.Queue()
    await event_bus.subscribe(project_id="proj_xxx", client_id="abc", queue=queue)

    # 发送（全局广播，不按项目过滤）
    event_bus.broadcast_all(event="system", data={"msg": "server restart"})
"""

from __future__ import annotations
import asyncio
import itertools
import json
import logging
from collections import deque
from typing import Dict, List, Optional

from core.observability.trace import get_trace_id

logger = logging.getLogger(__name__)

# 每项目保留的最近事件数（断线重连补发窗口）
REPLAY_BUFFER_SIZE = 500


class EventBus:
    """按 project_id 隔离的 SSE 事件总线

    架构：
        _clients: {
            project_id: {
                client_id: asyncio.Queue
            }
        }

    每个 SSE 连接创建一个 client_id，通过 subscribe/unsubscribe 管理生命周期。
    """

    def __init__(self):
        # project_id → {client_id → queue}
        self._clients: Dict[str, Dict[str, asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        # 保存 asyncio.Task 引用，防止被 GC 回收
        self._pending_tasks: set = set()
        # 断线重连补发：进程级单调序号 + 每项目环形缓冲
        self._seq = itertools.count(1)
        self._recent: Dict[str, deque] = {}

    async def subscribe(self, project_id: str, client_id: str, queue: asyncio.Queue):
        """注册一个 SSE 客户端

        Args:
            project_id: 项目 ID（用于按项目过滤事件）
            client_id: 客户端唯一标识（可用 uuid4 或 request id）
            queue: asyncio.Queue 用于推送事件
        """
        async with self._lock:
            if project_id not in self._clients:
                self._clients[project_id] = {}
            self._clients[project_id][client_id] = queue

    async def unsubscribe(self, project_id: str, client_id: str):
        """取消注册 SSE 客户端"""
        async with self._lock:
            if project_id in self._clients:
                self._clients[project_id].pop(client_id, None)
                if not self._clients[project_id]:
                    del self._clients[project_id]

    def replay_since(self, project_id: str, last_id: int) -> List[dict]:
        """断线重连补发：返回该项目 seq > last_id 的缓冲事件"""
        if not project_id:
            return []
        buf = self._recent.get(project_id)
        if not buf:
            return []
        return [p for p in buf if p.get("id", 0) > last_id]

    def deliver_local(self, event: str, data: dict, project_id: str = None):
        """仅投递本地 SSE 客户端（Redis 中继时使用，不重复持久化）"""
        payload = {"event": event, "data": data, "id": next(self._seq)}

        # 环形缓冲（按项目，供 Last-Event-ID 补发）
        if project_id:
            buf = self._recent.setdefault(project_id, deque(maxlen=REPLAY_BUFFER_SIZE))
            buf.append(payload)

        if project_id and project_id in self._clients:
            targets = {project_id: self._clients[project_id]}
        elif project_id is None:
            targets = self._clients
        else:
            return

        for pid, clients in list(targets.items()):
            for cid, queue in list(clients.items()):
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
                except Exception as e:
                    logger.warning("SSE 客户端异常，取消订阅: pid=%s cid=%s err=%s", pid, cid, e)
                    task = asyncio.create_task(self.unsubscribe(pid, cid))
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)

    def broadcast(self, event: str, data: dict, project_id: str = None):
        """推送事件给指定项目（或所有）的 SSE 客户端"""
        trace_id = get_trace_id()
        if trace_id:
            data = {**data, "trace_id": trace_id}

        # 持久化 + 指标（非阻塞，仅 origin 实例）
        if project_id:
            try:
                from core.observability.workflow_events import WorkflowEventStore
                from core.observability.metrics import metrics
                WorkflowEventStore.append_background(project_id, event, data, trace_id)
                metrics.record_event(event)
            except Exception as e:
                logger.debug("工作流事件持久化/指标记录失败（非致命）: %s", e)

        self.deliver_local(event, data, project_id)

        # Redis 跨实例 SSE
        try:
            from core.observability.redis_bridge import get_redis_sse_bridge
            get_redis_sse_bridge(self).publish_background(project_id, event, data)
        except Exception as e:
            logger.debug("Redis SSE 跨实例广播失败（非致命，单实例部署正常）: %s", e)

        # 全局广播：project_id=None 时 deliver_local 已处理所有客户端
        if project_id is None:
            return

        # project_id 指定但无本地订阅者时，deliver_local 已 return；Redis 仍已 publish

    def broadcast_all(self, event: str, data: dict):
        """广播全局事件（所有项目所有客户端）"""
        self.broadcast(event=event, data=data, project_id=None)

    @property
    def client_count(self) -> int:
        """活跃客户端总数"""
        count = sum(len(clients) for clients in self._clients.values())
        try:
            from core.observability.metrics import metrics
            metrics.set_sse_clients(count)
        except Exception as e:
            logger.debug("SSE 客户端计数指标更新失败（非致命）: %s", e)
        return count

    @property
    def project_count(self) -> int:
        """已连接的项目数"""
        return len(self._clients)


# 全局单例
event_bus = EventBus()


async def broadcast_event(project_id: str, event: str, data: dict) -> None:
    """跨模块统一 SSE 广播入口（async 薄封装）

    P0-1 修复：此前 7 文件 12 处 from core.events import broadcast_event
    调用一个不存在的函数，被 except 吞掉导致整类长内容/产物/竞争 SSE 事件丢失。
    此封装内部转调同步的 event_bus.broadcast。
    """
    event_bus.broadcast(event=event, data=data, project_id=project_id)
