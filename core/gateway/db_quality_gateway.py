"""DbQualityGateway — 数据库驱动的质量网关

从 DB 加载对应领域+阶段的规则，使用 checkers.py 注册表执行检查器，
结果写入 SSE 事件和统计表。

执行流程：
1. 从 DB 加载当前 domain + phase 的所有 enabled 规则 + 通用层规则
2. 对 Agent 产出逐条执行检查器
3. 汇总结果为 QualityCheckReport
4. 发射 SSE 事件（含完整 report）
5. 更新统计表（UPSERT）
6. 返回 overall_passed 供 GraphRuntime 决定后续流向
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from core.gateway.domain_registry import DomainRegistry
from core.gateway.checkers import execute_check, execute_check_async
from core.gateway.rules import CheckInput
from core.models.quality import QualityCheckReport, QualityViolation
from core.storage.database import get_db
from core.logging import get_logger

_logger = get_logger("db_quality_gateway")


class DbQualityGateway:
    """数据库驱动的质量网关"""

    @staticmethod
    async def check(
        project_id: str,
        domain_id: str,
        phase_id: str,
        agent_id: str,
        agent_output: Any,
        strictness: str = "standard",
        run_context: Optional[dict] = None,
    ) -> QualityCheckReport:
        """执行质量检查

        Args:
            project_id: 项目 ID
            domain_id: 领域 ID（如 "platform:comic_drama"）
            phase_id: 阶段 ID
            agent_id: Agent ID
            agent_output: Agent 产出
            strictness: relaxed | standard | strict

        Returns:
            QualityCheckReport
        """
        # 1. 加载领域规则 + 通用规则
        rules = await DomainRegistry.list_rules(domain_id, phase_id, enabled_only=True)
        general_rules = await DomainRegistry.list_rules("platform:general", enabled_only=True)
        all_rules = general_rules + rules

        # 2. 构造检查输入
        text_content = ""
        if isinstance(agent_output, str):
            text_content = agent_output
        elif isinstance(agent_output, dict):
            text_content = str(agent_output.get("result", "") or agent_output.get("content", ""))

        # Gap3 修复：长内容项目注入 continuity_state/current_unit/continuity_declaration
        meta: Dict[str, Any] = {"project_id": project_id, "phase_id": phase_id}
        if run_context:
            if run_context.get("continuity_state") is not None:
                meta["continuity_state"] = run_context.get("continuity_state")
            if run_context.get("current_unit") is not None:
                meta["current_unit"] = run_context.get("current_unit")
        # 从 agent_output 解析 continuity_declaration
        decl: Dict[str, Any] = {}
        if isinstance(agent_output, dict):
            result = agent_output.get("result", {})
            if isinstance(result, dict) and "continuity_declaration" in result:
                decl = result.get("continuity_declaration", {})
        if not decl and "```metadata" in text_content:
            try:
                import json as _json
                s = text_content.index("```metadata") + len("```metadata")
                e = text_content.index("```", s)
                decl = _json.loads(text_content[s:e].strip()).get("continuity_declaration", {})
            except Exception:
                pass
        if decl:
            meta["continuity_declaration"] = decl
        check_input = CheckInput(
            agent_id=agent_id,
            result=agent_output,
            text_content=text_content,
            structure_keys=list(agent_output.keys()) if isinstance(agent_output, dict) else [],
            metadata=meta,
        )

        # 3. 逐条执行（2026-08-09 P1：并行化，llm_check 不再串行）
        report = QualityCheckReport()
        report.total_rules = len(all_rules)

        async def _check_one(rule_data: Dict[str, Any]):
            """单条检查：semantic_check 非 strict 跳过（同步 pass）；其余并行执行。"""
            check_type = rule_data["check_type"]
            config = rule_data.get("config", {})
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (json.JSONDecodeError, TypeError):
                    config = {}
            # semantic_check 在非 strict 模式下跳过
            if check_type == "semantic_check" and strictness != "strict":
                return rule_data, None, True  # skipped → 记为 pass
            try:
                violation = await execute_check_async(check_type, config, check_input)
            except Exception as e:
                _logger.warning("检查器异常 rule=%s: %s", rule_data.get("rule_id"), e)
                violation = {"message": f"检查器异常: {e}"}
            return rule_data, violation, False

        results = await asyncio.gather(*[_check_one(r) for r in all_rules])

        for rule_data, violation, skipped in results:
            if skipped:
                report.passed += 1
                report.results.append(QualityViolation(
                    rule_id=rule_data["rule_id"],
                    rule_name=rule_data["name"],
                    severity=rule_data["severity"],
                    passed=True,
                ))
            elif violation is None:
                report.passed += 1
                report.results.append(QualityViolation(
                    rule_id=rule_data["rule_id"],
                    rule_name=rule_data["name"],
                    severity=rule_data["severity"],
                    passed=True,
                ))
            else:
                severity = rule_data["severity"]
                if severity == "error":
                    report.errors += 1
                else:
                    report.warnings += 1

                report.results.append(QualityViolation(
                    rule_id=rule_data["rule_id"],
                    rule_name=rule_data["name"],
                    severity=severity,
                    passed=False,
                    message=violation.get("message", ""),
                    fix_hint=rule_data.get("fix_hint", ""),
                    auto_fix_capable=bool(rule_data.get("auto_fix_capable")),
                ))

        # 计算 overall_passed + 填充 violations 列表
        # Gap3 修复：长内容项目自动追加 continuity_compliance 检查（不依赖 seed rule）
        if run_context and run_context.get("continuity_state") is not None:
            try:
                from core.gateway.checkers import check_continuity_compliance
                v = check_continuity_compliance({}, check_input)
                if v:
                    report.errors += 1
                    report.results.append(QualityViolation(
                        rule_id="continuity_compliance", rule_name="连续性义务检查",
                        severity="error", passed=False, message=v.get("message", ""),
                        fix_hint="", auto_fix_capable=False,
                    ))
                else:
                    report.passed += 1
                    report.results.append(QualityViolation(
                        rule_id="continuity_compliance", rule_name="连续性义务检查",
                        severity="info", passed=True,
                    ))
            except Exception:
                pass

        report.overall_passed = report.errors == 0
        report.violations = [r for r in report.results if not r.passed]

        # P1-9: 不再在此处发射 SSE quality_check —— graph 层 _execute_quality_gate
        # 是唯一发射点（含约束覆盖校验合并后的统一 quality_check 事件）。
        # 修复前此处 + graph 层各发一次，同一质量门禁 SSE 双发。
        # 4. 更新统计
        await DbQualityGateway._update_stats(report, domain_id, project_id)

        _logger.info(
            "质量检查完成: project=%s domain=%s phase=%s rules=%d pass=%d warn=%d err=%d",
            project_id, domain_id, phase_id, report.total_rules,
            report.passed, report.warnings, report.errors,
        )

        return report

    @staticmethod
    async def _update_stats(
        report: QualityCheckReport,
        domain_id: str,
        project_id: str,
    ) -> None:
        """更新规则触发统计（UPSERT）"""
        db = await get_db()
        for result in report.results:
            if not result.passed:
                await db.execute(
                    """INSERT INTO quality_rule_stats
                       (rule_id, domain_id, project_id, trigger_count, last_triggered, last_violation_message)
                       VALUES (?, ?, ?, 1, datetime('now','localtime'), ?)
                       ON CONFLICT(rule_id, domain_id, project_id)
                       DO UPDATE SET trigger_count = trigger_count + 1,
                                     last_triggered = datetime('now','localtime'),
                                     last_violation_message = ?""",
                    (result.rule_id, domain_id, project_id, result.message, result.message),
                )
            else:
                await db.execute(
                    """INSERT INTO quality_rule_stats
                       (rule_id, domain_id, project_id, pass_count)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT(rule_id, domain_id, project_id)
                       DO UPDATE SET pass_count = pass_count + 1""",
                    (result.rule_id, domain_id, project_id),
                )
        await db.commit()
