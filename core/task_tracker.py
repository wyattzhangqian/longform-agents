"""后台 asyncio 任务的强引用跟踪 — 防止孤儿任务被 GC 回收。

问题背景（2026-07-30 定位）：
    `asyncio.create_task(coro)` 若**不持有返回的 task 引用**，事件循环仅对其持
    弱引用。长任务（如工作流 execute，需跑数分钟）会在任意 GC 时刻被回收，
    收到 CancelledError。若上层 `except asyncio.CancelledError: pass` 静默吞掉，
    任务中止但无任何错误日志、状态不落库——表现为"项目跑到一半莫名其妙挂了"。

    Python 官方文档明确警告：
    "Save a reference to the result of this function, to avoid a task disappearing
    mid-execution. The event loop only keeps weak references to tasks."

解决：用全局集合强引用每个 fire-and-forget 任务，完成时自动移除。
用法：将裸 `asyncio.create_task(coro)` 替换为 `create_task(coro)`。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Coroutine, Any, Set

_logger = logging.getLogger(__name__)

# 全局强引用集合：持有所有未完成的后台任务，防止 GC 回收
_TRACKED_TASKS: "Set[asyncio.Task]" = set()


def create_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """创建被强引用跟踪的后台任务，防止 GC 在执行中途回收孤儿任务。

    Args:
        coro: 协程
        name: 可选任务名（便于日志/调试）

    Returns:
        被跟踪的 asyncio.Task（完成时自动从集合移除）
    """
    task = asyncio.create_task(coro, name=name)
    _TRACKED_TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task) -> None:
    """任务完成回调：移出跟踪集合；若因异常结束（非取消）记录日志。"""
    _TRACKED_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.error("后台任务异常结束 (task=%s): %s", task.get_name(), exc)


def tracked_count() -> int:
    """当前被跟踪的未完成任务数（监控/调试用的可观测性）。"""
    return len(_TRACKED_TASKS)
