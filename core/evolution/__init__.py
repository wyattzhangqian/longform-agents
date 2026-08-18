"""Evolution System — 成功案例自动收集 + 决策日志 + 进化触发

在编排生命周期中自动记录高质量产出和关键决策，
为后续的 Agent 自动进化提供数据基础。
"""

from __future__ import annotations
import json
import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime
from core.storage.database import get_db
from .collector import SuccessCase, DecisionLog


class EvolutionEngine:
    """进化引擎 — 成功案例收集 + 决策日志 + 进化触发

    用法：
        evolution = EvolutionEngine()

        # 记录成功案例
        await evolution.log_success("scriptwriter", result, quality_score=8.5)

        # 记录关键决策
        await evolution.log_decision("director", "采用暖色调风格", {...})

        # 检查是否触发进化
        if await evolution.should_evolve("scriptwriter"):
            # 触发 Agent 进化流程
            ...
    """

    def __init__(self):
        self._success_threshold: float = 7.0   # 质量分阈值（超过才收集）
        self._evolution_cooldown: int = 5      # E1: 进化冷却（最近 N 条案例中评估）
        self._project_id: str = ""

    def set_project(self, project_id: str):
        """设置当前项目 ID"""
        self._project_id = project_id

    async def log_execution(
        self,
        agent_id: str,
        result: Any,
        quality_score: float = 0,
        iterations: int = 1,
        duration_seconds: float = 0,
        project_id: str = "",
    ) -> Optional[SuccessCase]:
        """E2: 记录每次执行（不过滤分数，供 should_evolve 全量评估）"""
        case = SuccessCase(
            case_id=f"case_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            project_id=project_id or self._project_id,
            domain_id="",
            result_summary=self._summarize(result),
            quality_score=quality_score,
            iterations=iterations,
            duration_seconds=duration_seconds,
            prompt_used="",
        )
        return await self._persist_case(case)

    async def log_success(
        self,
        agent_id: str,
        result: Any,
        quality_score: float = 0,
        iterations: int = 1,
        duration_seconds: float = 0,
        prompt_used: str = "",
        domain_id: str = "",
        project_id: str = "",
    ) -> Optional[SuccessCase]:
        """记录成功案例

        只收集高质量产出（quality_score >= threshold 或 无评分默认收集）
        """
        if quality_score > 0 and quality_score < self._success_threshold:
            return None

        case = SuccessCase(
            case_id=f"case_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            project_id=project_id or self._project_id,
            domain_id=domain_id,
            result_summary=self._summarize(result),
            quality_score=quality_score,
            iterations=iterations,
            duration_seconds=duration_seconds,
            prompt_used=prompt_used,
        )

        return await self._persist_case(case)

    async def log_decision(
        self,
        agent_id: str,
        outcome: str,
        context: dict = None,
        decision_type: str = "strategy",
        was_successful: bool = True,
    ) -> DecisionLog:
        """记录关键决策"""
        log_entry = DecisionLog(
            log_id=f"dlog_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            project_id=self._project_id,
            decision_type=decision_type,
            context=context or {},
            outcome=outcome,
            was_successful=was_successful,
        )

        db = await get_db()
        await db.execute(
            """INSERT INTO evolution_logs
               (log_id, agent_id, project_id, decision_type, context, outcome, was_successful)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                log_entry.log_id, log_entry.agent_id, log_entry.project_id,
                log_entry.decision_type, json.dumps(log_entry.context),
                log_entry.outcome, int(log_entry.was_successful),
            ),
        )
        await db.commit()
        return log_entry

    async def should_evolve(self, agent_id: str) -> bool:
        """检查 Agent 是否应该进化

        条件：最近 N 个案例中高质量占比 > 50%
        """
        db = await get_db()
        # 修复：子查询先取最近 N 条，再聚合（COUNT(*)+LIMIT 对单行无效）
        cursor = await db.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN quality_score >= ? THEN 1 ELSE 0 END) as high_quality
               FROM (
                   SELECT quality_score FROM evolution_cases
                   WHERE agent_id = ?
                   ORDER BY created_at DESC LIMIT ?
               )""",
            (self._success_threshold, agent_id, self._evolution_cooldown),
        )
        row = await cursor.fetchone()
        if not row or row["total"] < 3:
            return False

        ratio = row["high_quality"] / row["total"] if row["total"] > 0 else 0
        return ratio > 0.5

    async def log_failure(
        self,
        agent_id: str,
        phase_id: str = "",
        reason: str = "",
    ) -> Optional[SuccessCase]:
        """记录失败案例（quality_score=-1 标记，供 SelfOptimizer 分析失败模式）"""
        case = SuccessCase(
            case_id=f"case_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            project_id=self._project_id,
            domain_id=phase_id,
            result_summary={"failure_reason": reason or "未知错误", "phase_id": phase_id},
            quality_score=-1,
            iterations=0,
            duration_seconds=0,
            prompt_used="",
        )
        return await self._persist_case(case)

    async def _persist_case(self, case: SuccessCase) -> SuccessCase:
        """持久化一条 evolution 案例（log_success / log_failure 共用）"""
        db = await get_db()
        await db.execute(
            """INSERT INTO evolution_cases
               (case_id, agent_id, project_id, domain_id, result_summary,
                quality_score, iterations, duration_seconds, prompt_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case.case_id, case.agent_id, case.project_id, case.domain_id,
                json.dumps(case.result_summary), case.quality_score,
                case.iterations, case.duration_seconds, case.prompt_used,
            ),
        )
        await db.commit()
        return case

    async def get_success_cases(
        self, agent_id: str = None, limit: int = 20
    ) -> List[SuccessCase]:
        """获取成功案例"""
        db = await get_db()
        sql = "SELECT * FROM evolution_cases"
        params: list = []

        if agent_id:
            sql += " WHERE agent_id = ?"
            params.append(agent_id)

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await db.execute(sql, params)
        cases = []
        for r in await cursor.fetchall():
            row = dict(r)
            cases.append(SuccessCase(
                case_id=row["case_id"],
                agent_id=row["agent_id"],
                project_id=row.get("project_id", ""),
                domain_id=row.get("domain_id", ""),
                result_summary=json.loads(row.get("result_summary", "{}")),
                quality_score=row.get("quality_score", 0),
                iterations=row.get("iterations", 1),
                duration_seconds=row.get("duration_seconds", 0),
                prompt_used=row.get("prompt_used", ""),
                created_at=row.get("created_at", ""),
            ))
        return cases

    @staticmethod
    def _summarize(result: Any) -> dict:
        """生成结果摘要"""
        if isinstance(result, dict):
            return {
                k: (f"[{len(v)} items]" if isinstance(v, list)
                    else f"{{...{len(v)} fields}}" if isinstance(v, dict)
                    else str(v)[:100])
                for k, v in result.items()
                if not k.startswith("_")
            }
        elif isinstance(result, str):
            return {"text": result[:200]}
        return {"raw": str(result)[:200]}

    async def get_decision_logs(
        self, agent_id: str = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """获取决策日志"""
        db = await get_db()
        sql = "SELECT * FROM evolution_logs"
        params: list = []

        if agent_id:
            sql += " WHERE agent_id = ?"
            params.append(agent_id)

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await db.execute(sql, params)
        logs = []
        for r in await cursor.fetchall():
            row = dict(r)
            logs.append({
                "log_id": row["log_id"],
                "agent_id": row["agent_id"],
                "project_id": row.get("project_id", ""),
                "decision_type": row.get("decision_type", ""),
                "context": json.loads(row.get("context", "{}")),
                "outcome": row.get("outcome", ""),
                "was_successful": bool(row.get("was_successful", 1)),
                "created_at": row.get("created_at", ""),
            })
        return logs


async def get_evolution_data(agent_id: str):  # returns Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]
    """获取 Agent 的进化数据（供 API /api/agents/{id}/optimization/suggestions 使用）

    返回成功案例数、失败案例数、最近案例和优化建议。
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN quality_score >= 0 THEN 1 ELSE 0 END) as successes, "
        "SUM(CASE WHEN quality_score < 0 THEN 1 ELSE 0 END) as failures "
        "FROM evolution_cases WHERE agent_id = ?",
        (agent_id,),
    )
    row = await cursor.fetchone()
    stats = {"total": 0, "successes": 0, "failures": 0}
    if row:
        stats = {
            "total": row["total"] or 0,
            "successes": row["successes"] or 0,
            "failures": row["failures"] or 0,
        }

    # 获取最近案例
    cursor = await db.execute(
        "SELECT * FROM evolution_cases WHERE agent_id = ? "
        "ORDER BY created_at DESC LIMIT 10",
        (agent_id,),
    )
    recent_cases = []
    for r in await cursor.fetchall():
        d = dict(r)
        d["result_summary"] = json.loads(d.get("result_summary", "{}"))
        recent_cases.append(d)

    # 获取决策日志
    cursor = await db.execute(
        "SELECT * FROM evolution_logs WHERE agent_id = ? "
        "ORDER BY created_at DESC LIMIT 20",
        (agent_id,),
    )
    decision_logs = []
    for r in await cursor.fetchall():
        d = dict(r)
        d["context"] = json.loads(d.get("context", "{}"))
        decision_logs.append(d)

    # 过滤成功案例（排除 quality_score=-1 的失败标记），供 SelfOptimizer 分析
    success_cases = [c for c in recent_cases if c.get("quality_score", 0) >= 0]

    return success_cases, decision_logs
