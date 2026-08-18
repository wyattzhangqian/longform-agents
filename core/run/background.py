"""background_safe_execute — 后台任务安全执行器（P1-8 从 api/routes/run.py 下沉）

通用后台任务包装：确保异常时项目状态 → failed、graph 注册表清理、
SSE 广播失败事件，不静默吞掉失败。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from core.logging import get_logger

_logger = get_logger("run.background")


async def background_safe_execute(
    project_id: str,
    coro_factory: Callable[..., Any],
    *args,
) -> None:
    """后台任务安全执行器

    包装任意异步后台任务，确保异常时：
    1. 项目状态 → failed
    2. graph 注册表清理
    3. SSE 广播失败事件
    """
    from core.project.manager import ProjectManager
    from core.events import event_bus

    try:
        await coro_factory(*args)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _logger.exception("后台任务执行失败 (project={}): {}", project_id, e)
        try:
            await ProjectManager.update_status(project_id, "failed")
            from core.run.graph_registry import remove_collaboration_graph
            remove_collaboration_graph(project_id)
            from core.run import graph_registry as _gr
            _gr.mark_execution_finished(project_id)
            event_bus.broadcast(
                event="agent_state",
                data={"state": "failed", "project_id": project_id, "error": str(e)},
                project_id=project_id,
            )
        except Exception:
            pass
