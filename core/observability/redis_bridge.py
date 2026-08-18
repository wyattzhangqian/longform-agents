"""Redis Pub/Sub — SSE 跨实例广播（编排仍单 leader，仅同步 SSE 事件）"""

from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING, Optional

from config import REDIS_URL, SSE_REDIS_ENABLED, INSTANCE_ID
from core.logging import get_logger

if TYPE_CHECKING:
    from core.events import EventBus

_logger = get_logger("redis_sse")

CHANNEL = "agent_platform:sse"


class RedisSSEBridge:
    """将 EventBus 事件通过 Redis 同步到其他 uvicorn 实例的 SSE 客户端"""

    def __init__(self, event_bus: "EventBus"):
        self._bus = event_bus
        self._redis = None
        self._pubsub = None
        self._listen_task: Optional[asyncio.Task] = None
        self.instance_id = INSTANCE_ID

    @property
    def enabled(self) -> bool:
        return SSE_REDIS_ENABLED

    async def start(self) -> None:
        if not SSE_REDIS_ENABLED:
            return
        try:
            import redis.asyncio as aioredis
            import uuid

            if not self.instance_id:
                self.instance_id = uuid.uuid4().hex[:12]

            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(CHANNEL)
            self._listen_task = asyncio.create_task(self._listen())
            _logger.info("Redis SSE 桥接已启动 instance=%s", self.instance_id)
        except Exception as e:
            _logger.warning("Redis SSE 桥接启动失败（降级为单实例）: %s", e)
            await self.stop()

    async def stop(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe(CHANNEL)
                await self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
            self._redis = None

    async def start_publisher_only(self) -> None:
        """Worker 进程：仅初始化 Redis 发布端（不订阅，避免 Celery 进程 SSE 丢失）"""
        if not SSE_REDIS_ENABLED:
            return
        if self._redis is not None:
            return
        try:
            import redis.asyncio as aioredis
            import uuid

            if not self.instance_id:
                self.instance_id = uuid.uuid4().hex[:12]

            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await self._redis.ping()
            _logger.info("Redis SSE 发布端已就绪 instance=%s", self.instance_id)
        except Exception as e:
            _logger.warning("Redis SSE 发布端启动失败: %s", e)
            self._redis = None

    async def publish(self, project_id: Optional[str], event: str, data: dict) -> None:
        if not self._redis:
            return
        try:
            msg = json.dumps(
                {
                    "origin": self.instance_id,
                    "project_id": project_id,
                    "event": event,
                    "data": data,
                },
                ensure_ascii=False,
                default=str,
            )
            await self._redis.publish(CHANNEL, msg)
        except Exception as e:
            _logger.debug("Redis SSE publish 失败: %s", e)

    def publish_background(
        self, project_id: Optional[str], event: str, data: dict
    ) -> None:
        if not self._redis:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(project_id, event, data))
        except RuntimeError:
            pass

    async def _listen(self) -> None:
        assert self._pubsub is not None
        async for message in self._pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message["data"])
                if payload.get("origin") == self.instance_id:
                    continue
                self._bus.deliver_local(
                    payload["event"],
                    payload["data"],
                    payload.get("project_id"),
                )
            except Exception as e:
                _logger.debug("Redis SSE 消息处理失败: %s", e)

    async def health(self) -> dict:
        if not SSE_REDIS_ENABLED:
            return {"enabled": False, "status": "disabled"}
        if not self._redis:
            return {"enabled": True, "status": "unavailable"}
        try:
            await self._redis.ping()
            return {
                "enabled": True,
                "status": "connected",
                "instance_id": self.instance_id,
                "channel": CHANNEL,
            }
        except Exception as e:
            return {"enabled": True, "status": "error", "error": str(e)}


_bridge: Optional[RedisSSEBridge] = None


def get_redis_sse_bridge(event_bus: "EventBus") -> RedisSSEBridge:
    global _bridge
    if _bridge is None:
        _bridge = RedisSSEBridge(event_bus)
    return _bridge
