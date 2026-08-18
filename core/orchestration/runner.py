"""工作流执行器 — HTTP / Celery 共用

D1: 所有 /api/run 路径统一经 CollaborationGraph (GraphRuntime) 调度。
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Union, Optional

from api.models import RunRequest
from core.agent.state_manager import StateManager
from core.agent.registry import get_registry
from core.events import event_bus
from core.logging import get_logger
from core.orchestration.protocol import compile_protocol, merge_run_params
from core.project.manager import ProjectManager
from core.agent.types import ProjectConfig
from core.run.collaboration_graph import CollaborationGraph
from core.run.diagnostics import RunDiagnostics, Severity
from core.run.run_recovery import clear_recover_pending

_logger = get_logger("orchestration.runner")
_platform_ready = False

# 限制同时执行的 run 数量，防止资源耗尽
import os as _os
_MAX_CONCURRENT_RUNS = int(_os.environ.get("MAX_CONCURRENT_RUNS", "3"))
_RUN_SEM = None  # 延迟初始化（需在 event loop 内）


async def ensure_platform_ready() -> None:
    global _platform_ready
    if _platform_ready:
        return
    from core.storage.database import init_db

    await init_db()
    # 领域能力由质量领域体系提供（quality_domains 种子），无需 adapter 装配
    _platform_ready = True


async def resolve_project(req: RunRequest):
    if req.project_id:
        project = await ProjectManager.get(req.project_id)
        if project:
            project.config.task_description = req.task
            return project

    return await ProjectManager.create(
        name=req.project_name,
        project_id=req.project_id or "",
        mode=req.mode,
        config=ProjectConfig(
            task_description=req.task,
            review_mode=req.review_mode,
            extra={"use_tools": req.use_tools},
        ),
        template_id=req.template_id,
    )


async def execute_workflow(payload: Union[RunRequest, Dict[str, Any]]) -> Dict[str, Any]:
    global _RUN_SEM
    from core.run.graph_registry import (
        is_execution_alive,
        mark_execution_finished,
        mark_execution_started,
    )

    import asyncio as _asyncio
    if _RUN_SEM is None:
        _RUN_SEM = _asyncio.Semaphore(_MAX_CONCURRENT_RUNS)

    await ensure_platform_ready()

    req = payload if isinstance(payload, RunRequest) else RunRequest(**payload)
    project = await resolve_project(req)

    if req.project_id and is_execution_alive(project.id):
        raise RuntimeError(f"项目 {project.id} 工作流已在运行中")

    # 分布式运行锁：多实例 / 多进程部署时同一项目只允许一个执行者
    from core.run.run_lock import run_lock

    mark_execution_started(project.id)
    try:
        async with _RUN_SEM:
            async with run_lock(project.id):
                return await _execute_workflow_body(req, project)
    finally:
        mark_execution_finished(project.id)


async def _execute_workflow_body(req: RunRequest, project) -> Dict[str, Any]:
    output_dir = f"outputs/{project.id}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 降级诊断累加器（2026-08-09 P1）：collab 创建前（domain/Ingestor/骨架）的降级
    # 先记录于此，collab 就绪后 flush 到 collab.run_context，最终透传给用户。
    _diag = RunDiagnostics()

    # LLM readiness 预检（P1-5）：LLM 不可用时提前失败，避免白烧骨架规划 120s 超时
    try:
        from core.studio.service import llm_available
        if not llm_available():
            _logger.warning("LLM 未配置，run 提前失败: project=%s", project.id)
            await ProjectManager.update_status(project.id, "failed")
            event_bus.broadcast(
                event="workflow_error",
                data={"project_id": project.id, "error": "LLM 未配置，无法运行工作流（请在设置页配置 API Key）"},
                project_id=project.id,
            )
            return {"status": "failed", "error": "LLM 未配置，无法运行工作流（请在设置页配置 API Key）"}
    except Exception:
        pass

    if req.project_id and project.status != "paused":
        if project.status in ("running", "queued"):
            _logger.warning("中断/重启运行，清除 checkpoint: %s", project.id)
        await StateManager.delete_checkpoint(project.id)

    agent_ids = list(req.agent_ids or [])
    if not agent_ids and project.agents:
        agent_ids = [
            pa.agent_id for pa in sorted(project.agents, key=lambda x: x.join_order)
        ]
    template_id = req.template_id or project.template_id or ""

    project.config.task_description = req.task
    project.config.review_mode = req.review_mode
    if req.domain_id:
        project.config.domain_id = req.domain_id
    if req.use_tools is not None:
        project.config.extra["use_tools"] = req.use_tools

    # 持久化 config 变更（task_description / review_mode / domain_id / use_tools）
    try:
        config_dict = project.config.model_dump()
        await ProjectManager.update(project.id, {"config": config_dict})
    except Exception as e:
        _logger.warning("项目 config 持久化失败（非致命）: %s", e)

    await ProjectManager.update_status(project.id, "running")
    await clear_recover_pending(project.id)
    try:
        from core.observability.metrics import metrics
        metrics.workflow_started(req.mode or "sequential")
    except Exception:
        pass
    event_bus.broadcast(
        event="agent_state",
        data={"state": "running", "project_id": project.id},
        project_id=project.id,
    )

    template = None
    if template_id:
        registry = get_registry()
        template = await registry.get_template(template_id)
        if not template:
            raise ValueError(f"模板不存在: {template_id}")

    params = run_params_from_request(
        req,
        template.workflow_config if template and hasattr(template, "workflow_config") else None,
    )

    auto_plan = bool(req.auto_plan or (not template_id and not agent_ids))

    # 领域识别应天然生效（planner 的职责），不限于 auto_plan。
    # 之前只在 auto_plan 且无 agent_ids 时识别 → natural/agent_ids 路径不设 domain_id
    # → 领域知识（写作规范/章节字数）与质量规则都不加载，writer 不知道写作要求。
    # 修正：只要未显式指定 domain，就用 LLM 从任务识别。
    effective_domain_id = req.domain_id
    if not effective_domain_id:
        try:
            from api.routes.plan import _match_domain_llm
            matched_id, _, confidence, _ = await _match_domain_llm(req.task)
            if matched_id:
                effective_domain_id = matched_id
                _logger.info(f"LLM 识别 domain_id={effective_domain_id} ({confidence:.2f})")
        except Exception as e:
            _logger.warning("domain 识别失败: %s", e)
            _diag.record(
                Severity.WARNING, "domain_recognition",
                f"LLM 识别 domain 失败: {e}",
                "沿用空 domain，领域知识/质量规则不生效",
            )

    if not req.use_graph_engine:
        _logger.warning("use_graph_engine=false 已弃用，仍走 CollaborationGraph (D1)")

    # --- Phase 4 新增：Ingestor 处理 ---
    ingest_output = None
    if req.ingest_mode and req.ingest_mode != "create":
        try:
            from core.ingestor.base import Ingestor, IngestorInput, ExistingUnit, ReferenceWork
            from tools.llm_client import LLMClient
            ingestor = Ingestor(llm_client=LLMClient())
            ingest_input = IngestorInput(
                mode=req.ingest_mode,
                intent=req.task,
                content_type=req.content_type,
                total_units=req.total_units,
                existing_units=[ExistingUnit(**u) for u in req.existing_units],
                continue_from=req.continue_from,
                reference_works=[ReferenceWork(**r) for r in req.reference_works],
                rewrite_units=req.rewrite_units,
                rewrite_feedback=req.rewrite_feedback,
                existing_outline=req.existing_outline,
            )
            ingest_output = await ingestor.ingest(ingest_input)
        except Exception as e:
            _logger.warning("Ingestor 处理失败: %s", e)
            _diag.record(
                Severity.WARNING, "ingestor",
                f"Ingestor 处理失败（mode={req.ingest_mode}）: {e}",
                "跳过预填充，续写/改写可能缺上下文",
            )

    # --- Phase 1 新增：长内容骨架规划 ---
    skeleton = None
    if req.total_units > 1:
        try:
            from core.skeleton.integration import plan_skeleton_if_needed
            from tools.llm_client import LLMClient
            skeleton = await plan_skeleton_if_needed(
                project_id=project.id,
                task=req.task,
                content_type=req.content_type or "novel",
                total_units=req.total_units,
                llm_client=LLMClient(),
                diagnostics=_diag,
            )
        except Exception as e:
            _logger.warning("骨架规划失败，继续无骨架执行: %s", e)
            _diag.record(
                Severity.CRITICAL, "skeleton_planner",
                f"骨架规划异常: {e}",
                "继续无骨架执行（writer 无骨架约束，人物设定可能漂移）",
            )

    try:
        collab = await CollaborationGraph.from_request(
            project,
            req.task,
            template=template,
            agent_ids=agent_ids or None,
            mode=params.get("mode", "auto"),
            strategy=getattr(req, "strategy", "") or params.get("strategy", ""),
            review_mode=params["review_mode"],
            domain_id=effective_domain_id,
            roundtable_config=params["roundtable_config"],
            parallel_config=params["parallel_config"],
            auto_plan=auto_plan,
            use_llm_decompose=req.use_llm_decompose,
        )

        # collab 就绪：把 collab 创建前的降级 flush 进 run_context（P1）
        if _diag.entries:
            for e in _diag.entries:
                collab.run_context.record_degradation(
                    e.severity.value, e.component, e.message, e.recovery_action,
                )

        # --- 骨架驱动的 Phase 静态扩展（在 from_request 之后，骨架注入 ctx 之前）---
        # 把"写作"phase 替换为 N 个章节 phase，用扩展后的 specs 重建 graph
        if skeleton and req.total_units > 1:
            try:
                from core.skeleton.integration import expand_phases_with_skeleton
                from core.run.template_compiler import TemplateCompiler as _TC
                expanded_specs = expand_phases_with_skeleton(list(collab.phase_specs), skeleton)
                if len(expanded_specs) != len(collab.phase_specs):
                    new_graph, new_specs = _TC.compile(
                        specs=expanded_specs,
                        mode=collab.mode,
                        graph_id=f"run_{project.id}",
                    )
                    collab.graph = new_graph
                    collab.phase_specs = new_specs
                    _logger.info(
                        "长内容 Phase 扩展: %d → %d phases（%d 单元）",
                        len(collab.phase_specs), len(new_specs), req.total_units,
                    )
            except Exception as e:
                _logger.warning("Phase 扩展失败，使用原 phases: %s", e)
                collab.run_context.record_degradation(
                    Severity.CRITICAL, "skeleton_planner",
                    f"骨架驱动 Phase 扩展失败: {e}",
                    "使用原 phases（多章变单章，骨架不生效）",
                )

        from core.run.graph_registry import set_collaboration_graph, remove_collaboration_graph
        set_collaboration_graph(project.id, collab)

        # --- Phase 1 新增：骨架注入 RunContext ---
        if skeleton:
            collab.run_context.skeleton = skeleton.model_dump()
            collab.run_context.content_type = req.content_type or "novel"
            collab.run_context.total_units = req.total_units
            collab.run_context.current_unit = 1

            # --- 修正2：skeleton_ready SSE 事件 ---
            try:
                from core.events import broadcast_event
                await broadcast_event(project.id, "skeleton_ready", {"skeleton": skeleton.model_dump()})
            except Exception:
                pass

            # --- Phase 2 新增：分层记忆 + 连续性追踪初始化 ---
            try:
                from core.memory.layered_memory import LayeredMemory
                from core.memory.continuity_tracker import ContinuityTracker

                mem = LayeredMemory()
                mem.build_L4(skeleton.model_dump())
                arc = skeleton.get_arc_for_unit(1)
                if arc:
                    mem.build_L3(arc.model_dump())

                collab.run_context.layered_memory = mem.to_dict()

                tracker = ContinuityTracker.from_skeleton(skeleton.model_dump())
                collab.run_context.continuity_state = tracker.to_state()
            except Exception as e:
                _logger.warning("分层记忆初始化失败: %s", e)
                collab.run_context.record_degradation(
                    Severity.CRITICAL, "layered_memory",
                    f"分层记忆初始化失败: {e}",
                    "连续性追踪失效，后续章节可能不连贯",
                )

            # --- 修正3：Atmosphere + Character Profiles 初始化（非 None，供前端展示）---
            try:
                from core.memory.atmosphere_tracker import AtmosphereTracker
                collab.run_context.atmosphere_state = AtmosphereTracker().to_state()
                characters = set()
                for arc in skeleton.arcs:
                    for ca in (arc.character_arcs or []):
                        characters.add(ca.get("character", ""))
                if characters:
                    collab.run_context.character_profiles = [
                        {"character_id": c, "core_identity": "", "behavioral_patterns": [],
                         "speech_style": {"characteristics": [], "avoids": [], "emotion_expression": ""},
                         "relationships": [], "growth_stage": {"current": "", "next_milestone": "", "growth_trigger": ""}}
                        for c in characters if c
                    ]
            except Exception as e:
                _logger.warning("Atmosphere/Profile 初始化失败: %s", e)
                collab.run_context.record_degradation(
                    Severity.WARNING, "atmosphere",
                    f"Atmosphere/Character Profile 初始化失败: {e}",
                    "氛围/角色一致性追踪不可用",
                )

        # --- Phase 4 新增：Ingestor 预填数据注入 ---
        if ingest_output:
            try:
                if ingest_output.memory_preset:
                    collab.run_context.layered_memory = ingest_output.memory_preset
                if ingest_output.continuity_preset:
                    collab.run_context.continuity_state = ingest_output.continuity_preset
                if ingest_output.character_profiles_preset:
                    collab.run_context.character_profiles = ingest_output.character_profiles_preset
                if ingest_output.atmosphere_preset:
                    collab.run_context.atmosphere_state = ingest_output.atmosphere_preset
                collab.run_context.current_unit = ingest_output.start_unit

                # 风格规则注入知识库
                if ingest_output.knowledge_entries_to_inject:
                    try:
                        from core.knowledge.store import KnowledgeStore
                        from core.knowledge.models_kb import KnowledgeEntry, KnowledgeCategory, InjectMode
                        for entry_dict in ingest_output.knowledge_entries_to_inject:
                            ke = KnowledgeEntry(
                                domain_id=req.domain_id or "platform:default",
                                category=KnowledgeCategory.STYLE,
                                title=entry_dict.get("title", ""),
                                content=entry_dict.get("content", ""),
                                inject_mode=InjectMode.ALWAYS,
                            )
                            await KnowledgeStore.create_entry(ke)
                    except Exception as e:
                        _logger.warning("知识条目注入失败: %s", e)
                        collab.run_context.record_degradation(
                            Severity.WARNING, "ingestor",
                            f"知识条目注入失败: {e}",
                            "agent 缺领域知识",
                        )

                # RAG 索引
                if ingest_output.rag_content_to_index:
                    try:
                        import uuid as _uuid
                        from core.knowledge import KnowledgeItem
                        from core.knowledge.vector_kb import get_knowledge_base, bulk_store
                        kb = get_knowledge_base(f"project_{project.id}")
                        items = []
                        for item_dict in ingest_output.rag_content_to_index:
                            meta = item_dict.get("metadata", {})
                            ki = KnowledgeItem(
                                id=f"ingest_{_uuid.uuid4().hex[:8]}",
                                content=item_dict["content"],
                                domain_id=f"project_{project.id}",
                                source="ingestor",
                                tags=[f"unit_{meta.get('unit_number', '')}"] if meta.get("unit_number") else [],
                            )
                            items.append(ki)
                        await bulk_store(kb, items)
                    except Exception as e:
                        _logger.warning("RAG 索引失败: %s", e)
                        collab.run_context.record_degradation(
                            Severity.WARNING, "ingestor",
                            f"RAG 索引失败: {e}",
                            "检索不可用",
                        )
            except Exception as e:
                _logger.warning("Ingestor 预填数据注入失败: %s", e)
                collab.run_context.record_degradation(
                    Severity.WARNING, "ingestor",
                    f"Ingestor 预填数据注入失败: {e}",
                    "续写/改写缺上下文",
                )

        ordered_ids = [s.agent_id for s in collab.phase_specs]
        if ordered_ids:
            await ProjectManager.set_agents(project.id, ordered_ids)

        import asyncio as _asyncio
        # 工作流整体超时 = 各 agent 节点预算之和 + 调度开销（2026-08-09 fix）。
        # 单 agent 预算化上限默认 ~1920s；3 章顺序执行（writer 32000 tokens + 空产出重试）
        # 实测 ~50min，固定 1800s / 阶段数×600 都被超掉（reviewer 被整体超时取消）。
        # 用节点预算和作为上限：节点自身超时才是真卡死的边界，工作流上限只做兜底。
        _workflow_timeout = float(_os.getenv("WORKFLOW_TIMEOUT_SECONDS", "1800"))
        try:
            from core.graph.runtime import resolve_agent_timeout
            from core.graph.types import NodeType
            _sum_budgets = 0.0
            for _n in collab.graph.nodes:
                if _n.type == NodeType.AGENT:
                    _sum_budgets += resolve_agent_timeout({}, _n)
            _workflow_timeout = max(
                _workflow_timeout,
                _sum_budgets + float(_os.getenv("WORKFLOW_SCHEDULE_OVERHEAD_SECONDS", "600")),
            )
        except Exception as _te:
            _logger.debug("工作流超时预算计算失败（用默认）: %s", _te)
        try:
            result, status = await _asyncio.wait_for(collab.execute(), timeout=_workflow_timeout)
        except _asyncio.TimeoutError:
            _logger.error("工作流整体超时 (%ds): project=%s", _workflow_timeout, project.id)
            await ProjectManager.update_status(project.id, "failed")
            event_bus.broadcast(
                event="workflow_error",
                data={"project_id": project.id, "error": f"工作流整体超时 ({_workflow_timeout:.0f}s)"},
                project_id=project.id,
            )
            remove_collaboration_graph(project.id)
            return {"status": "failed", "error": f"工作流整体超时 ({_workflow_timeout:.0f}s)"}

        if status != "paused":
            remove_collaboration_graph(project.id)

        # 降级结果透传（2026-08-09 P1/P2）：run 完成时把 diagnostics 附加到 result，
        # 并广播 run_degraded 事件（前端据此展示降级警告）。
        try:
            _run_ctx = collab.run_context
            if _run_ctx and getattr(_run_ctx, "diagnostics", None) and _run_ctx.diagnostics.get("is_degraded"):
                if not isinstance(result, dict):
                    result = {"status": status, "graph_id": getattr(result, "graph_id", "")}
                result["degraded"] = True
                result["diagnostics"] = _run_ctx.diagnostics
                event_bus.broadcast(
                    event="run_degraded",
                    data=_run_ctx.diagnostics,
                    project_id=project.id,
                )
                _logger.warning(
                    "工作流完成但有降级: project=%s critical=%s warning=%s",
                    project.id,
                    _run_ctx.diagnostics.get("critical_count", 0),
                    _run_ctx.diagnostics.get("warning_count", 0),
                )
        except Exception as _de:
            _logger.debug("降级透传失败（不影响 run）: %s", _de)

        # PR-11：运行结束后收集 feedback（质量门结果回流，用于后续 Agent 选择优化）
        try:
            from core.orchestration.feedback import record_run_feedback, RunFeedback, PhaseFeedback
            _run_ctx = collab.run_context
            if _run_ctx and _run_ctx.quality_records:
                phase_results = []
                for qr in _run_ctx.quality_records:
                    phase_results.append(PhaseFeedback(
                        phase_id=qr.phase_id,
                        agent_id=qr.agent_id,
                        status="success" if qr.passed else "failed",
                        quality_gate_passed=qr.passed,
                    ))
                if phase_results:
                    feedback = RunFeedback(
                        plan_id=_run_ctx.config_snapshot.get("plan_id", ""),
                        revision=int(_run_ctx.config_snapshot.get("plan_revision", 1)),
                        run_id=_run_ctx.run_id,
                        phase_results=phase_results,
                    )
                    await record_run_feedback(feedback)
        except Exception as _fe:
            _logger.warning("feedback 记录失败（非致命）: %s", _fe)

        await ProjectManager.update_status(project.id, status)
        event_bus.broadcast(
            event="workflow_result",
            data={
                "project_id": project.id,
                "status": status,
                "result_keys": list(result.keys()) if isinstance(result, dict) else [],
                "phase_ids": result.get("phase_ids", []),
                # P0-4：终态映射需要的完整信息
                "termination_reason": (
                    result.get("error") or result.get("termination_reason")
                    if isinstance(result, dict) else None
                ),
                "error": result.get("error") if isinstance(result, dict) else None,
                "degraded": result.get("degraded") if isinstance(result, dict) else None,
            },
            project_id=project.id,
        )
        _logger.info("工作流完成: project=%s status=%s graph=%s", project.id, status, result.get("graph_id"))
        try:
            from core.observability.metrics import metrics
            metrics.workflow_finished(status, req.mode or "sequential")
        except Exception:
            pass
        return {
            "project_id": project.id,
            "status": status,
            "result_keys": list(result.keys()) if isinstance(result, dict) else [],
            "graph_id": result.get("graph_id"),
            "run_id": result.get("run_id"),
        }
    except Exception as e:
        _logger.exception("工作流执行失败: %s", e)
        await ProjectManager.update_status(project.id, "failed")
        # #10 plans 状态卡 executed：崩溃时回写 failed
        try:
            from core.storage.database import get_db
            _db = await get_db()
            await _db.execute(
                "UPDATE plans SET status = 'failed' WHERE project_id = ? AND status = 'executed'",
                (project.id,),
            )
            await _db.commit()
        except Exception:
            pass
        try:
            from core.observability.metrics import metrics
            metrics.workflow_finished("failed", req.mode or "sequential")
        except Exception:
            pass
        event_bus.broadcast(
            event="error",
            data={"message": str(e), "project_id": project.id},
            project_id=project.id,
        )
        raise


async def launch_workflow(payload: Union[RunRequest, Dict[str, Any]]) -> None:
    """后台任务入口 — 确保异常时更新项目状态。"""
    req = payload if isinstance(payload, RunRequest) else RunRequest(**payload)
    project_id = req.project_id
    try:
        await execute_workflow(payload)
    except Exception as e:
        _logger.exception("后台工作流执行失败: %s", e)
        if project_id:
            try:
                from core.run.run_recovery import mark_recover_pending
                from core.storage.database import get_db
                await mark_recover_pending(project_id)
                await ProjectManager.update_status(project_id, "failed")
                # #10 plans 状态卡 executed：崩溃时回写 failed
                _db = await get_db()
                await _db.execute(
                    "UPDATE plans SET status = 'failed' WHERE project_id = ? AND status = 'executed'",
                    (project_id,),
                )
                await _db.commit()
            except Exception:
                pass
            try:
                event_bus.broadcast(
                    event="agent_state",
                    data={"state": "failed", "project_id": project_id, "message": str(e)},
                    project_id=project_id,
                )
            except Exception:
                pass


def run_params_from_request(req: RunRequest, workflow_config: Optional[dict] = None) -> dict:
    compiled = compile_protocol(
        workflow_config or {},
        default_mode=req.mode,
        default_review=req.review_mode,
    )
    return merge_run_params(
        compiled,
        req.mode,
        req.review_mode,
        req.roundtable_config,
        req.parallel_config,
    )
