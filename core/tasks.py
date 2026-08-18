"""Celery 异步任务队列 — 通用核心层

将长时间运行的工作流执行从 HTTP 请求中解耦，
通过 Redis 作为 Broker 实现异步任务分发。

M2：Worker 启动时初始化 DB + Redis SSE 发布端，确保跨进程事件可达 API SSE 客户端。
"""

from __future__ import annotations

import asyncio
from typing import Dict, Any

from celery import Celery
from celery.result import AsyncResult
from celery.signals import worker_ready, worker_shutdown

from config import REDIS_URL, CELERY_CONFIG, POSTGRES_ENABLED, SSE_REDIS_ENABLED
from core.logging import get_logger

logger = get_logger(__name__)

# ============================================================
# Celery App
# ============================================================

celery_app = Celery(
    "agent_platform",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=CELERY_CONFIG.get("soft_time_limit", 1800),
    task_time_limit=CELERY_CONFIG.get("time_limit", 3600),
    result_expires=CELERY_CONFIG.get("result_expires", 86400),
    broker_connection_retry_on_startup=True,
    imports=("core.tasks",),
)


async def _init_worker_runtime() -> None:
    """Worker 进程运行时初始化（DB / Schema / Redis SSE 发布 / APM）"""
    from core.storage.database import init_db

    await init_db()

    if POSTGRES_ENABLED:
        from core.storage.postgres import init_postgres_schema
        try:
            await init_postgres_schema()
        except Exception as e:
            logger.warning("Worker PostgreSQL schema 初始化失败: %s", e)

    if SSE_REDIS_ENABLED:
        from core.events import event_bus
        from core.observability.redis_bridge import get_redis_sse_bridge

        bridge = get_redis_sse_bridge(event_bus)
        await bridge.start_publisher_only()
    elif CELERY_CONFIG.get("enabled"):
        logger.warning(
            "CELERY Worker 已启动但 SSE_REDIS_ENABLED=false — "
            "工作流 SSE 事件无法送达 API 实例，建议同时开启"
        )

    from core.observability.apm import init_apm
    from core.observability.otel import init_otel

    init_apm()
    init_otel()


@worker_ready.connect
def on_worker_ready(**kwargs):
    logger.info("Celery Worker 正在初始化运行时...")
    try:
        asyncio.run(_init_worker_runtime())
        logger.info("Celery Worker 已就绪")
    except Exception as e:
        logger.exception("Celery Worker 初始化失败: %s", e)


@worker_shutdown.connect
def on_worker_shutdown(**kwargs):
    logger.info("Celery Worker 正在关闭...")
    try:
        asyncio.run(_shutdown_worker_runtime())
    except Exception as e:
        logger.warning("Celery Worker 关闭异常（非致命）: %s", e)


async def _shutdown_worker_runtime() -> None:
    from core.observability.redis_bridge import get_redis_sse_bridge
    from core.events import event_bus
    from core.storage.database import close_db

    try:
        await get_redis_sse_bridge(event_bus).stop()
    except Exception as e:
        logger.warning("Redis SSE bridge 关闭失败（非致命）: %s", e)
    await close_db()


# ============================================================
# 任务状态查询
# ============================================================

def get_task_status(task_id: str) -> Dict[str, Any]:
    """获取 Celery 任务状态"""
    result = AsyncResult(task_id, app=celery_app)
    response = {
        "task_id": task_id,
        "status": result.state,
    }
    if result.state == "SUCCESS":
        response["result"] = result.result
    elif result.state == "FAILURE":
        response["error"] = str(result.result)
    elif result.state == "PROGRESS":
        response["progress"] = result.info if result.info else {}

    return response


def revoke_task(task_id: str):
    """取消任务"""
    celery_app.control.revoke(task_id, terminate=True)


# ============================================================
# 健康检查
# ============================================================

async def check_celery_health() -> Dict[str, Any]:
    """检查 Celery 连接状态"""
    try:
        result = celery_app.control.ping(timeout=1.0)
        workers = [w for w in result if w]
        return {
            "celery_ok": len(workers) > 0,
            "workers": len(workers),
            "broker": "connected" if workers else "no_workers",
        }
    except Exception as e:
        return {
            "celery_ok": False,
            "error": str(e),
            "broker": "unreachable",
        }


# ============================================================
# 工作流执行任务
# ============================================================

@celery_app.task(bind=True, name="workflow.run", max_retries=2)
def run_workflow_task(self, payload: dict):
    """Celery 工作流执行任务"""
    import asyncio
    from core.orchestration.runner import execute_workflow

    async def _run():
        return await execute_workflow(payload)

    try:
        return asyncio.run(_run())
    except Exception as exc:
        logger.exception("Celery 工作流失败: %s", exc)
        raise self.retry(exc=exc, countdown=30) from exc
