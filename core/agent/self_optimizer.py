"""Self Optimizer — Agent 自我优化引擎 (AInative v3)

闭合 EvolutionEngine 的进化循环：不只是收集案例，而是分析案例、
生成优化建议、并实际应用到 Agent 的 prompt/策略/配置中。

核心流程：
    1. 数据收集 → EvolutionEngine 记录成功案例和决策日志
    2. 模式分析 → 从案例中提取成功/失败模式
    3. 优化生成 → 生成具体的优化建议（prompt 改进、参数调整、策略更新）
    4. 应用执行 → 将优化应用到 Agent 定义中
    5. A/B 验证 → 对比优化前后效果（可选）

这是 AInative 平台"越用越好"的核心——Agent 从经验中持续学习进化。
"""

from __future__ import annotations
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from pydantic import BaseModel, Field
from core.logging import get_logger

_logger = get_logger("self_optimizer")


# ============================================================
# 数据模型
# ============================================================

class OptimizationSuggestion(BaseModel):
    """优化建议"""
    id: str
    agent_id: str
    type: str = "prompt"                               # prompt | parameter | strategy | tool | memory
    description: str = ""                               # 优化描述
    current_value: str = ""                             # 当前值
    suggested_value: str = ""                           # 建议值
    rationale: str = ""                                 # 优化理由（基于数据）
    confidence: float = 0.5                             # 优化信心 0-1
    expected_improvement: float = 0.0                   # 预期改善幅度
    source_cases: List[str] = Field(default_factory=list)  # 支持优化的案例 ID
    applied: bool = False
    applied_at: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class OptimizationSnapshot(BaseModel):
    """优化快照 — 记录优化前的 Agent 定义，支持回滚"""
    snapshot_id: str
    agent_id: str
    original_prompt: str
    original_temperature: float = 0.7
    original_max_tokens: int = 4096
    applied_suggestions: List[str] = Field(default_factory=list)
    pre_optimization_scores: List[float] = Field(default_factory=list)
    post_optimization_scores: List[float] = Field(default_factory=list)
    is_reverted: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class OptimizationReport(BaseModel):
    """优化报告"""
    agent_id: str
    suggestions: List[OptimizationSuggestion] = Field(default_factory=list)
    total_suggestions: int = 0
    auto_applied: int = 0                               # 自动应用的
    pending_review: int = 0                             # 待用户审核的
    analyzed_cases: int = 0                             # 分析的案例数
    overall_improvement: float = 0.0                    # 整体预期改善
    snapshot_id: Optional[str] = None                   # 关联的快照 ID
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


# ============================================================
# SelfOptimizer — 自我优化引擎
# ============================================================

class SelfOptimizer:
    """自我优化引擎

    分析 Evolution 收集的案例数据，生成优化建议。

    用法：
        optimizer = SelfOptimizer(llm_client=llm_client)
        report = await optimizer.analyze(
            agent_id="analyzer",
            success_cases=cases,
            decision_logs=logs,
        )
        # 自动应用高信心的优化
        await optimizer.auto_apply(report, agent_definition, llm_client)
    """

    # 自动应用阈值
    AUTO_APPLY_THRESHOLD = 0.8     # 信心 > 此值自动应用
    MIN_CASES_FOR_ANALYSIS = 2     # 最少案例数（E1 降低阈值）

    def __init__(self, llm_client=None):
        """初始化自我优化引擎

        Args:
            llm_client: 可选 LLM 客户端（用于深度分析和建议增强）
        """
        self._llm_client = llm_client

    async def analyze(
        self,
        agent_id: str,
        success_cases: Optional[List[Dict[str, Any]]] = None,
        decision_logs: Optional[List[Dict[str, Any]]] = None,
        llm_client=None,            # 可选 LLM 客户端（用于深度分析）
    ) -> OptimizationReport:
        """分析 Agent 的执行数据，生成优化建议

        Args:
            agent_id: Agent ID
            success_cases: Evolution 收集的成功案例
            decision_logs: Evolution 记录的决策日志
            llm_client: LLM 客户端（用于智能分析，不传则用规则分析）

        Returns:
            OptimizationReport 包含所有优化建议
        """
        import uuid

        cases = success_cases or []
        logs = decision_logs or []

        if len(cases) < self.MIN_CASES_FOR_ANALYSIS:
            _logger.info("案例不足，跳过优化分析: agent=%s, cases=%d",
                          agent_id, len(cases))
            return OptimizationReport(agent_id=agent_id, analyzed_cases=len(cases))

        suggestions = []

        # 1. 分析 prompt 优化
        prompt_suggestions = self._analyze_prompt(cases, agent_id)
        suggestions.extend(prompt_suggestions)

        # 2. 分析策略优化
        strategy_suggestions = self._analyze_strategy(logs, agent_id)
        suggestions.extend(strategy_suggestions)

        # 3. 分析参数优化
        param_suggestions = self._analyze_parameters(cases, agent_id)
        suggestions.extend(param_suggestions)

        # 4. 如果有 LLM 客户端，做更深度的分析
        if llm_client and suggestions:
            enhanced = await self._llm_enhance(suggestions, cases, logs, llm_client)
            if enhanced:
                suggestions = enhanced

        # 5. 构建报告
        auto_count = sum(1 for s in suggestions if s.confidence >= self.AUTO_APPLY_THRESHOLD)
        pending_count = len(suggestions) - auto_count
        overall = sum(s.expected_improvement for s in suggestions) / max(len(suggestions), 1)

        report = OptimizationReport(
            agent_id=agent_id,
            suggestions=suggestions,
            total_suggestions=len(suggestions),
            auto_applied=auto_count,
            pending_review=pending_count,
            analyzed_cases=len(cases) + len(logs),
            overall_improvement=overall,
        )

        _logger.info("优化分析完成: agent=%s, suggestions=%d, auto=%d, improvement=%.2f",
                      agent_id, len(suggestions), auto_count, overall)

        return report

    async def auto_apply(
        self,
        report: OptimizationReport,
        agent_definition,
        llm_client=None,
    ) -> List[OptimizationSuggestion]:
        """自动应用高信心的优化建议

        应用前自动创建 OptimizationSnapshot，支持后续回滚。
        """
        import uuid

        # 1. 创建优化前快照
        snapshot = OptimizationSnapshot(
            snapshot_id=f"snap_{uuid.uuid4().hex[:12]}",
            agent_id=report.agent_id,
            original_prompt=agent_definition.system_prompt,
            original_temperature=agent_definition.temperature or 0.7,
            original_max_tokens=agent_definition.max_tokens or 4096,
        )

        # 2. 应用每条优化建议
        applied = []
        for suggestion in report.suggestions:
            if suggestion.confidence < self.AUTO_APPLY_THRESHOLD:
                continue

            success = self._apply_suggestion(suggestion, agent_definition, llm_client)
            if success:
                suggestion.applied = True
                suggestion.applied_at = datetime.now().isoformat()
                snapshot.applied_suggestions.append(suggestion.id)
                applied.append(suggestion)
                _logger.info("已自动应用优化: agent=%s, type=%s",
                              suggestion.agent_id, suggestion.type)

        # 3. 持久化快照
        if applied:
            report.snapshot_id = snapshot.snapshot_id
            await self._save_snapshot(snapshot)

            # 4. 持久化优化后的 agent_definition 回 DB（闭环：分析→应用→写回）
            try:
                from core.agent.registry import get_registry
                registry = get_registry()
                await registry.update(agent_definition.id, agent_definition)
                _logger.info("Agent %s 优化已持久化（%d 条建议）", agent_definition.id, len(applied))
            except Exception as e:
                _logger.warning("Agent 优化持久化失败（非致命，快照已保存可回滚）: %s", e)

        # === E4 新增：写入 evolution_history（进化轨迹可见）===
        if applied:
            try:
                from core.agent.types import EvolutionEvent
                event = EvolutionEvent(
                    version=f"opt_v{len(agent_definition.evolution_history) + 1}",
                    changes="; ".join(s.description for s in applied if s.description)[:200],
                )
                agent_definition.evolution_history.append(event)
                from core.agent.registry import get_registry
                await get_registry().update(agent_definition.id, agent_definition)
            except Exception as e:
                _logger.warning("Failed to persist evolution_history: %s", e)

        return applied

    @staticmethod
    async def _save_snapshot(snapshot: OptimizationSnapshot):
        """将快照持久化到数据库"""
        import json
        try:
            from core.storage.database import get_db
            db = await get_db()
            await db.execute(
                """INSERT OR REPLACE INTO optimization_snapshots
                   (snapshot_id, agent_id, original_prompt, original_temperature,
                    original_max_tokens, applied_suggestions, pre_optimization_scores,
                    post_optimization_scores, is_reverted, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.snapshot_id, snapshot.agent_id, snapshot.original_prompt,
                    snapshot.original_temperature, snapshot.original_max_tokens,
                    json.dumps(snapshot.applied_suggestions),
                    json.dumps(snapshot.pre_optimization_scores),
                    json.dumps(snapshot.post_optimization_scores),
                    int(snapshot.is_reverted), snapshot.created_at,
                ),
            )
            await db.commit()
            _logger.info("优化快照已保存: snapshot=%s, agent=%s",
                          snapshot.snapshot_id, snapshot.agent_id)
        except Exception as e:
            _logger.warning("快照持久化失败（非致命）: %s", e)

    @staticmethod
    async def rollback(snapshot_id: str, agent_definition) -> bool:
        """回滚到优化前状态"""
        import json
        try:
            from core.storage.database import get_db
            db = await get_db()
            cursor = await db.execute(
                "SELECT * FROM optimization_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            row = await cursor.fetchone()
            if not row:
                _logger.warning("快照不存在: %s", snapshot_id)
                return False

            r = dict(row)
            agent_definition.system_prompt = r["original_prompt"]
            agent_definition.temperature = r["original_temperature"]
            agent_definition.max_tokens = r["original_max_tokens"]

            # 标记快照为已回滚
            await db.execute(
                "UPDATE optimization_snapshots SET is_reverted = 1 WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            await db.commit()
            _logger.info("已回滚到快照: snapshot=%s, agent=%s",
                          snapshot_id, agent_definition.id)
            return True
        except Exception as e:
            _logger.warning("回滚失败: %s", e)
            return False

    # ==================== 分析器 ====================

    @staticmethod
    def _analyze_prompt(cases: List[dict], agent_id: str) -> List[OptimizationSuggestion]:
        """分析 prompt 优化空间"""
        import uuid

        suggestions = []
        high_quality = [c for c in cases if c.get("quality_score", 0) >= 7.0]
        low_quality = [c for c in cases if c.get("quality_score", 0) < 5.0]

        if high_quality and low_quality:
            # 对比高质量和低质量案例的 prompt 差异
            hq_prompts = [c.get("prompt_used", "") for c in high_quality if c.get("prompt_used")]
            lq_prompts = [c.get("prompt_used", "") for c in low_quality if c.get("prompt_used")]

            if hq_prompts and lq_prompts:
                # 高质量 prompt 通常更长更具体
                avg_hq_len = sum(len(p) for p in hq_prompts) / len(hq_prompts)
                avg_lq_len = sum(len(p) for p in lq_prompts) / len(lq_prompts)

                if avg_hq_len > avg_lq_len * 1.2:
                    suggestions.append(OptimizationSuggestion(
                        id=f"opt_{uuid.uuid4().hex[:6]}",
                        agent_id=agent_id,
                        type="prompt",
                        description="增加 prompt 详细度和具体指令",
                        current_value=f"平均 {int(avg_lq_len)} 字符",
                        suggested_value=f"建议扩展到 {int(avg_hq_len)} 字符以上",
                        rationale=f"高质量案例的 prompt 平均长 {int(avg_hq_len - avg_lq_len)} 字符",
                        confidence=0.75,
                        expected_improvement=0.15,
                    ))

        # 迭代次数分析
        avg_iterations = sum(c.get("iterations", 1) for c in cases) / max(len(cases), 1)
        if avg_iterations > 3:
            suggestions.append(OptimizationSuggestion(
                id=f"opt_{uuid.uuid4().hex[:6]}",
                agent_id=agent_id,
                type="parameter",
                description="优化 Agent 推理策略以减少迭代",
                current_value=f"平均 {avg_iterations:.1f} 轮",
                suggested_value="建议优化 system prompt 中的推理指令",
                rationale="迭代次数过多，可能说明 prompt 指令不够清晰",
                confidence=min(0.9, avg_iterations / 10),
                expected_improvement=0.1,
            ))

        return suggestions

    @staticmethod
    def _analyze_strategy(logs: List[dict], agent_id: str) -> List[OptimizationSuggestion]:
        """分析策略优化空间"""
        import uuid

        suggestions = []
        if not logs:
            return suggestions

        successful = sum(1 for l in logs if l.get("was_successful", False))
        total = len(logs)
        success_rate = successful / total if total > 0 else 0

        # 决策类型分布
        type_stats: Dict[str, Tuple[int, int]] = {}
        for log in logs:
            dt = log.get("decision_type", "unknown")
            s, f = type_stats.get(dt, (0, 0))
            if log.get("was_successful"):
                type_stats[dt] = (s + 1, f)
            else:
                type_stats[dt] = (s, f + 1)

        # 找出失败率高的决策类型
        for dt, (s, f) in type_stats.items():
            total_dt = s + f
            if total_dt >= 2 and f / total_dt > 0.5:
                suggestions.append(OptimizationSuggestion(
                    id=f"opt_{uuid.uuid4().hex[:6]}",
                    agent_id=agent_id,
                    type="strategy",
                    description=f"改进 {dt} 类型决策策略",
                    current_value=f"成功率 {s}/{total_dt}",
                    suggested_value=f"需要重新评估 {dt} 决策逻辑",
                    rationale=f"此类决策失败率 {f/total_dt:.0%}，建议优化决策依据",
                    confidence=min(0.9, f / total_dt),
                    expected_improvement=0.2,
                ))

        return suggestions

    @staticmethod
    def _analyze_parameters(cases: List[dict], agent_id: str) -> List[OptimizationSuggestion]:
        """分析参数优化空间"""
        import uuid

        suggestions = []
        if not cases:
            return suggestions

        # 执行时长分析
        durations = [c.get("duration_seconds", 0) for c in cases if c.get("duration_seconds", 0) > 0]
        if durations:
            avg_duration = sum(durations) / len(durations)
            if avg_duration > 60:
                suggestions.append(OptimizationSuggestion(
                    id=f"opt_{uuid.uuid4().hex[:6]}",
                    agent_id=agent_id,
                    type="parameter",
                    description="考虑优化执行效率",
                    current_value=f"平均 {avg_duration:.0f}s",
                    suggested_value="检查是否可以减少不必要的 LLM 调用或工具查询",
                    rationale="执行时间偏长，可能存在优化空间",
                    confidence=0.6,
                    expected_improvement=0.05,
                ))

        return suggestions

    @staticmethod
    async def _llm_enhance(
        suggestions: List[OptimizationSuggestion],
        cases: List[dict],
        logs: List[dict],
        llm_client,
    ) -> Optional[List[OptimizationSuggestion]]:
        """使用 LLM 增强优化建议质量"""
        try:
            import json
            import uuid as _uuid

            cases_summary = json.dumps([
                {"quality": c.get("quality_score", 0), "iterations": c.get("iterations", 1)}
                for c in cases[-5:]
            ], ensure_ascii=False)

            prompt = f"""基于以下 Agent 执行数据，评估和完善优化建议。

已有建议：
{json.dumps([s.model_dump() for s in suggestions[:3]], ensure_ascii=False, default=str)}

案例摘要：{cases_summary}

请以 JSON 格式返回完善后的建议列表（保持原有 id，只更新 description 和 rationale 使其更具体）。"""

            response = await llm_client.chat(prompt)
            # 尝试解析 LLM 输出
            json_str = response.strip()
            if "```" in json_str:
                json_str = json_str.split("```")[1]
            try:
                enhanced_data = json.loads(json_str)
                for i, sd in enumerate(enhanced_data):
                    if i < len(suggestions):
                        suggestions[i].description = sd.get("description", suggestions[i].description)
                        suggestions[i].rationale = sd.get("rationale", suggestions[i].rationale)
                        suggestions[i].confidence = sd.get("confidence", suggestions[i].confidence)
            except (json.JSONDecodeError, KeyError):
                pass

            return suggestions
        except Exception as e:
            _logger.debug("LLM 优化增强失败: %s", e)
            return suggestions

    # ==================== 应用 ====================

    @staticmethod
    def _apply_suggestion(
        suggestion: OptimizationSuggestion,
        agent_definition,
        llm_client=None,
    ) -> bool:
        """应用单条优化建议到 AgentDefinition"""
        try:
            if suggestion.type == "prompt":
                # [修复] 不再 append 到 system_prompt，改为存入 extra["optimization_hints"]
                # ContextBuilder 运行时按 token 预算选择性注入
                extra = agent_definition.extra or {}
                hints: list = extra.get("optimization_hints", [])
                # 最多保留 5 条最新优化，超出则淘汰最旧的
                MAX_HINTS = 5
                new_hint = {
                    "id": suggestion.id,
                    "text": suggestion.suggested_value,
                    "description": suggestion.description,
                    "confidence": suggestion.confidence,
                    "applied_at": datetime.now().isoformat(),
                }
                hints.append(new_hint)
                if len(hints) > MAX_HINTS:
                    hints = sorted(hints, key=lambda h: h.get("confidence", 0), reverse=True)[:MAX_HINTS]
                extra["optimization_hints"] = hints
                agent_definition.extra = extra

            elif suggestion.type == "parameter":
                if "temperature" in suggestion.description.lower():
                    current = agent_definition.temperature or 0.7
                    agent_definition.temperature = round(max(0.0, min(2.0, current - 0.05)), 2)

            elif suggestion.type == "strategy":
                extra = agent_definition.extra or {}
                hints = extra.get("strategy_hints", [])
                hint = f"{suggestion.description}: {suggestion.suggested_value}"
                if hint not in hints:
                    hints.append(hint)
                    if len(hints) > 5:
                        hints = hints[-5:]
                extra["strategy_hints"] = hints
                agent_definition.extra = extra

            return True
        except Exception as e:
            _logger.warning("应用优化建议失败: %s", e)
            return False
