"""Task Decomposer — 任务自主分解 (AInative v3)

Agent 不再只能执行单一任务，而是能将复杂任务自主拆解为子任务，
分配给不同 Agent 或自行逐步执行。

核心流程：
    1. 任务接收 → 分析复杂度和可分解性
    2. 子任务拆解 → 生成 SubTask 列表（含依赖关系和所需能力）
    3. 执行规划 → 拓扑排序生成执行顺序
    4. 分配匹配 → 与 TeamBuilder 联动分配子任务给合适 Agent

这是 AInative 自主协作的核心——Agent 不是被动执行者，而是主动规划者。
"""

from __future__ import annotations
import asyncio
from typing import Optional, List, Dict, Any, Set, Tuple
from datetime import datetime
from pydantic import BaseModel, Field
from core.logging import get_logger

_logger = get_logger("decomposer")


# ============================================================
# 数据模型
# ============================================================

class SubTask(BaseModel):
    """子任务定义"""
    id: str
    title: str = ""
    description: str = ""                              # 详细描述
    required_capabilities: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)  # 依赖的子任务 ID 列表
    estimated_complexity: float = 0.5                  # 估计复杂度 0-1
    assigned_agent: str = ""                           # 分配的 Agent（空=待分配）
    status: str = "pending"                            # pending | running | completed | failed
    result: Optional[Dict[str, Any]] = None            # 执行结果
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # ── 协作策略（空=由后处理推断）──
    dialogue_policy: str = ""      # handoff_only / shared_thread
    on_complete: str = ""          # continue / retry / review
    # ── 阶段契约（Phase 0 统一计划契约，映射到 PhasePlan）──
    objective: str = ""                              # 阶段目标
    expected_inputs: List[str] = Field(default_factory=list)   # 需读取的上游文件
    expected_outputs: List[str] = Field(default_factory=list)  # 需产出的文件
    expected_artifacts: List[str] = Field(default_factory=list)  # 期望产物（默认同 expected_outputs）
    acceptance_criteria: List[str] = Field(default_factory=list)  # 验收标准
    quality_domain_ids: List[str] = Field(default_factory=list)   # 质量领域 ID
    knowledge_packs: List[str] = Field(default_factory=list)      # 知识包
    tool_policy: str = "auto"                        # auto / on_demand / disabled
    tool_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    max_retries: int = 3
    risk_level: str = "medium"
    budget: Dict[str, Any] = Field(default_factory=dict)


class DecompositionPlan(BaseModel):
    """任务分解计划"""
    plan_id: str
    original_task: str
    sub_tasks: List[SubTask] = Field(default_factory=list)
    execution_order: List[List[str]] = Field(default_factory=list)  # 拓扑分层执行
    total_complexity: float = 0.0
    estimated_steps: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    reasoning: str = ""  # 推理模型的思维链（为什么这样分解）


# ============================================================
# TaskDecomposer — 任务分解引擎
# ============================================================

class TaskDecomposer:
    """任务分解引擎

    将复杂任务分解为可执行的子任务图。

    用法：
        decomposer = TaskDecomposer()
        plan = await decomposer.decompose(
            task="分析用户行为数据并生成可视化报告",
            available_capabilities=["data_analysis", "visualization", "report_writing"],
        )
        # plan.execution_order → [["analyze"], ["visualize"], ["report"]]
    """

    MAX_DEPTH = 5                  # 最大嵌套深度
    MAX_SUB_TASKS = 20             # 最大子任务数

    # 任务类型 → 默认阶段骨架（Phase 1 自适应规划范式；仅通用先验，领域差异来自 DomainPrior）
    TASK_TYPE_SKELETONS: Dict[str, List[str]] = {
        "code": ["需求", "方案", "实现", "测试", "修复", "验证"],
        "research": ["问题定义", "检索", "交叉验证", "综合", "引用审查"],
        "analysis": ["数据准备", "清洗", "分析", "验证", "洞察", "输出"],
        "creative": ["受众/目标", "概念", "大纲", "生产", "评审", "修订"],
        "long_content": ["总纲", "单元计划", "生产", "连贯性", "汇编"],
        "ops": ["现状", "风险", "变更", "验证", "回滚"],
    }
    _DEFAULT_SKELETON = ["调研/分析", "构思/设计", "初稿创作", "润色完善", "检查输出"]

    @classmethod
    def skeleton_for_task_type(cls, task_type: str) -> List[str]:
        """返回任务类型对应的阶段骨架（未知类型走通用骨架）。纯函数，可单测。"""
        return cls.TASK_TYPE_SKELETONS.get(task_type) or cls._DEFAULT_SKELETON

    async def decompose(
        self,
        task: str,
        available_capabilities: List[str] = None,
        max_sub_tasks: int = None,
        llm_client=None,           # 可选 LLM 客户端（用于智能拆解）
        task_type: str = "",       # Phase 1：任务类型，决定阶段骨架
        domain_prior_text: str = "",  # Phase 1：领域先验文本（DomainPrior.to_prompt_context）
    ) -> DecompositionPlan:
        """分解任务

        Args:
            task: 任务描述
            available_capabilities: 可用的 Agent 能力池
            max_sub_tasks: 最大子任务数
            llm_client: LLM 客户端（用于智能拆解，不传则用规则拆解）
            task_type: 任务类型（code/research/analysis/creative/long_content/ops）
            domain_prior_text: 领域先验文本（优先于通用骨架）

        Returns:
            DecompositionPlan 分解计划
        """
        import uuid

        max_st = max_sub_tasks or self.MAX_SUB_TASKS
        capabilities = available_capabilities or []
        reasoning = ""

        _logger.info("任务分解: %s, capabilities=%s, task_type=%s", task[:80], capabilities, task_type)

        # 1. 智能分析（优先用 LLM）
        if llm_client:
            sub_tasks, reasoning = await self._llm_decompose(
                task, capabilities, llm_client, max_st, task_type=task_type, domain_prior_text=domain_prior_text,
            )
        else:
            sub_tasks = self._rule_decompose(task, capabilities, max_st)

        # 2. 拓扑排序
        execution_order = self._topological_sort(sub_tasks)

        # [新增] 智能推断协作策略
        sub_tasks = infer_collaboration_strategy(sub_tasks)

        # 3. 构建计划
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        total_complexity = sum(t.estimated_complexity for t in sub_tasks) / max(len(sub_tasks), 1)

        plan = DecompositionPlan(
            plan_id=plan_id,
            original_task=task,
            sub_tasks=sub_tasks,
            execution_order=execution_order,
            total_complexity=total_complexity,
            estimated_steps=len(execution_order),
            reasoning=reasoning,
        )

        _logger.info("分解完成: plan=%s, sub_tasks=%d, steps=%d",
                      plan_id, len(sub_tasks), len(execution_order))

        return plan

    # ==================== LLM 智能分解 ====================

    async def _llm_decompose(
        self,
        task: str,
        capabilities: List[str],
        llm_client,
        max_tasks: int,
        task_type: str = "",
        domain_prior_text: str = "",
    ) -> Tuple[List[SubTask], str]:
        """使用 LLM 智能分解任务，返回 (子任务列表, 推理思维链)

        失败时重试一次（降 temperature），再失败才回退规则分解。
        """
        prompt = self._build_decompose_prompt(
            task, capabilities, max_tasks,
            task_type=task_type, domain_prior_text=domain_prior_text,
        )

        reasoning = ""
        last_error = None
        for attempt in range(2):
            try:
                messages = [{"role": "user", "content": prompt}]
                if hasattr(llm_client, 'chat_messages_with_reasoning'):
                    response, reasoning = await llm_client.chat_messages_with_reasoning(messages)
                else:
                    response = await llm_client.chat(prompt)

                # LLM 返回错误标记字符串时直接当作解析失败
                if isinstance(response, str) and response.startswith("[LLM"):
                    raise ValueError(f"LLM 返回错误: {response[:200]}")

                return self._parse_decompose_response(task, response, capabilities, max_tasks, reasoning), reasoning

            except Exception as e:
                last_error = e
                if attempt == 0:
                    _logger.warning(
                        "LLM 分解失败(attempt 1/2): %s — 重试中...", e
                    )
                    # 重试：缩短 prompt 降低复杂度
                    prompt = (
                        f"将任务分解为 {max_tasks} 个子任务，返回 JSON 数组。"
                        f"每项含 title/description/required_capabilities/dependencies。\n"
                        f"可用能力: {', '.join(capabilities[:20])}\n"
                        f"任务: {task}"
                    )
                    await asyncio.sleep(0.5)
                else:
                    _logger.warning(
                        "LLM 分解失败(attempt 2/2): %s — 回退规则分解", e
                    )

        return self._rule_decompose(task, capabilities, max_tasks), ""

    def _build_decompose_prompt(
        self,
        task: str,
        capabilities: List[str],
        max_tasks: int,
        task_type: str = "",
        domain_prior_text: str = "",
    ) -> str:
        """构建任务分解 prompt（提取出来供流式调用复用）

        task_type: 任务类型（code/research/analysis/creative/long_content/ops），
                   决定阶段骨架（Phase 1 自适应规划范式）。
        domain_prior_text: 领域先验文本（DomainPrior.to_prompt_context），
                   优先于通用骨架，LLM 做「骨架 → 裁剪 → 补充 → 实例化」。
        """
        caps_str = ", ".join(capabilities) if capabilities else "通用创作能力"
        skeleton = self.skeleton_for_task_type(task_type)
        skeleton_str = " → ".join(skeleton)
        domain_block = (
            f"\n\n【领域先验（优先遵循，可裁剪/补充，不要照搬）】\n{domain_prior_text}"
            if domain_prior_text else ""
        )
        return f"""将以下任务分解为可执行的子任务。返回 JSON 数组，每个元素包含：
- title: 子任务标题
- description: 详细描述（1-2句）
- required_capabilities: 该步骤需要的能力标签列表（从以下选：{caps_str}）
- dependencies: 依赖的前置子任务标题列表（可为空）
- expected_outputs: 该步骤应产出的文件名列表（如 ["outline.md"]，可为空）
- acceptance_criteria: 该步骤的验收标准列表（2-3条，可执行、可检验）

规则：
1. 每步子任务应明确、可独立执行
2. 依赖必须形成有向无环图（DAG）
3. 最多 {max_tasks} 个子任务，创作类任务 4-8 步
4. 参考以下阶段骨架（可裁剪/合并/补充）：{skeleton_str}
5. required_capabilities 必须严格从上述列表中选择，不要自创标签
{domain_block}

任务：{task}

请只返回 JSON 数组，不要有其他文字。"""

    def _parse_decompose_response(
        self,
        task: str,
        response: str,
        capabilities: List[str],
        max_tasks: int,
        reasoning: str = "",
    ) -> List[SubTask]:
        """解析 LLM 分解响应为 SubTask 列表（提取出来供流式调用复用）"""
        import json
        import uuid as _uuid

        # 提取 JSON
        json_str = response.strip()
        if "```" in json_str:
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        tasks_data = json.loads(json_str)

        # [修复] 先为每个 task 分配 ID，再解析依赖关系
        # 原逻辑在 _find_dep_id 之后才赋值 id，导致依赖全部丢失
        for td in tasks_data:
            td["_id"] = f"sub_{_uuid.uuid4().hex[:6]}"

        sub_tasks = []
        for i, td in enumerate(tasks_data):
            dep_ids = [
                td2["_id"] for dt in td.get("dependencies", [])
                for td2 in tasks_data
                if td2.get("title") == dt
            ]
            sub_tasks.append(SubTask(
                id=td["_id"],
                title=td.get("title", f"sub_task_{i}"),
                description=td.get("description", ""),
                required_capabilities=td.get("required_capabilities", []),
                dependencies=dep_ids,
                estimated_complexity=min(1.0, len(td.get("description", "")) / 500),
                # Phase 1：阶段验收契约字段（LLM 生成，缺失时默认空）
                objective=td.get("objective", ""),
                expected_outputs=list(td.get("expected_outputs") or []),
                expected_artifacts=list(td.get("expected_artifacts") or td.get("expected_outputs") or []),
                acceptance_criteria=list(td.get("acceptance_criteria") or []),
            ))

        return sub_tasks[:max_tasks]

    # ==================== 规则分解 ====================

    def _rule_decompose(
        self,
        task: str,
        capabilities: List[str],
        max_tasks: int,
    ) -> List[SubTask]:
        """基于规则的启发式分解（LLM 不可用时的回退）"""
        import uuid as _uuid

        sub_tasks = []

        # 基于关键词拆解
        keywords_map = {
            "分析": ("analyze", ["analysis"], "对数据进行深入分析"),
            "数据": ("process_data", ["data_processing"], "处理和清洗数据"),
            "可视化": ("visualize", ["visualization"], "生成可视化图表"),
            "报告": ("report", ["report_writing"], "撰写分析报告"),
            "审核": ("review", ["quality_check"], "审核结果质量"),
            "优化": ("optimize", ["optimization"], "优化和改进方案"),
            "生成": ("generate", ["generation"], "生成内容或产出"),
            "总结": ("summarize", ["synthesis"], "总结和归纳"),
        }

        used_ids: Set[str] = set()
        for keyword, (task_id, caps, desc) in keywords_map.items():
            if keyword in task and task_id not in used_ids and len(sub_tasks) < max_tasks:
                sub_tasks.append(SubTask(
                    id=task_id,
                    title=f"{keyword}阶段",
                    description=f"{desc}: {task}",
                    required_capabilities=[c for c in caps if c in capabilities],
                    estimated_complexity=0.5,
                ))
                used_ids.add(task_id)

        # 至少有一个默认子任务
        if not sub_tasks:
            sub_tasks.append(SubTask(
                id="execute",
                title="执行任务",
                description=task,
                required_capabilities=capabilities[:3],
                estimated_complexity=0.5,
            ))

        # 添加执行依赖（前一步完成才执行下一步）
        for i in range(1, len(sub_tasks)):
            sub_tasks[i].dependencies = [sub_tasks[i - 1].id]

        return sub_tasks

    # ==================== 拓扑排序 ====================

    @staticmethod
    def _topological_sort(sub_tasks: List[SubTask]) -> List[List[str]]:
        """拓扑排序：将 DAG 分层，同层可并行执行"""
        # 构建依赖图
        graph: Dict[str, List[str]] = {t.id: t.dependencies for t in sub_tasks}
        task_map: Dict[str, SubTask] = {t.id: t for t in sub_tasks}

        layers: List[List[str]] = []
        completed: Set[str] = set()
        remaining = set(t.id for t in sub_tasks)

        max_iterations = len(sub_tasks) + 1
        for _ in range(max_iterations):
            if not remaining:
                break

            # 当前可执行的：所有依赖都已完成
            ready = [
                tid for tid in remaining
                if all(dep in completed for dep in graph.get(tid, []))
            ]

            if not ready:
                # 存在循环依赖，直接全部释放
                layers.append(list(remaining))
                break

            layers.append(ready)
            completed.update(ready)
            remaining -= set(ready)

        return layers

    # ==================== 辅助方法 ====================

    def get_ready_sub_tasks(self, plan: DecompositionPlan,
                            completed_ids: Set[str]) -> List[SubTask]:
        """获取当前可执行的子任务"""
        ready = []
        for sub in plan.sub_tasks:
            if sub.id in completed_ids:
                continue
            if sub.status == "running":
                continue
            deps_met = all(dep in completed_ids for dep in sub.dependencies)
            if deps_met:
                ready.append(sub)
        return ready

    def get_next_layer(self, plan: DecompositionPlan,
                       current_layer: int) -> List[str]:
        """获取下一层的子任务 ID 列表"""
        if 0 <= current_layer < len(plan.execution_order):
            return plan.execution_order[current_layer]
        return []

    def get_progress(self, plan: DecompositionPlan) -> Dict[str, Any]:
        """获取分解计划的执行进度"""
        total = len(plan.sub_tasks)
        completed = sum(1 for t in plan.sub_tasks if t.status == "completed")
        running = sum(1 for t in plan.sub_tasks if t.status == "running")
        failed = sum(1 for t in plan.sub_tasks if t.status == "failed")
        return {
            "total": total,
            "completed": completed,
            "running": running,
            "failed": failed,
            "pending": total - completed - running - failed,
            "progress": completed / total if total > 0 else 0,
        }


# ═══════════════════════════════════════════════════════════
# 智能协作策略推断
# ═══════════════════════════════════════════════════════════

# 审核/质检类能力标签
_REVIEW_CAPS = {"review", "quality_check", "proofread", "proofreading", "copy_editing", "审校", "审阅"}

# 核心创意交付物关键词（上游产出这些内容时，下游应该讨论确认）
_CORE_DELIVERABLE_KEYWORDS = {
    "设定", "大纲", "世界观", "角色", "人物", "剧本", "分镜",
    "outline", "character", "worldview", "storyboard", "script",
    "concept", "architecture", "规划", "策划",
}

# 质检语义关键词（description 含这些时触发 retry）
_QUALITY_KEYWORDS = {
    "检查", "校对", "审阅", "验证", "审核", "核查", "质检",
    "review", "check", "proofread", "verify", "audit", "quality",
}


def infer_collaboration_strategy(sub_tasks: List["SubTask"]) -> List["SubTask"]:
    """为每个子任务推断合理的 dialogue_policy 和 on_complete

    规则：
    1. 审核类阶段 → on_complete="retry"（打回不合格）
    2. 最后一个阶段 → on_complete="retry"（终稿质量把关）
    3. 下游是审核类 or 上游产出核心交付物 → dialogue_policy="shared_thread"
    4. 其余 → dialogue_policy="handoff_only", on_complete="continue"

    只填充 dialogue_policy/on_complete 为空的阶段（不覆盖用户已配置的值）。
    """
    if not sub_tasks:
        return sub_tasks

    # 构建依赖反向索引：谁依赖我 = 我的下游
    downstream_map: Dict[str, List[str]] = {t.id: [] for t in sub_tasks}
    task_map = {t.id: t for t in sub_tasks}
    for t in sub_tasks:
        for dep_id in t.dependencies:
            if dep_id in downstream_map:
                downstream_map[dep_id].append(t.id)

    last_task = sub_tasks[-1]  # 执行顺序最后一个

    for task in sub_tasks:
        caps_lower = {c.lower() for c in task.required_capabilities}
        desc_lower = (task.description + " " + task.title).lower()

        # ── on_complete 推断 ──
        if not task.on_complete:
            is_review_task = bool(caps_lower & _REVIEW_CAPS)
            is_last = (task.id == last_task.id)
            has_quality_keyword = any(kw in desc_lower for kw in _QUALITY_KEYWORDS)

            if is_review_task or is_last or has_quality_keyword:
                task.on_complete = "retry"
            else:
                task.on_complete = "continue"

        # ── dialogue_policy 推断 ──
        if not task.dialogue_policy:
            should_discuss = False

            # 规则 1：当前阶段是审核类 → 应该跟上游讨论
            if bool(caps_lower & _REVIEW_CAPS):
                should_discuss = True

            # 规则 2：上游产出核心交付物 → 下游应该讨论确认
            if not should_discuss:
                for dep_id in task.dependencies:
                    upstream = task_map.get(dep_id)
                    if upstream:
                        upstream_desc = (upstream.description + " " + upstream.title).lower()
                        if any(kw in upstream_desc for kw in _CORE_DELIVERABLE_KEYWORDS):
                            should_discuss = True
                            break

            # 规则 3：上下游属于不同专业方向（capabilities 交集为空）
            if not should_discuss and task.dependencies:
                for dep_id in task.dependencies:
                    upstream = task_map.get(dep_id)
                    if upstream:
                        upstream_caps = {c.lower() for c in upstream.required_capabilities}
                        if upstream_caps and caps_lower and not (upstream_caps & caps_lower):
                            should_discuss = True
                            break

            task.dialogue_policy = "shared_thread" if should_discuss else "handoff_only"

    return sub_tasks
