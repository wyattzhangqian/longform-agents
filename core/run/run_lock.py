"""分布式运行锁（P1 多实例编排）

多实例 / 多进程部署时，同一项目的工作流只允许一个实例执行。
基于共享数据库（SQLite 文件 / PostgreSQL）的抢占式锁：

  - 抢占：INSERT 主键冲突即失败
  - 心跳：持有期间每 HEARTBEAT_INTERVAL 秒刷新 heartbeat_at
  - 接管：持有者心跳超过 STALE_AFTER 秒未更新（实例崩溃），其他实例可接管
  - 释放：DELETE WHERE owner = 本实例（不会误删别人接管后的锁）

单实例部署零额外成本（仅一次 INSERT / DELETE）。
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from core.logging import get_logger

_logger = get_logger("run.run_lock")

# 本进程唯一标识
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

HEARTBEAT_INTERVAL = 10.0   # 心跳间隔（秒）— 更频繁刷新，减少误判
STALE_AFTER = 45.0          # 心跳静默多久视为持有者已死（秒）— 更快回收僵尸锁

_heartbeat_tasks: dict = {}


async def acquire_run_lock(project_id: str) -> bool:
    """尝试获取项目运行锁；成功返回 True 并启动心跳"""
    from core.storage.database import get_db

    db = await get_db()
    now = time.time()
    try:
        await db.execute(
            "INSERT INTO run_locks (project_id, owner, acquired_at, heartbeat_at) VALUES (?, ?, ?, ?)",
            (project_id, INSTANCE_ID, now, now),
        )
        await db.commit()
        _start_heartbeat(project_id)
        return True
    except Exception:
        pass  # 主键冲突 → 已有持有者，尝试接管陈旧锁

    cursor = await db.execute(
        "SELECT owner, heartbeat_at FROM run_locks WHERE project_id = ?",
        (project_id,),
    )
    row = await cursor.fetchone()
    if not row:
        # 持有者刚释放，重试一次
        try:
            await db.execute(
                "INSERT INTO run_locks (project_id, owner, acquired_at, heartbeat_at) VALUES (?, ?, ?, ?)",
                (project_id, INSTANCE_ID, now, now),
            )
            await db.commit()
            _start_heartbeat(project_id)
            return True
        except Exception:
            return False

    owner, heartbeat_at = row["owner"], float(row["heartbeat_at"])
    if owner == INSTANCE_ID:
        return True  # 本实例已持有（重入）

    if now - heartbeat_at < STALE_AFTER:
        return False  # 持有者存活

    # 陈旧锁接管（带 owner 条件，避免并发双接管）
    await db.execute(
        "UPDATE run_locks SET owner = ?, acquired_at = ?, heartbeat_at = ? "
        "WHERE project_id = ? AND owner = ?",
        (INSTANCE_ID, now, now, project_id, owner),
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT owner FROM run_locks WHERE project_id = ?", (project_id,),
    )
    row = await cursor.fetchone()
    if row and row["owner"] == INSTANCE_ID:
        _logger.warning("接管陈旧运行锁: project=%s 原持有者=%s", project_id, owner)
        _start_heartbeat(project_id)
        return True
    return False


async def release_run_lock(project_id: str) -> None:
    """释放锁（仅删除本实例持有的）并停止心跳"""
    _stop_heartbeat(project_id)
    try:
        from core.storage.database import get_db
        db = await get_db()
        await db.execute(
            "DELETE FROM run_locks WHERE project_id = ? AND owner = ?",
            (project_id, INSTANCE_ID),
        )
        await db.commit()
    except Exception as e:
        _logger.warning("释放运行锁失败（将由 stale 接管兜底）: %s", e)


async def get_lock_owner(project_id: str) -> Optional[str]:
    """查询当前锁持有者（无锁或已陈旧返回 None）"""
    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute(
        "SELECT owner, heartbeat_at FROM run_locks WHERE project_id = ?",
        (project_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    if time.time() - float(row["heartbeat_at"]) >= STALE_AFTER:
        return None
    return row["owner"]


def _start_heartbeat(project_id: str) -> None:
    _stop_heartbeat(project_id)

    async def beat():
        from core.storage.database import get_db
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                db = await get_db()
                await db.execute(
                    "UPDATE run_locks SET heartbeat_at = ? WHERE project_id = ? AND owner = ?",
                    (time.time(), project_id, INSTANCE_ID),
                )
                await db.commit()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _logger.warning("运行锁心跳异常: project=%s err=%s", project_id, e)

    try:
        _heartbeat_tasks[project_id] = asyncio.get_running_loop().create_task(beat())
    except RuntimeError:
        pass  # 无事件循环（同步测试场景）


def _stop_heartbeat(project_id: str) -> None:
    task = _heartbeat_tasks.pop(project_id, None)
    if task and not task.done():
        task.cancel()


@asynccontextmanager
async def run_lock(project_id: str):
    """async with run_lock(pid): ... — 获取失败抛 RuntimeError"""
    ok = await acquire_run_lock(project_id)
    if not ok:
        owner = await get_lock_owner(project_id)
        raise RuntimeError(
            f"项目 {project_id} 正在其他实例上运行（持有者: {owner or '未知'}）"
        )
    try:
        yield
    finally:
        await release_run_lock(project_id)
