"""WorkflowPlanner — TaskDecomposer + AgentTeamBuilder → agent_order (无 Engine 依赖)"""

from __future__ import annotations
import asyncio
import json
from typing import List, Optional, Tuple

from core.agent.registry import get_registry
from core.agent.types import AgentDefinition
from core.orchestration.decomposer import TaskDecomposer, DecompositionPlan
from core.agent.team_builder import AgentTeamBuilder, TeamComposition, TeamAssignment, RequiredRole
from core.discovery.capability import CapabilityDiscovery
from core.logging import get_logger
from tools.llm_client import LLMClient

_logger = get_logger("planner")


# ── 领域-Agent 映射（从模板系统数据驱动，不再硬编码）──

# 通用 agent ID 白名单 — 跨领域共享，始终保留
_GENERIC_AGENT_IDS = {
    "fixture_agent_general",
    "fixture_agent_researcher",
    "fixture_agent_reviewer",
    "fixture_agent_writer",
    "fixture_agent_legacy_tools",
    "builtin_general",
    "builtin_researcher",
    "builtin_reviewer",
    "builtin_writer",
}

# 内置 Agent 领域关键词语义兜底 — 模板系统不可用时使用
# （仅 6 个领域，与 quality_domains 表一致，新增领域无需改代码）
_AGENT_ID_DOMAIN_FALLBACK: dict[str, str] = {
    "novel": "platform:web_novel",
    "comic_static": "platform:comic_static",
    "comic": "platform:comic_drama",
    "drama": "platform:comic_drama",
    "music": "platform:music_production",
    "research": "platform:research_report",
}

# P0-1: 平台已知内容领域（走模板匹配）。未知领域不再静默路由 web_novel，
# 而是走通用 Agent 能力组建（见 _compose_generic_team）。
_PLATFORM_CONTENT_DOMAINS = (
    "web_novel", "comic_static", "comic_drama", "music_production", "research_report",
)


class _UnknownDomainRouting(Exception):
    """P0-1: 领域未知 → 不路由到内容模板，交由通用能力组建处理。"""


async def _compose_generic_team(task, plan, agent_map, capabilities, agent_order):
    """P0-1: 未知领域兜底 —— 基于能力标签组建通用团队（不依赖内容模板）。

    与 matcher 异常回退共用同一套 capability 组建逻辑，保证未知领域
    走通用 Agent（builtin_researcher/writer/reviewer/general + 用户自定义）。
    """
    from core.agent.team_builder import AgentTeamBuilder
    required_caps: List[str] = []
    for st in plan.sub_tasks:
        required_caps.extend(st.required_capabilities)
    required_caps = list(dict.fromkeys(required_caps)) or capabilities[:3]

    builder = AgentTeamBuilder(agent_map)
    composition = await builder.form_team(task, required_capabilities=required_caps)

    for layer in plan.execution_order:
        for sub_id in layer:
            sub = next((s for s in plan.sub_tasks if s.id == sub_id), None)
            if not sub:
                continue
            assigned = _pick_agent_for_subtask(sub, composition, agent_map, agent_order)
            if assigned and assigned not in agent_order:
                agent_order.append(assigned)
                sub.assigned_agent = assigned

    return composition


async def _load_template_domain_map() -> dict[str, str]:
    """从模板系统加载 agent_id → domain_id 映射（数据驱动，无需硬编码）。

    模板 ID 格式: tpl_{domain}_{role}，agent 实例 ID: agent_tpl_{domain}_{role}。
    同时从 quality_domains 加载 canonical 领域 ID 列表。
    """
    mapping: dict[str, str] = {}
    try:
        from core.agent.template import get_template_registry
        registry = get_template_registry()
        templates = await registry.list()
        for tmpl in templates:
            if not tmpl.domain:
                continue
            domain_id = f"platform:{tmpl.domain}" if not tmpl.domain.startswith("platform:") else tmpl.domain
            # agent 实例 ID 通常为 agent_{template_id}
            agent_id = f"agent_{tmpl.id}"
            mapping[agent_id] = domain_id
            # 也记录模板 ID 本身（供 fixture agent 匹配）
            mapping[tmpl.id] = domain_id
    except Exception:
        pass

    # 兜底：从 quality_domains 加载 canonical 领域列表
    if not mapping:
        try:
            from core.storage.database import get_read_db
            db = await get_read_db()
            cursor = await db.execute(
                "SELECT id FROM quality_domains WHERE is_archived = 0 AND source = 'platform'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                dom_id = row["id"]
                short = dom_id.replace("platform:", "")
                mapping[short] = dom_id
        except Exception:
            pass

    return mapping


async def _agent_domain_from_id(agent_id: str) -> str:
    """从 agent ID 推断领域（优先模板系统，回退关键词兜底）"""
    aid = agent_id.lower()

    # 优先: 从模板 ID 推断 (agent_tpl_comic_script_writer → tpl_comic_script_writer)
    if aid.startswith("agent_tpl_"):
        tmpl_id = aid[len("agent_"):]
        try:
            from core.agent.template import get_template_registry
            tmpl = await get_template_registry().get(tmpl_id)
            if tmpl and tmpl.domain:
                d = tmpl.domain
                return f"platform:{d}" if not d.startswith("platform:") else d
        except Exception:
            pass

    # 兜底: 关键词匹配（仅 6 个内置领域）
    for key, domain in _AGENT_ID_DOMAIN_FALLBACK.items():
        if key in aid:
            return domain
    return ""


async def _filter_agents_by_domain(
    agents: List[AgentDefinition],
    domain_id: str,
) -> List[AgentDefinition]:
    """根据 domain_id 过滤 agents（数据驱动：从模板系统获取领域映射）。

    保留规则：
    - 用户自定义 agents（agent_*，非 fixture）始终保留
    - 通用 agents（GENERIC_AGENT_IDS）始终保留
    - 领域专属 agents 仅在 domain_id 匹配时保留（从模板 domain 字段推断）
    """
    if not domain_id:
        return agents

    # 从模板系统加载 agent→domain 映射（优先）
    template_map = await _load_template_domain_map()

    filtered = []
    excluded = []
    for a in agents:
        aid = a.id
        # 用户自定义 agent 始终保留
        if aid.startswith("agent_") and not aid.startswith("agent_fixture") and not aid.startswith("agent_tpl_"):
            filtered.append(a)
            continue
        # 通用 agent 始终保留
        if aid in _GENERIC_AGENT_IDS:
            filtered.append(a)
            continue
        # 从模板映射获取 agent 所属领域
        agent_domain = template_map.get(aid) or await _agent_domain_from_id(aid)
        if agent_domain and agent_domain != domain_id:
            excluded.append(aid)
            continue

        filtered.append(a)

    if excluded:
        _logger.info(
            "领域过滤 domain_id=%s: 排除 %d 个不相关领域 agents: %s",
            domain_id, len(excluded), excluded,
        )

    return filtered


async def _gather_capabilities(agents: List[AgentDefinition], domain_id: str) -> List[str]:
    """从模板系统收集 capability_tags 作为词汇表（plan_agent_order / stream 共享）

    优先从模板系统收集（标签统一、与匹配器对齐），模板为空时回退到 Agent capabilities。
    """
    capabilities: List[str] = []
    try:
        from core.agent.template import get_template_registry
        t_registry = get_template_registry()
        all_templates = await t_registry.list()
        for tmpl in all_templates:
            tmpl_domain = (tmpl.domain or "").replace("platform:", "")
            if not domain_id or tmpl_domain == domain_id:
                capabilities.extend(tmpl.capability_tags)
        capabilities = list(dict.fromkeys(capabilities))
    except Exception:
        pass
    if not capabilities:
        for a in agents:
            a_domain = getattr(a, "type", "") or ""
            is_generic = a.id.startswith("builtin_") or "general" in a.id
            if is_generic or a_domain in ("domain", ""):
                capabilities.extend(a.capabilities)
            else:
                a_domain_id = await _agent_domain_from_id(a.id)
                if a_domain_id == domain_id or not a_domain_id:
                    capabilities.extend(a.capabilities)
        capabilities = list(dict.fromkeys(capabilities))
    return capabilities


async def ensure_generic_agents(registry) -> List[AgentDefinition]:
    """确保至少有一批可用的通用 Agent。

    空池（用户尚未创建任何 Agent）时，注册 GenericDomainAdapter 内置的
    researcher/writer/reviewer/general 通用 Agent，避免自动编排在空池上
    选到空 Agent。已有 active Agent 时直接返回，不重复注册。
    """
    agents = await registry.list({"status": "active"})
    if agents:
        return agents

    # 内置通用 Agent 定义（插件体系废弃后迁至 fixtures）
    from core.fixtures.builtin_agents import BUILTIN_GENERIC_AGENTS

    registered = 0
    for defn in BUILTIN_GENERIC_AGENTS:
        try:
            if await registry.get(defn["id"]):
                continue
            await registry.register(defn)
            registered += 1
        except Exception as e:
            _logger.warning("注册内置通用 Agent 失败 id=%s: %s", defn.get("id"), e)

    if registered:
        _logger.info("空 Agent 池兜底：已注册 %d 个内置通用 Agent", registered)

    return await registry.list({"status": "active"})


async def plan_agent_order(
    task: str,
    use_llm: bool = True,
    domain_id: str = "",
) -> Tuple[DecompositionPlan, TeamComposition, List[str], List[dict], str, List[List[str]]]:
    """分解任务 → 模板匹配 → 返回 agent 执行顺序

    Args:
        task: 用户任务描述
        use_llm: 是否用 LLM 智能分解
        domain_id: 领域ID（如 platform:web_novel）

    Returns:
        6 元组: (plan, composition, agent_order, match_details, planning_reasoning, execution_layers)
        execution_layers: 拓扑层级列表，同层可并行
    """
    registry = get_registry()
    agents = await ensure_generic_agents(registry)

    # 领域过滤
    agents = await _filter_agents_by_domain(agents, domain_id)
    agent_map = {a.id: a for a in agents}

    # Step 1: LLM 分解任务
    capabilities = await _gather_capabilities(agents, domain_id)

    # 任务分解用 flash 模型（快速稳定出多步骤 JSON）
    try:
        from config import DEFAULT_LLM_MODEL
        decompose_model = DEFAULT_LLM_MODEL  # deepseek-v4-flash
    except Exception:
        decompose_model = "deepseek-v4-flash"
    llm = LLMClient({"model": decompose_model}) if use_llm else None

    decomposer = TaskDecomposer()
    # PR-9：加载领域先验，注入分解（骨架 → 裁剪 → 补充 → 实例化）
    domain_prior_text = ""
    if domain_id:
        try:
            from core.orchestration.domain_prior import load_domain_prior
            _dp = await load_domain_prior(domain_id)
            domain_prior_text = _dp.to_prompt_context() if _dp else ""
        except Exception:
            domain_prior_text = ""
    plan = await decomposer.decompose(
        task, capabilities, llm_client=llm, max_sub_tasks=8,
        domain_prior_text=domain_prior_text,
    )
    decompose_reasoning = plan.reasoning

    # PR-10：validate → critique → refine（非流式，refine 后更新 sub_tasks）
    if use_llm and plan.sub_tasks:
        try:
            from core.run.plan_spec import normalize_plan
            from core.orchestration.plan_validator import validate_plan
            from core.orchestration.plan_critic import critique_plan
            from core.orchestration.plan_refiner import refine_plan
            from core.orchestration.decomposer import SubTask

            candidate = normalize_plan({
                "task": task,
                "domain_id": domain_id,
                "strategy": "pipeline",
                "phases": [
                    {**sub.model_dump(), "agent_id": getattr(sub, "assigned_agent", "") or sub.id}
                    for sub in plan.sub_tasks
                ],
                "execution_layers": plan.execution_order,
            })

            validation = validate_plan(candidate)
            if validation.valid:
                critique = await critique_plan(candidate, llm)
                if critique.issues:
                    refined = await refine_plan(candidate, critique, llm)
                    if refined is not None:
                        revalidation = validate_plan(refined)
                        if revalidation.valid:
                            new_sub_tasks = []
                            for p in refined.phases:
                                new_sub_tasks.append(SubTask(
                                    id=p.phase_id,
                                    title=p.label or p.phase_id,
                                    description=p.description,
                                    dependencies=list(p.dependencies),
                                    assigned_agent=p.agent_id,
                                    objective=p.objective,
                                    expected_outputs=list(p.expected_outputs),
                                    expected_artifacts=list(p.expected_artifacts),
                                    acceptance_criteria=list(p.acceptance_criteria),
                                    dialogue_policy=p.dialogue_policy or "shared_thread",
                                    on_complete=p.on_complete or "continue",
                                    tool_policy=p.tool_policy or "auto",
                                    max_retries=p.max_retries,
                                ))
                            plan.sub_tasks = new_sub_tasks
                            plan.execution_order = refined.execution_layers or plan.execution_order
        except Exception as e:
            _logger.warning("Critic/Refiner 失败（非致命）: %s", e)

    # 用推理模型生成"为什么这样规划"的简短解释（用户可感知）
    planning_reasoning = decompose_reasoning
    if use_llm and plan.sub_tasks:
        # 后台异步生成规划解释，不阻塞主流程（强引用跟踪，防 GC 回收）
        from core.task_tracker import create_task as _tracked_create_task
        steps_desc = "\n".join(f"- {t.title}" for t in plan.sub_tasks[:8])
        _reasoning_task = _tracked_create_task(
            _generate_planning_reasoning(task, steps_desc),
            name="planning_reasoning",
        )
        try:
            planning_reasoning = await asyncio.wait_for(asyncio.shield(_reasoning_task), timeout=40.0)
        except asyncio.TimeoutError:
            _logger.warning("推理模型生成解释超时(40s)，返回空")
            planning_reasoning = ""

    # Step 2: 使用 TemplateMatcher 匹配 Agent（新逻辑）
    # 将 domain_id 从 "platform:xxx" 转换为模板系统的格式
    tmpl_domain = domain_id.replace("platform:", "") if domain_id.startswith("platform:") else domain_id
    is_known_domain = tmpl_domain in _PLATFORM_CONTENT_DOMAINS

    agent_order: List[str] = []
    match_details: List[dict] = []  # 匹配度元信息，供前端编排预览展示
    try:
        # P0-1: 未知领域不再静默路由 web_novel —— 抛 _UnknownDomainRouting 走通用能力组建
        if not is_known_domain:
            raise _UnknownDomainRouting(f"未知领域: {domain_id or '(空)'}")
        from core.orchestration.template_matcher import get_template_matcher
        from core.agent.template import get_template_registry

        matcher = get_template_matcher(llm_client=llm)
        match_results = await matcher.match(plan.sub_tasks, tmpl_domain, llm_client=llm)

        for result in match_results:
            agent_id = None

            if result.matched_template_id and result.matched_template_id.startswith("tpl_"):
                # 从模板查找已注册的 Agent 实例
                agent_id = f"agent_{result.matched_template_id}"
                if agent_id not in agent_map:
                    # 模板存在但 Agent 未实例化，即时创建
                    try:
                        t_registry = get_template_registry()
                        agent_dict = await t_registry.instantiate(result.matched_template_id)
                        agent_dict["id"] = agent_id
                        agent_dict["type"] = "domain"
                        await registry.register(agent_dict)
                        agent_map[agent_id] = await registry.get(agent_id)
                    except Exception:
                        agent_id = None

            elif result.matched_template_id and result.matched_template_id.startswith("temp_"):
                # LLM 临时生成的 Agent，需要实例化
                try:
                    t_registry = get_template_registry()
                    temp_data = await t_registry.get_temporary(result.matched_template_id)
                    if temp_data:
                        agent_id = f"agent_{result.matched_template_id}"
                        # 2026-08-12：temp agent 兜底处理任意任务，必须给基础配置——
                        # file_write 产出 + thinking:disabled 防 reasoning 吃光 + max_tokens 至少 16384
                        _mt = int(temp_data.get("max_tokens") or 0)
                        _mt = _mt if _mt >= 16384 else 16384
                        _model = temp_data.get("recommended_model") or DEFAULT_LLM_MODEL
                        agent_dict = {
                            "id": agent_id,
                            "name": temp_data.get("name", result.subtask_title),
                            "type": "domain",
                            "status": "active",
                            "emoji": temp_data.get("emoji", "🤖"),
                            "role": temp_data.get("role", ""),
                            "capabilities": json.loads(temp_data.get("capability_tags", "[]")),
                            "system_prompt": temp_data.get("system_prompt", ""),
                            "model": _model,
                            "temperature": temp_data.get("temperature", 0.7),
                            "max_tokens": _mt,
                            "tool_ids": ["file_write"],
                            "model_profile": {
                                "llm": {
                                    "model_id": _model,
                                    "temperature": temp_data.get("temperature", 0.7),
                                    "max_tokens": _mt,
                                    "extra": {"thinking": "disabled"},
                                },
                                "image": None, "video": None, "music": None, "judge": None,
                            },
                            "version": "1.0.0",
                            "extra": {"temporary": True, "temp_id": result.matched_template_id},
                        }
                        await registry.register(agent_dict)
                        agent_map[agent_id] = await registry.get(agent_id)
                except Exception:
                    agent_id = None

            # 收集匹配度元信息
            # Phase 3：接入历史质量评分（feedback → agent_matcher，时间衰减 + 最小样本数）
            final_score = float(result.score)
            if agent_id:
                try:
                    from core.orchestration.agent_matcher import score_agent
                    _am = await score_agent(result.subtask_title, agent_id, result.score)
                    final_score = _am.score
                    if _am.reasons:
                        result.reason = (result.reason or "") + f"；{_am.reasons[0]}"
                except Exception:
                    final_score = float(result.score)

            is_generic = (result.matched_template_name or "") and "助手" in (result.matched_template_name or "")
            match_details.append({
                "subtask_title": result.subtask_title,
                "agent_id": agent_id or "",
                "agent_name": result.matched_template_name or "未匹配",
                "score": round(final_score),
                "level": result.level,
                "is_generic": is_generic,
                "fallback_used": bool(result.fallback_type),
                "needs_custom": result.score < 60 and not agent_id,
                "suggestion": result.suggestion if hasattr(result, "suggestion") else None,
            })

            if agent_id and agent_id not in agent_order:
                agent_order.append(agent_id)
                # 更新 sub_task 的 assigned_agent
                for sub in plan.sub_tasks:
                    if sub.title == result.subtask_title:
                        sub.assigned_agent = agent_id
                        break

            _logger.info(
                "模板匹配: subtask=%s → agent=%s score=%.0f level=%s fallback=%s",
                result.subtask_title,
                result.matched_template_name or "none",
                result.score,
                result.level,
                result.fallback_type,
            )

    except Exception as e:
        if isinstance(e, _UnknownDomainRouting):
            _logger.info("P0-1 未知领域 {}：走通用 Agent 能力组建（不再静默路由 web_novel）", e)
        else:
            _logger.warning("模板匹配失败，回退到旧逻辑: %s", e)
        composition = await _compose_generic_team(task, plan, agent_map, capabilities, agent_order)

    # 兜底：如果 agent_order 仍为空
    if not agent_order:
        agent_order = [a.id for a in agents[:3]]

    # P1-10补：确保每个子任务都分配到真实 Agent。
    # 修复：LLM 偶发弱输出时 matcher/_compose 可能留空 assigned_agent，
    # PhaseSpec.from_plan 会 fallback 到 task.id（sub_* 子任务 id 当 agent_id）
    # → set_agents 插 project_agents 触发 FK 崩溃。
    _fallback_pool = agent_order or [a.id for a in agents[:3]] or list(agent_map.keys())
    if _fallback_pool:
        _assign_i = 0
        for _sub in plan.sub_tasks:
            if not getattr(_sub, "assigned_agent", ""):
                _aid = _fallback_pool[_assign_i % len(_fallback_pool)]
                _sub.assigned_agent = _aid
                _assign_i += 1
                if _aid not in agent_order:
                    agent_order.append(_aid)

    # 构建 TeamComposition（保留兼容性）
    composition = TeamComposition(
        team_id=f"team_{plan.plan_id}",
        task_description=task,
        assignments=[
            TeamAssignment(
                role=RequiredRole(
                    role_id=sub.id,
                    description=sub.title,
                    required_capabilities=sub.required_capabilities,
                ),
                agent_id=aid,
                confidence=0.7,
            )
            for sub, aid in zip(plan.sub_tasks, agent_order)
        ],
        total_agents=len(agent_order),
        avg_confidence=0.7,
    )

    # P0-1 补充：未知领域走通用组建（_compose_generic_team）时 match_details 为空，
    # 导致 preview 编排预览空白。此时从已分配的 sub_tasks 构造 match_details，
    # 让前端能展示通用团队（builtin_researcher 等）。
    if not match_details:
        for sub in plan.sub_tasks:
            _aid = getattr(sub, "assigned_agent", "")
            if not _aid:
                continue
            _agent_def = agent_map.get(_aid)
            _agent_name = getattr(_agent_def, "name", "") if _agent_def else ""
            match_details.append({
                "subtask_title": sub.title,
                "agent_id": _aid,
                "agent_name": _agent_name or _aid,
                "score": 100 if _agent_def else 60,
                "level": "strong" if _agent_def else "acceptable",
                "is_generic": True,
                "fallback_used": False,
                "needs_custom": not _agent_def,
                "suggestion": None,
            })

    # 收集规划思维链：保留 LLM 推理结果，追加 match_details 摘要
    match_summary_lines = []
    for d in match_details:
        if d.get("agent_name"):
            line = f"- **{d['subtask_title']}** → {d['agent_name']}（匹配度 {d['score']}%, {d['level']}）"
            match_summary_lines.append(line)
    match_summary = "\n".join(match_summary_lines)
    # 不要覆盖 LLM 推理结果——在前面追加 match 摘要
    if planning_reasoning and match_summary:
        planning_reasoning = f"## Agent 匹配详情\n{match_summary}\n\n## 规划分析\n{planning_reasoning}"
    elif match_summary:
        planning_reasoning = f"## Agent 匹配详情\n{match_summary}"

    _logger.info(
        "规划完成: sub_tasks=%d, agents=%d, order=%s, layers=%d",
        len(plan.sub_tasks),
        len(agent_order),
        agent_order,
        len(plan.execution_order),
    )
    return plan, composition, agent_order, match_details, planning_reasoning, plan.execution_order


async def plan_workflow(task: str, engine, use_llm: bool = True):
    """已废弃 — 保留签名供旧代码 import，不再配置 Engine"""
    plan, composition, agent_order, match_details, _, _execution_layers = await plan_agent_order(task, use_llm=use_llm)
    if hasattr(engine, "agent_order"):
        engine.agent_order = agent_order
    return plan, composition, agent_order


def _sse_event(phase: str, data: dict) -> str:
    """格式化为 SSE 事件字符串"""
    import json as _json
    payload = _json.dumps({**data, "phase": phase}, ensure_ascii=False)
    return f"event: {phase}\ndata: {payload}\n\n"


def _infer_mode_from_layers(execution_order: List[List[str]]) -> str:
    """根据拓扑层级自动推断执行模式

    规则：
    - 所有层都只有 1 个任务 → sequential
    - 任何层有 >1 个任务 → dag (新模式)
    - 只有 1 层且 >1 个任务 → parallel
    """
    if not execution_order:
        return "sequential"
    has_parallel_layer = any(len(layer) > 1 for layer in execution_order)
    if not has_parallel_layer:
        return "sequential"
    if len(execution_order) == 1:
        return "parallel"
    return "dag"


async def plan_agent_order_stream(
    task: str,
    domain_id: str = "",
    strategy: str = "pipeline",
    content_type: str = "",
    total_units: int = 0,
    reference_works: List[Dict[str, Any]] = None,
    existing_outline: str = "",
    uploaded_file_paths: List[str] = None,
    clarify_answers: Dict[str, str] = None,
):
    """流式版本的 plan_agent_order —— 以 SSE 事件流返回规划过程

    事件类型：
      - domain: 领域识别进度
      - clarification_required: 需要用户澄清（Phase 2，blocking）
      - skeleton_preview: 长内容骨架预规划（Pass1+2，仅当 total_units>1）
      - decompose: 任务分解进度
      - matching: Agent 匹配进度
      - reasoning: 推理模型思维链 token（流式）
      - plan: 最终计划结果

    clarify_answers: 用户对澄清问题的回答 {question_id: answer}。空=首次规划
    （先意图理解，必要时触发澄清）；非空=已澄清，跳过澄清把答案注入分解。
    """
    import json as _json
    import traceback

    registry = get_registry()
    agents = await ensure_generic_agents(registry)

    # 领域过滤
    agents = await _filter_agents_by_domain(agents, domain_id)
    agent_map = {a.id: a for a in agents}

    # ===== 长内容：骨架预规划（Pass1+2，跳过 Pass3 详细 Brief）=====
    skeleton_outline = None
    if total_units > 1:
        try:
            from core.skeleton.planner import SkeletonPlanner
            from config import DEFAULT_LLM_MODEL as _DLM
            yield _sse_event("skeleton_preview", {"status": "start", "message": f"检测到长内容（{total_units} 单元），正在预规划全局骨架..."})
            sp = SkeletonPlanner(llm_client=LLMClient({"model": _DLM}))
            skeleton_outline = await sp.plan_outline(
                intent=task,
                content_type=content_type or "novel",
                total_units=total_units,
            )
            yield _sse_event("skeleton_preview", {
                "status": "done",
                "skeleton": skeleton_outline.model_dump(),
                "arcs_count": len(skeleton_outline.arcs),
                "units_count": len(skeleton_outline.units),
            })
        except Exception as e:
            _logger.warning("骨架预规划失败（非致命，退回普通规划）: %s", e)
            yield _sse_event("skeleton_preview", {"status": "error", "message": f"骨架预规划失败：{e}，退回普通规划"})
            skeleton_outline = None

    # ===== Phase 0: 意图理解 + 澄清（Phase 2 5.1）=====
    clarify_answers = clarify_answers or {}
    if not clarify_answers:
        # 首次规划：先理解意图，答案会改变计划结构时才触发澄清
        try:
            from core.orchestration.intent import analyze_intent
            from config import DEFAULT_LLM_MODEL as _IM
            _intent = await analyze_intent(task, LLMClient({"model": _IM}))
            if _intent.needs_clarification:
                yield _sse_event("clarification_required", {
                    "status": "required",
                    "blocking": True,
                    "questions": [q.model_dump() for q in _intent.open_questions],
                    "intent": _intent.model_dump(),
                })
                return  # 暂停，等用户回答后带 clarify_answers 重新 stream
        except Exception as e:
            _logger.warning("意图理解失败（非致命，继续规划）: %s", e)

    # ===== Phase 0.5: 领域先验加载（PR-9：读领域阶段骨架，注入分解 prompt）=====
    domain_prior = None
    if domain_id:
        try:
            from core.orchestration.domain_prior import load_domain_prior
            domain_prior = await load_domain_prior(domain_id)
            if domain_prior and domain_prior.phases:
                yield _sse_event("domain_prior", {
                    "domain_id": domain_id,
                    "phases": [p.model_dump() for p in domain_prior.phases],
                    "typical_mode": domain_prior.typical_mode,
                })
        except Exception as e:
            _logger.warning("领域先验加载失败（非致命，走通用策略）: %s", e)
            domain_prior = None

    # ===== Phase 1: Decompose（流式推送思维链 + 内容 token）=====
    yield _sse_event("decompose", {"status": "start", "message": "正在分析任务并拆解为子任务..."})

    capabilities = await _gather_capabilities(agents, domain_id)

    try:
        from config import DEFAULT_LLM_MODEL
        decompose_model = DEFAULT_LLM_MODEL
    except Exception:
        decompose_model = "deepseek-v4-flash"
    llm = LLMClient({"model": decompose_model})

    # 内联流式分解：直接用 chat_messages_stream，实时 yield token
    decomposer = TaskDecomposer()
    decompose_prompt = decomposer._build_decompose_prompt(task, capabilities, 8)

    # Phase 2：注入用户澄清答案（用户已回答 clarification_required，严格依据答案分解）
    if clarify_answers:
        answers_str = "；".join(f"{qid}：{ans}" for qid, ans in clarify_answers.items())
        decompose_prompt = decompose_prompt + f"\n\n【用户澄清】\n{answers_str}\n请严格依据这些澄清答案分解任务。"

    # Phase 0.5：注入领域先验（PR-9：骨架 → 裁剪 → 补充 → 实例化，不是严格遵循）
    if domain_prior:
        prior_text = domain_prior.to_prompt_context()
        if prior_text:
            decompose_prompt = decompose_prompt + f"\n\n【领域最佳实践】\n{prior_text}\n请基于上述骨架裁剪/补充/实例化，不要从零发明。"

    # ===== 长内容：注入参考作品/已有章节内容（后端预读完整内容，分段注入 prompt）=====
    # 注：LLMClient 不支持 function calling，无法让 LLM 调用 file_read 工具
    # 改为后端预读文件内容，分段注入 prompt（前几段 + 关键摘要）
    reference_works = reference_works or []
    uploaded_file_paths = uploaded_file_paths or []
    if reference_works or existing_outline:
        ref_ctx_parts = []
        # 后端预读文件内容（如果文件已保存到磁盘）
        if uploaded_file_paths:
            for fp in uploaded_file_paths[:2]:  # 最多读前 2 个文件
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        full_content = f.read()
                    # 分段注入：前 2000 字 + 中间 1000 字 + 后 1000 字（覆盖开头/发展/结尾）
                    segments = []
                    if len(full_content) > 4000:
                        segments.append(f"【开头】\n{full_content[:2000]}")
                        mid = len(full_content) // 2
                        segments.append(f"【中段】\n{full_content[mid-500:mid+500]}")
                        segments.append(f"【结尾】\n{full_content[-1000:]}")
                    else:
                        segments.append(full_content[:4000])
                    filename = fp.split("/")[-1]
                    ref_ctx_parts.append(f"【上传文件《{filename}》完整内容（分段）】\n" + "\n\n".join(segments))
                except Exception as e:
                    _logger.warning("读取上传文件失败 %s: %s", fp, e)
        # 同时注入 reference_works 内容片段（前端已传）
        if reference_works:
            for rw in reference_works[:2]:
                title = rw.get("title", "参考作品")
                content = (rw.get("content") or "")[:2000]
                if content:
                    ref_ctx_parts.append(f"【参考作品《{title}》内容片段】\n{content}\n")
        if existing_outline:
            ref_ctx_parts.append(f"【已有章节内容片段】\n{existing_outline[:2000]}\n")
        if ref_ctx_parts:
            ref_ctx = (
                "\n\n" + "\n".join(ref_ctx_parts) +
                "\n请基于上述参考作品的实际内容（而非仅凭标题猜测）判断其类型、风格、领域，"
                "并在分解任务时参考其文风/结构/节奏特征。"
            )
            decompose_prompt = decompose_prompt + ref_ctx

    # ===== 长内容：注入骨架上下文，让 LLM 分解为"骨架设计 + 首章 + 审校"结构 =====
    if skeleton_outline:
        arcs_summary = "\n".join(
            f"  - {a.title}（第 {a.unit_range[0]}-{a.unit_range[1]} 单元）：{a.core_conflict}"
            for a in skeleton_outline.arcs
        )
        skeleton_ctx = (
            f"\n\n【长内容骨架预规划】\n"
            f"前提：{skeleton_outline.premise}\n"
            f"主题：{skeleton_outline.theme}\n"
            f"总单元数：{total_units}\n"
            f"叙事弧：\n{arcs_summary}\n\n"
            f"请将任务分解为以下结构（不要为每个单元创建子任务，单元执行将在执行阶段基于骨架动态展开）：\n"
            f"1. 骨架设计与世界观准备（含角色设定、世界观细节、大纲确认）\n"
            f"2. 首个单元的创作（基于骨架的第 1 单元，验证基调与风格）\n"
            f"3. 审校与连续性检查（完成后质量审校、伏笔核对）\n"
            f"如需其他辅助职责（如连续性追踪员、氛围调控），可酌情增加，但总数不超过 5 个。"
        )
        decompose_prompt = decompose_prompt + skeleton_ctx

    decompose_messages = [{"role": "user", "content": decompose_prompt}]
    full_response = ""
    decompose_reasoning = ""

    try:
        async for chunk in llm.chat_messages_stream(decompose_messages):
            if chunk.startswith("__REASONING__:"):
                token = chunk[len("__REASONING__:"):]
                if token:
                    decompose_reasoning += token
                    yield _sse_event("decompose", {"status": "reasoning", "content": token})
            else:
                full_response += chunk
                yield _sse_event("decompose", {"status": "content", "content": chunk})
    except Exception:
        _logger.warning("流式分解失败，回退非流式")

    # 解析 LLM 输出为 sub_tasks，构建完整 DecompositionPlan
    if full_response:
        try:
            sub_tasks = decomposer._parse_decompose_response(task, full_response, capabilities, 8, decompose_reasoning)
            execution_order = decomposer._topological_sort(sub_tasks)
            # [新增] 智能推断协作策略
            from core.orchestration.decomposer import infer_collaboration_strategy
            sub_tasks = infer_collaboration_strategy(sub_tasks)
            import uuid as _uuid
            plan = DecompositionPlan(
                plan_id=f"plan_{_uuid.uuid4().hex[:8]}",
                original_task=task,
                sub_tasks=sub_tasks,
                execution_order=execution_order,
                total_complexity=sum(t.estimated_complexity for t in sub_tasks) / max(len(sub_tasks), 1),
                estimated_steps=len(execution_order),
                reasoning=decompose_reasoning,
            )
        except Exception as e:
            _logger.warning("流式分解解析失败，回退非流式: %s", e)
            plan = await decomposer.decompose(task, capabilities, llm_client=llm, max_sub_tasks=8)
    else:
        plan = await decomposer.decompose(task, capabilities, llm_client=llm, max_sub_tasks=8)

    yield _sse_event("decompose", {
        "status": "done",
        "sub_tasks": [{"title": t.title, "description": t.description} for t in plan.sub_tasks],
        "total": len(plan.sub_tasks),
    })

    # ===== Phase 2: Template Matching =====
    yield _sse_event("matching", {"status": "start", "message": "正在匹配最合适的Agent..."})

    tmpl_domain = domain_id.replace("platform:", "") if domain_id.startswith("platform:") else domain_id
    is_known_domain = tmpl_domain in _PLATFORM_CONTENT_DOMAINS

    agent_order: List[str] = []
    match_details: List[dict] = []

    try:
        # P0-1: 未知领域不再静默路由 web_novel —— 抛 _UnknownDomainRouting 走通用能力组建
        if not is_known_domain:
            raise _UnknownDomainRouting(f"未知领域: {domain_id or '(空)'}")
        from core.orchestration.template_matcher import get_template_matcher
        from core.agent.template import get_template_registry

        matcher = get_template_matcher(llm_client=llm)
        match_results = await matcher.match(plan.sub_tasks, tmpl_domain, llm_client=llm)

        for result in match_results:
            agent_id = None
            if result.matched_template_id and result.matched_template_id.startswith("tpl_"):
                agent_id = f"agent_{result.matched_template_id}"
                if agent_id not in agent_map:
                    try:
                        t_registry = get_template_registry()
                        agent_dict = await t_registry.instantiate(result.matched_template_id)
                        agent_dict["id"] = agent_id
                        agent_dict["type"] = "domain"
                        await registry.register(agent_dict)
                        agent_map[agent_id] = await registry.get(agent_id)
                    except Exception:
                        agent_id = None
            elif result.matched_template_id and result.matched_template_id.startswith("temp_"):
                try:
                    t_registry = get_template_registry()
                    temp_data = await t_registry.get_temporary(result.matched_template_id)
                    if temp_data:
                        agent_id = f"agent_{result.matched_template_id}"
                        agent_dict = {
                            "id": agent_id, "name": temp_data.get("name", result.subtask_title),
                            "type": "domain", "status": "active",
                            "emoji": temp_data.get("emoji", "🤖"),
                            "role": temp_data.get("role", ""),
                            "capabilities": _json.loads(temp_data.get("capability_tags", "[]")),
                            "system_prompt": temp_data.get("system_prompt", ""),
                            "model": temp_data.get("recommended_model", DEFAULT_LLM_MODEL),
                            "temperature": temp_data.get("temperature", 0.7),
                            "max_tokens": temp_data.get("max_tokens", 4096),
                            "version": "1.0.0",
                            "extra": {"temporary": True, "temp_id": result.matched_template_id},
                        }
                        await registry.register(agent_dict)
                        agent_map[agent_id] = await registry.get(agent_id)
                except Exception:
                    agent_id = None

            is_generic = (result.matched_template_name or "") and "助手" in (result.matched_template_name or "")
            detail = {
                "subtask_title": result.subtask_title,
                "agent_id": agent_id or "",
                "agent_name": result.matched_template_name or "未匹配",
                "score": round(result.score),
                "level": result.level,
                "is_generic": is_generic,
                "fallback_used": bool(result.fallback_type),
                "needs_custom": result.score < 60 and not agent_id,
                "suggestion": result.suggestion if hasattr(result, "suggestion") else None,
            }
            match_details.append(detail)

            if agent_id and agent_id not in agent_order:
                agent_order.append(agent_id)
                for sub in plan.sub_tasks:
                    if sub.title == result.subtask_title:
                        sub.assigned_agent = agent_id
                        break

            # 逐个推送匹配结果
            yield _sse_event("matching", {
                "status": "progress",
                "subtask": result.subtask_title,
                "agent_name": result.matched_template_name or "未匹配",
                "score": round(result.score),
                "level": result.level,
            })

    except Exception as e:
        if isinstance(e, _UnknownDomainRouting):
            _logger.info("P0-1 未知领域 {}：走通用 Agent 能力组建（不再静默路由 web_novel）", e)
        else:
            _logger.warning("模板匹配失败，回退到旧逻辑: %s", e)
        composition = await _compose_generic_team(task, plan, agent_map, capabilities, agent_order)

    if not agent_order:
        agent_order = [a.id for a in agents[:3]]

    # P1-10补：确保每个子任务都分配到真实 Agent（防 from_plan 落 sub.id → FK 崩溃）
    _fallback_pool = agent_order or [a.id for a in agents[:3]] or list(agent_map.keys())
    if _fallback_pool:
        _assign_i = 0
        for _sub in plan.sub_tasks:
            if not getattr(_sub, "assigned_agent", ""):
                _aid = _fallback_pool[_assign_i % len(_fallback_pool)]
                _sub.assigned_agent = _aid
                _assign_i += 1
                if _aid not in agent_order:
                    agent_order.append(_aid)

    yield _sse_event("matching", {"status": "done"})

    # ===== Phase 3: Reasoning (streaming) =====
    steps_desc = "\n".join(f"- {t.title}" for t in plan.sub_tasks[:8])
    reasoning_text = ""
    if plan.sub_tasks:
        yield _sse_event("reasoning", {"status": "start", "message": "正在生成规划解释..."})
        try:
            from config import get_model_for_scope
            model = get_model_for_scope("planner")
            reason_client = LLMClient({"model": model})
            prompt = f"""用户任务：{task}
AI 规划了以下执行步骤：
{steps_desc}
请分析每一步的必要性：为什么需要这一步？用中文，分点简洁说明。"""
            messages = [{"role": "user", "content": prompt}]
            async for chunk in reason_client.chat_messages_stream(messages):
                if chunk.startswith("__REASONING__:"):
                    token = chunk[len("__REASONING__:"):]
                    reasoning_text += token
                    yield _sse_event("reasoning", {"status": "token", "content": token})
                else:
                    reasoning_text += chunk
                    yield _sse_event("reasoning", {"status": "token", "content": chunk})
        except Exception:
            _logger.warning("推理模型流式生成失败:\n%s", traceback.format_exc())

    yield _sse_event("reasoning", {"status": "done"})

    # ===== Build match summary =====
    match_summary_lines = []
    for d in match_details:
        if d.get("agent_name"):
            line = f"- **{d['subtask_title']}** → {d['agent_name']}（匹配度 {d['score']}%, {d['level']}）"
            match_summary_lines.append(line)
    match_summary = "\n".join(match_summary_lines)
    if reasoning_text and match_summary:
        planning_reasoning = f"## Agent 匹配详情\n{match_summary}\n\n## 规划分析\n{reasoning_text}"
    elif match_summary:
        planning_reasoning = f"## Agent 匹配详情\n{match_summary}"
    else:
        planning_reasoning = reasoning_text

    # ===== Phase 3.5: validate → critique → refine（PR-10：语义审查 + 自动修订）=====
    candidate = None
    try:
        from core.run.plan_spec import normalize_plan
        from core.orchestration.plan_validator import validate_plan
        from core.orchestration.plan_critic import critique_plan
        from core.orchestration.plan_refiner import refine_plan

        # SubTask → PhasePlan：assigned_agent 映射为 agent_id
        candidate = normalize_plan({
            "task": task,
            "domain_id": domain_id,
            "strategy": strategy,
            "phases": [
                {**sub.model_dump(), "agent_id": getattr(sub, "assigned_agent", "") or sub.id}
                for sub in plan.sub_tasks
            ],
            "execution_layers": plan.execution_order,
        })

        validation = validate_plan(candidate)
        if not validation.valid:
            _logger.warning("规划结构校验失败（保留原计划继续）: %s", validation.errors)
            candidate = None
        else:
            yield _sse_event("critic", {"status": "start", "phase": "critic"})
            critique = await critique_plan(candidate, llm)
            yield _sse_event("critic", {
                "status": "done",
                "issues": [i.model_dump() for i in critique.issues],
                "overall": critique.overall,
            })

            if critique.issues:
                yield _sse_event("refine", {"status": "start", "phase": "refine"})
                refined = await refine_plan(candidate, critique, llm)
                if refined is not None:
                    revalidation = validate_plan(refined)
                    if revalidation.valid:
                        candidate = refined
                yield _sse_event("refine", {"status": "done", "phase": "refine"})
    except Exception as e:
        _logger.warning("Critic/Refiner 失败（非致命，保留原计划）: %s", e)
        candidate = None

    # ===== Phase 4: Complete =====
    # 构建 dependencies 映射：subtask_id → [依赖的 subtask_id]（phase_id 是唯一引用键，不用 title）
    dependencies_map = {sub.id: list(sub.dependencies) for sub in plan.sub_tasks}
    agent_name_map = {d["subtask_title"]: d.get("agent_name", "") for d in match_details}

    # AI 生成项目名称
    _project_name = await _generate_project_name(task)
    _domain_name = domain_id.replace("platform:", "").replace("_", " ").title() if domain_id else ""

    # Phase 4：pipeline 从 candidate（refine 后）或 plan.sub_tasks 生成
    if candidate is not None and candidate.phases:
        pipeline_list = [
            {
                "phase_id": p.phase_id,
                "name": p.label or p.phase_id,
                "title": p.label or p.phase_id,
                "description": p.description,
                "objective": p.objective or p.description,
                "agent_id": p.agent_id,
                "agent_name": agent_name_map.get(p.label, agent_name_map.get(p.phase_id, "")),
                "dependencies": list(p.dependencies),
                "expected_inputs": list(p.expected_inputs),
                "expected_outputs": list(p.expected_outputs),
                "expected_artifacts": list(p.expected_artifacts or p.expected_outputs),
                "acceptance_criteria": list(p.acceptance_criteria),
                "dialogue_policy": p.dialogue_policy or "shared_thread",
                "on_complete": p.on_complete or "continue",
                "quality_domain_ids": list(p.quality_domain_ids),
                "knowledge_packs": list(p.knowledge_packs),
                "tool_policy": p.tool_policy or "auto",
                "max_retries": p.max_retries,
                "risk_level": p.risk_level,
            }
            for p in candidate.phases
        ]
        final_execution_layers = candidate.execution_layers or plan.execution_order
        summary_n = len(candidate.phases)
    else:
        pipeline_list = [
            {
                "phase_id": sub.id,
                "name": sub.title,
                "title": sub.title,
                "description": sub.description,
                "objective": sub.objective or sub.description,
                "agent_id": getattr(sub, "assigned_agent", ""),
                "agent_name": agent_name_map.get(sub.title, ""),
                "dependencies": dependencies_map.get(sub.id, []),
                "expected_inputs": list(sub.expected_inputs),
                "expected_outputs": list(sub.expected_outputs),
                "expected_artifacts": list(sub.expected_artifacts or sub.expected_outputs),
                "acceptance_criteria": list(sub.acceptance_criteria),
                "dialogue_policy": sub.dialogue_policy or "shared_thread",
                "on_complete": sub.on_complete or "continue",
                "quality_domain_ids": list(sub.quality_domain_ids),
                "knowledge_packs": list(sub.knowledge_packs),
                "tool_policy": sub.tool_policy or "auto",
                "max_retries": sub.max_retries,
                "risk_level": getattr(sub, "risk_level", "medium"),
            }
            for sub in plan.sub_tasks
        ]
        final_execution_layers = plan.execution_order
        summary_n = len(plan.sub_tasks)

    yield _sse_event("plan", {
        "status": "complete",
        "pipeline": pipeline_list,
        "execution_layers": [
            [sid for sid in layer]
            for layer in final_execution_layers
        ],
        "agent_order": agent_order,
        "mode": "auto",
        "strategy": strategy,
        "domain_id": domain_id,
        "domain_name": _domain_name,
        "project_name": _project_name,
        "planning_reasoning": planning_reasoning,
        "summary": f"{summary_n} phases planned",
        # 长内容：骨架预规划结果（前端展示用，执行阶段会重新做 Pass1+2+3）
        "skeleton": skeleton_outline.model_dump() if skeleton_outline else None,
        "total_units": total_units if total_units > 1 else 0,
        "content_type": content_type or "",
    })


async def _generate_project_name(task: str) -> str:
    """用 AI 为项目生成一个简洁的中文名称（≤12字）"""
    import traceback
    try:
        from config import get_model_for_scope
        model = get_model_for_scope("planner")
        client = LLMClient({"model": model})
        prompt = f"""为一个项目起一个简洁的中文名称（≤12个字），概括这段任务描述的核心主题：

任务：{task}

要求：
- ≤12个汉字
- 不包含引号、特殊符号
- 能让人一眼看出这个项目是做什么的
- 只回复名称，不要解释

名称："""
        resp = await client.chat(prompt, temperature=0.5, max_tokens=30)
        name = resp.strip().strip('"').strip("'").strip("。").strip()[:24]
        if not name:
            return task.split("\n")[0][:30].strip()
        return name
    except Exception:
        import logging
        logging.getLogger("planner").debug("AI 项目命名失败，fallback: %s", traceback.format_exc())
        return task.split("\n")[0][:30].strip()


async def _generate_planning_reasoning(task: str, steps_desc: str) -> str:
    """用推理模型生成规划解释（后台任务）"""
    import traceback
    try:
        from config import get_model_for_scope
        model = get_model_for_scope("planner")
        client = LLMClient({"model": model})
        prompt = f"""用户任务：{task}
AI 规划了以下执行步骤：
{steps_desc}
请分析每一步的必要性：为什么需要这一步？用中文，分点简洁说明。"""
        resp, reason = await client.chat_messages_with_reasoning([{"role": "user", "content": prompt}])
        # 检测 LLM 调用是否失败（返回错误字符串）
        if resp.startswith("[LLM Error]") or resp.startswith("[LLM BudgetExceeded]") or resp.startswith("[LLM CircuitOpen]"):
            _logger.warning("推理模型生成解释失败(model=%s): %s", model, resp[:200])
            return ""
        parts = []
        if reason:
            parts.append(reason)
        if resp and not resp.startswith("["):
            parts.append(resp)
        result = "\n\n".join(parts).strip()
        _logger.info("推理模型生成解释成功(model=%s): %d chars", model, len(result))
        return result
    except Exception:
        _logger.warning("推理模型生成解释异常:\n%s", traceback.format_exc())
        return ""


def _pick_agent_for_subtask(sub, composition, agent_map, already_used) -> Optional[str]:
    discovery = CapabilityDiscovery(list(agent_map.values()))

    for cap in sub.required_capabilities:
        role_id = f"role_{cap}"
        role_map = {a.role.role_id: a.agent_id for a in composition.assignments}
        if role_id in role_map:
            aid = role_map[role_id]
            if aid not in already_used:
                return aid

    best_id = discovery.best_for_capabilities(
        sub.required_capabilities,
        exclude_ids=list(already_used),
    )
    if best_id:
        return best_id

    for aid in agent_map:
        if aid not in already_used:
            return aid
    return next(iter(agent_map), None)
