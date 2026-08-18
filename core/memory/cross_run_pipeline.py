"""CrossRunMemoryPipeline — 从已完成 run 中提取跨 run 记忆

使用 LLM 分析已完成 run 的人工反馈、质量修复记录、输出特征，
提取可复用的记忆（用户偏好、质量模式、领域知识、工作流洞察）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.memory.models import MemoryEntry, MemoryType
from core.memory.memory_store import CrossRunMemoryStore
from core.logging import get_logger

_logger = get_logger("memory.pipeline")

EXTRACTION_PROMPT = """分析以下已完成的工作流运行，提取可复用的记忆。

## 运行摘要
- Agent: {agent_name}
- 任务: {task_description}
- 完成阶段: {phases_summary}
- 质量评分: {avg_quality_score}
- 人工干预次数: {intervention_count}

## 人工反馈:
{human_feedback}

## 质量违规与修复:
{quality_fixes}

## 最终输出特征:
{output_characteristics}

请提取以下类别的记忆:
1. user_preference — 人工反馈揭示的风格/格式/语气偏好
2. quality_pattern — 反复出现的质量问题及其成功修复
3. domain_knowledge — 执行中学到的领域事实
4. workflow_insight — 流程改进（如"用户倾向于在阶段X后审查"）

返回 JSON 数组: [{{"type": "...", "content": "...", "confidence": 0.0-1.0}}]
只包含高信号记忆（confidence > 0.6）。每次最多 5 条。
"""


class CrossRunMemoryPipeline:
    """跨 Run 记忆提取管道"""

    MIN_CONFIDENCE = 0.6
    MAX_ENTRIES_PER_RUN = 5

    def __init__(self, llm_client=None):
        """
        Args:
            llm_client: LLMClient 实例（可选，缺失时降级为规则提取）
        """
        self.llm = llm_client

    async def extract_from_run(
        self,
        agent_id: str,
        agent_name: str,
        run_id: str,
        task: str,
        phases_summary: str,
        avg_quality_score: float,
        run_context: Optional[Dict[str, Any]] = None,
        quality_events: Optional[List[Dict[str, Any]]] = None,
        human_feedback: Optional[List[str]] = None,
        output_characteristics: str = "",
    ) -> List[MemoryEntry]:
        """从已完成 run 中提取记忆

        Args:
            agent_id: Agent ID
            agent_name: Agent 名称
            run_id: Run ID
            task: 任务描述
            phases_summary: 完成阶段摘要
            avg_quality_score: 平均质量分
            run_context: RunContext 快照（可选）
            quality_events: 质量事件列表
            human_feedback: 人工反馈列表
            output_characteristics: 输出特征描述

        Returns:
            提取的记忆列表
        """
        feedback_list = human_feedback or []
        fixes_list = self._collect_quality_fixes(quality_events or [])

        if not feedback_list and not fixes_list and not output_characteristics:
            _logger.info("无足够信号提取记忆: agent=%s, run=%s", agent_id, run_id)
            return []

        prompt = EXTRACTION_PROMPT.format(
            agent_name=agent_name,
            task_description=task[:500],
            phases_summary=phases_summary,
            avg_quality_score=avg_quality_score,
            intervention_count=len(feedback_list),
            human_feedback="\n".join(feedback_list) or "无",
            quality_fixes="\n".join(fixes_list) or "无",
            output_characteristics=output_characteristics or "未分析",
        )

        raw_memories: List[Dict[str, Any]] = []

        if self.llm:
            try:
                response = await self.llm.chat(prompt, temperature=0.2)
                raw_memories = self._parse_llm_response(response)
            except Exception as e:
                _logger.warning("LLM 提取记忆失败，降级为规则提取: %s", e)
                raw_memories = self._rule_based_extract(
                    feedback_list, fixes_list, output_characteristics
                )
        else:
            raw_memories = self._rule_based_extract(
                feedback_list, fixes_list, output_characteristics
            )

        # 过滤低置信度 + 限制数量
        entries: List[MemoryEntry] = []
        for mem in raw_memories[: self.MAX_ENTRIES_PER_RUN]:
            confidence = float(mem.get("confidence", 0.5))
            if confidence < self.MIN_CONFIDENCE:
                continue
            try:
                memory_type = MemoryType(mem.get("type", "user_preference"))
            except ValueError:
                memory_type = MemoryType.USER_PREFERENCE

            now = datetime.now().isoformat()
            entry = MemoryEntry(
                id=f"xrm_{uuid.uuid4().hex[:12]}",
                agent_id=agent_id,
                memory_type=memory_type,
                content=mem.get("content", ""),
                source_run_id=run_id,
                source_phase=mem.get("source_phase"),
                confidence=confidence,
                created_at=now,
                last_accessed=now,
            )
            entries.append(entry)

        if entries:
            await CrossRunMemoryStore.add_memories(agent_id, entries)
            _logger.info(
                "提取并存储 %d 条跨 run 记忆: agent=%s, run=%s",
                len(entries), agent_id, run_id,
            )

        return entries

    def _collect_quality_fixes(
        self, quality_events: List[Dict[str, Any]]
    ) -> List[str]:
        """从质量事件中收集修复记录"""
        fixes = []
        for ev in quality_events:
            ev_type = ev.get("type", "")
            if ev_type in ("quality_check", "quality_fix"):
                violations = ev.get("violations", [])
                for v in violations:
                    rule_name = v.get("rule_name", v.get("rule_id", "unknown"))
                    fix_desc = v.get("fix_description", v.get("fix_hint", ""))
                    if fix_desc:
                        fixes.append(f"违规 '{rule_name}' 修复: {fix_desc}")
                # 也检查 report 字段（轮次 19 增强）
                report = ev.get("report")
                if report and isinstance(report, dict):
                    for v in report.get("results", []):
                        if v.get("passed") is False:
                            rule_name = v.get("rule_name", "unknown")
                            fix_hint = v.get("fix_hint", "")
                            if fix_hint:
                                fixes.append(f"违规 '{rule_name}' 建议: {fix_hint}")
        return fixes

    def _parse_llm_response(self, response: str) -> List[Dict[str, Any]]:
        """解析 LLM 返回的 JSON 记忆列表"""
        if not response:
            return []
        # 尝试提取 JSON 块
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
        json_str = match.group(1).strip() if match else response.strip()
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and "memories" in parsed:
                return parsed["memories"]
        except (json.JSONDecodeError, AttributeError):
            pass
        return []

    def _rule_based_extract(
        self,
        feedback: List[str],
        fixes: List[str],
        output_chars: str,
    ) -> List[Dict[str, Any]]:
        """无 LLM 时的降级规则提取"""
        memories: List[Dict[str, Any]] = []

        # 从人工反馈提取用户偏好
        for fb in feedback[:3]:
            memories.append({
                "type": "user_preference",
                "content": fb[:200],
                "confidence": 0.7,
            })

        # 从质量修复提取质量模式
        for fix in fixes[:3]:
            memories.append({
                "type": "quality_pattern",
                "content": fix[:200],
                "confidence": 0.65,
            })

        # 从输出特征提取领域知识
        if output_chars:
            memories.append({
                "type": "domain_knowledge",
                "content": output_chars[:200],
                "confidence": 0.6,
            })

        return memories[: self.MAX_ENTRIES_PER_RUN]
