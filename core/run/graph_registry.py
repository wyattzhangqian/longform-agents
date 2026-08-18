"""CollaborationGraph 内存注册表 — 支持 review/resume 热恢复"""

from __future__ import annotations
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.run.collaboration_graph import CollaborationGraph

_running: dict[str, "CollaborationGraph"] = {}
# 工作流已提交但 Graph 尚未注册（from_request 编译阶段）
_executing: set[str] = set()


def get_collaboration_graph(project_id: str) -> Optional["CollaborationGraph"]:
    return _running.get(project_id)


def mark_execution_started(project_id: str) -> None:
    _executing.add(project_id)


def mark_execution_finished(project_id: str) -> None:
    _executing.discard(project_id)


def is_execution_alive(project_id: str) -> bool:
    """进程内是否有活跃工作流（含启动中的编译阶段）。"""
    return project_id in _executing or project_id in _running


def set_collaboration_graph(project_id: str, graph: "CollaborationGraph") -> None:
    _running[project_id] = graph


def remove_collaboration_graph(project_id: str) -> None:
    _running.pop(project_id, None)


async def ensure_collaboration_graph(project_id: str) -> "CollaborationGraph":
    """获取内存 Graph 或从 RunContext checkpoint 冷恢复 (D1/D2)"""
    from core.run.collaboration_graph import CollaborationGraph

    collab = get_collaboration_graph(project_id)
    if collab:
        return collab
    return await CollaborationGraph.from_checkpoint(project_id)
