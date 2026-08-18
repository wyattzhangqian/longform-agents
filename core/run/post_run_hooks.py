"""PostRunHookManager — Run 完成后触发的 hook 链

职责：
1. 提取跨 run 记忆（CrossRunMemoryPipeline）
2. 触发 SelfOptimizer 分析（如果存在）

所有 hook 失败时 graceful 降级，不阻断 run 完成流程。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from core.logging import get_logger

_logger = get_logger("post_run_hooks")


class PostRunHookManager:
    """Run 完成后触发的 hook 链"""

    def __init__(self, memory_pipeline=None, optimizer=None):
        """
        Args:
            memory_pipeline: CrossRunMemoryPipeline 实例（可选）
            optimizer: SelfOptimizer 实例（可选）
        """
        self.memory_pipeline = memory_pipeline
        self.optimizer = optimizer

    async def on_run_completed(
        self,
        agent_id: str,
        agent_name: str,
        run_id: str,
        task: str,
        phases_summary: str,
        avg_quality_score: float = 0.0,
        run_context: Optional[Dict[str, Any]] = None,
        quality_events: Optional[List[Dict[str, Any]]] = None,
        human_feedback: Optional[List[str]] = None,
        output_characteristics: str = "",
    ) -> None:
        """Run 完成后触发的 hook 链（异步，不阻塞调用方）

        所有 hook 失败时 graceful 降级。
        """
        # 1. 提取跨 run 记忆
        if self.memory_pipeline:
            try:
                await self.memory_pipeline.extract_from_run(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    run_id=run_id,
                    task=task,
                    phases_summary=phases_summary,
                    avg_quality_score=avg_quality_score,
                    run_context=run_context,
                    quality_events=quality_events,
                    human_feedback=human_feedback,
                    output_characteristics=output_characteristics,
                )
            except Exception as e:
                _logger.warning("记忆提取失败（非致命）: %s", e)

        # 2. 触发 SelfOptimizer 分析（如果存在）
        if self.optimizer:
            try:
                # SelfOptimizer.analyze 需要 success_cases 和 decision_logs
                # 从 run_context 提取可用数据
                cases = self._extract_success_cases(run_context, avg_quality_score)
                logs = self._extract_decision_logs(run_context)
                await self.optimizer.analyze(
                    agent_id=agent_id,
                    success_cases=cases,
                    decision_logs=logs,
                )
            except Exception as e:
                _logger.warning("Self-optimization 失败（非致命）: %s", e)

    def _extract_success_cases(
        self,
        run_context: Optional[Dict[str, Any]],
        quality_score: float,
    ) -> List[Dict[str, Any]]:
        """从 RunContext 提取成功案例"""
        if not run_context:
            return []
        return [{
            "quality_score": quality_score * 10 if quality_score <= 1.0 else quality_score,
            "iterations": len(run_context.get("completed_phase_ids", [])),
            "task": run_context.get("task", ""),
        }]

    def _extract_decision_logs(
        self, run_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """从 RunContext 提取决策日志"""
        if not run_context:
            return []
        # 从 config_snapshot 或 values 中提取决策记录
        values = run_context.get("values", {})
        logs = values.get("decision_logs", [])
        if isinstance(logs, list):
            return logs
        return []

    async def on_run_completed_fire_and_forget(self, **kwargs) -> None:
        """触发后不等待完成（fire-and-forget，强引用跟踪防 GC 回收）"""
        from core.task_tracker import create_task as _tracked_task
        _tracked_task(self.on_run_completed(**kwargs), name="post_run_hooks")
