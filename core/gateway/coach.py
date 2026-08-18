"""QualityCoach — 质量门教练模式

从"门卫"升级为"教练"：
检查 → 不合格 → 生成修复建议 → Agent 重新执行（带 feedback）→ 再检查 → 通过/标记 open
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from core.run.run_context import RunContext

logger = logging.getLogger(__name__)

MAX_FIX_RETRIES = 2

# autofix 内 agent.execute 超时（2026-08-09 P0）：coach agent 是 runtime 临时创建的
# BaseAgent，不走 _execute_agent，预算化超时对它不生效 → 必须显式 wait_for。
# 超时后放行（不卡死质量门），并在观测层标记。
_AUTOFIX_AGENT_TIMEOUT = int(os.getenv("AUTOFIX_AGENT_TIMEOUT_SECONDS", "300"))


class QualityGateResult:
    """质量门结果 — 含修复后的产出（2026-08-10 P0-2：失败语义）

    passed=False 必须伴随 status + failure_code，不能把失败伪装成通过。
    """

    def __init__(
        self,
        passed: bool = True,
        output: Any = None,
        attempts: int = 0,
        warnings: Optional[List[Dict[str, Any]]] = None,
        status: str = "",
        failure_code: str = "",
    ):
        self.passed = passed
        self.output = output
        self.attempts = attempts
        self.warnings = warnings or []
        self.status = status or ("passed" if passed else "failed")
        self.failure_code = failure_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status,
            "failure_code": self.failure_code,
            "attempts": self.attempts,
            "warnings": self.warnings,
        }


def _format_fix_feedback(violations: List[Dict[str, Any]], suggestion: str = "") -> str:
    """将违规格式化为 Agent 可理解的 feedback"""
    lines = []
    for i, v in enumerate(violations, 1):
        lines.append(f"### 问题 {i}")
        lines.append(f"- **级别：** {v.get('severity', 'warning')}")
        lines.append(f"- **规则：** {v.get('rule_id', 'unknown')}")
        lines.append(f"- **描述：** {v.get('message', '')}")
        hint = v.get("fix_hint") or v.get("suggestion") or ""
        if hint:
            lines.append(f"- **修复建议：** {hint}")
        lines.append("")
    if suggestion and not any(v.get("fix_hint") or v.get("suggestion") for v in violations):
        lines.append(f"**总体建议：** {suggestion}")
    return "\n".join(lines)


async def quality_gate_with_autofix(
    agent: Any,
    output: str,
    quality_config: Dict[str, Any],
    run_context: RunContext,
    project_id: str = "",
) -> QualityGateResult:
    """教练模式质量门 — 检查 + 自动修复 + 重试

    Args:
        agent: BaseAgent 实例（用于重新执行）
        output: Agent 原始产出文本
        quality_config: 质量检查配置
        run_context: 运行上下文
        project_id: 项目 ID（SSE 推送用）

    Returns:
        QualityGateResult — 含修复后产出
    """
    from core.gateway import QualityGateway

    gateway = QualityGateway()
    if quality_config.get("rules"):
        for rule_cfg in quality_config["rules"]:
            from core.gateway import QualityRule
            gateway.add_rules([QualityRule(**rule_cfg)])

    for attempt in range(MAX_FIX_RETRIES + 1):
        # 1. 检查
        check_result = gateway.check(output)
        violations = check_result.get("violations", []) if isinstance(check_result, dict) else []

        if not violations:
            return QualityGateResult(passed=True, output=output, attempts=attempt)

        # 2. 最后一次仍不过 → 显式失败（2026-08-10 P0-2：不再标记 open 放行）
        if attempt >= MAX_FIX_RETRIES:
            run_context.add_observation(
                source="quality_coach",
                content=(
                    f"QualityGate: {len(violations)} violations after "
                    f"{MAX_FIX_RETRIES} fix attempts. Not passing."
                ),
                kind="system",
            )
            if run_context is not None:
                run_context.record_degradation(
                    "critical", "quality_gate",
                    f"质量门 {MAX_FIX_RETRIES} 轮 Autofix 仍不通过（{len(violations)} violations）",
                    "节点 FAILED，不放行",
                )
            return QualityGateResult(
                passed=False,
                status="failed",
                failure_code="max_retries_exceeded",
                output=output,
                attempts=attempt,
                warnings=violations,
            )

        # 3. 生成修复建议（QualityAdvisor）
        suggestion = ""
        try:
            from core.gateway.quality_advisor import QualityAdvisor
            advisor = QualityAdvisor(None)
            # 简化版建议：从 violation message 构造
            suggestion = _format_fix_feedback(violations)
        except Exception as e:
            logger.debug("QualityAdvisor 不可用: %s", e)
            suggestion = _format_fix_feedback(violations)

        # 4. 推送 SSE：正在自动修复
        _emit_autofix_event(project_id, attempt + 1, len(violations))

        # 5. Agent 重新执行（带 feedback）
        fix_task = (
            f"你的上一次产出未通过质量检查。请修正以下问题，重新输出完整结果。"
            f"保留原有正确内容，只修复指出的问题。\n\n"
            f"{suggestion}"
        )

        # 通过 agent 重新执行（P0：wait_for 包裹，超时放行不卡死质量门）
        try:
            new_output = await asyncio.wait_for(
                agent.execute(input_data={
                    "task": fix_task,
                    "phase_id": f"quality_autofix_{attempt}",
                }),
                timeout=_AUTOFIX_AGENT_TIMEOUT,
            )
            if isinstance(new_output, dict):
                output = new_output.get("content", new_output.get("text", str(new_output)))
            elif isinstance(new_output, str):
                output = new_output
            else:
                output = str(new_output)
        except asyncio.TimeoutError:
            logger.warning(
                "autofix agent.execute 超时（%ds）attempt=%d，标记失败不放行",
                _AUTOFIX_AGENT_TIMEOUT, attempt,
            )
            if run_context is not None:
                run_context.record_degradation(
                    "critical", "quality_autofix",
                    f"autofix 超时（{_AUTOFIX_AGENT_TIMEOUT}s）",
                    "Autofix 失败，质量门不通过",
                )
            return QualityGateResult(
                passed=False, status="timeout",
                failure_code="autofix_timeout",
                output=output, attempts=attempt,
            )
        except Exception as e:
            logger.warning("质量门自动修复执行失败 (attempt %d): %s", attempt, e)
            if run_context is not None:
                run_context.record_degradation(
                    "critical", "quality_autofix",
                    f"autofix 异常: {e}",
                    "Autofix 失败，质量门不通过",
                )
            return QualityGateResult(
                passed=False, status="failed",
                failure_code="autofix_error",
                output=output, attempts=attempt,
            )

    return QualityGateResult(
        passed=False, status="failed",
        failure_code="max_retries_exceeded",
        output=output, attempts=MAX_FIX_RETRIES,
    )


def _emit_autofix_event(project_id: str, attempt: int, violations_count: int) -> None:
    """推送 autofix SSE 事件"""
    if not project_id:
        return
    try:
        from core.events import event_bus
        event_bus.broadcast(
            event="quality_autofix",
            data={
                "attempt": attempt,
                "violations_count": violations_count,
                "message": f"质量检查未通过，正在自动修复（第 {attempt} 次）...",
            },
            project_id=project_id,
        )
    except Exception:
        pass
