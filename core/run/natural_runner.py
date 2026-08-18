"""natural_runner — 自然语言启动协作的服务层（P1-8）

把 /api/run/natural 系列的「规划 + Agent 匹配 + 写 plans + 排程执行」从
路由下沉到 core，路由保持薄封装（只做参数解析 + 调服务 + 返回）。

对外接口：
- plan_natural(task, save_draft=True)   → 只规划不执行，可选存 draft plan
- start_natural_run(task, project_id)   → 规划 → 匹配 → ensure project → 建图 → 排程执行
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from core.logging import get_logger

_logger = get_logger("run.natural_runner")


def _infer_total_units(task: str) -> int:
    """从自然语言任务推断长篇章节数（骨架接入用）。

    优先正则解析"共N章/写N章/N章/N回"等；解析不到且含长篇关键词时默认 8 章，
    否则返回 0（单篇，不走骨架）。骨架只在 total_units > 1 时接入。
    """
    import re

    t = task or ""
    m = re.search(r"(?:共|写|作|写满)?\s*(\d{1,3})\s*(?:章|回|话|篇)", t)
    if m:
        return max(1, min(int(m.group(1)), 50))
    # 长内容关键词兜底
    if re.search(r"小说|长篇|连载|章回|剧本|多章|连续剧", t):
        return 8
    return 0


def _infer_mode_from_plan(plan: dict) -> str:
    """从 dag_planner 输出推断执行模式（sequential / dag）"""
    phases = plan.get("phases", [])
    if len(phases) <= 1:
        return "sequential"
    # 检查是否所有阶段都线性依赖前一个
    for i, phase in enumerate(phases[1:], 1):
        deps = phase.get("dependencies", [])
        if not deps:
            return "dag"  # 有无依赖的非首阶段 → 可并行
        prev_id = phases[i - 1].get("phase_id", "")
        if deps != [prev_id]:
            return "dag"  # 依赖不是"仅依赖前一个" → 非线性
    return "sequential"


async def _agent_domain_from_id_safe(agent_id: str) -> str:
    """取 agent 的领域（带异常保护）。"""
    try:
        from core.orchestration.planner import _agent_domain_from_id
        return (await _agent_domain_from_id(agent_id) or "").replace("platform:", "")
    except Exception:
        return ""


async def _agent_domain_matches(agent_id: str, domain_id: str) -> bool:
    """agent 是否属于该领域（精确，或同族前缀 fallback，如 comic_static/comic_drama 都算 comic）。"""
    if not domain_id:
        return False
    adom = await _agent_domain_from_id_safe(agent_id)
    if not adom:
        return False
    rdom = domain_id.replace("platform:", "")
    if adom == rdom:
        return True
    # 同族前缀：comic_static vs comic_drama → comic；web_novel 单段精确即可
    return adom.split("_")[0] == rdom.split("_")[0] and "_" in adom


async def _agent_domain_exact(agent_id: str, domain_id: str) -> bool:
    """agent 领域与目标领域精确一致。"""
    if not domain_id:
        return False
    adom = await _agent_domain_from_id_safe(agent_id)
    return bool(adom) and adom == domain_id.replace("platform:", "")


async def _match_agents(plan: dict, domain_id: str = "") -> List[Dict[str, Any]]:
    """按能力匹配 Agent，返回带 agent_id / agent_name 的 phases 列表。

    2026-08-10 修复：natural 路径此前只认 11 个通用能力，domain 专用 agent 匹配不到。
    现在按领域偏好选 agent：
      - 识别出领域 → 优先该领域的 agent_tpl_*（domain 专用模板）
      - 无领域 → 优先 builtin_/fixture_（通用）
    """
    from core.agent.registry import get_registry
    from core.discovery.capability import CapabilityDiscovery

    registry = get_registry()
    active_agents = await registry.list({"status": "active"})
    discovery = CapabilityDiscovery(active_agents)

    phases_with_agents: List[Dict[str, Any]] = []
    for phase in plan.get("phases", []):
        caps = phase.get("required_capabilities", ["general"])
        hits = discovery.discover(required_capabilities=caps, limit=8)
        agent_id = ""
        if hits:
            if domain_id:
                # 领域偏好：优先领域**精确**匹配的 agent_tpl_*，再同族前缀，再最高分
                for h in hits:
                    if h["id"].startswith("agent_tpl_") and await _agent_domain_exact(h["id"], domain_id):
                        agent_id = h["id"]
                        break
                if not agent_id:
                    for h in hits:
                        if h["id"].startswith("agent_tpl_") and await _agent_domain_matches(h["id"], domain_id):
                            agent_id = h["id"]
                            break
            else:
                # 无领域 → 优先通用 builtin/fixture
                for h in hits:
                    if h["id"].startswith("builtin_") or h["id"].startswith("fixture_"):
                        agent_id = h["id"]
                        break
            if not agent_id:
                agent_id = hits[0]["id"]  # 默认最高分
        if not agent_id:
            agent_id = "fixture_agent_general"
        agent_def = await registry.get(agent_id) if agent_id else None
        phases_with_agents.append({
            **phase,
            "agent_id": agent_id,
            "agent_name": getattr(agent_def, "name", agent_id) if agent_def else agent_id,
        })
    return phases_with_agents


async def _gather_domain_capabilities(domain_id: str) -> Optional[List[str]]:
    """收集领域能力池（模板系统 capability_tags，按领域过滤）。

    2026-08-10 fix：_gather_capabilities 内部用去掉 platform: 的 tmpl_domain 与
    domain_id 比较，所以这里先去掉前缀，否则模板分支永远匹配不上（全落 agent 兜底）。

    行为约定：未识别出领域（domain_id 空）时返回 None → dag_planner 用默认 11 个
    通用能力（保持旧行为，避免 LLM 词汇表变宽后为通用任务选到 domain agent）。
    """
    if not domain_id:
        return None
    try:
        from core.orchestration.planner import _gather_capabilities
        from core.agent.registry import get_registry
        agents = await get_registry().list({"status": "active"})
        normalized = (domain_id or "").replace("platform:", "")
        caps = await _gather_capabilities(agents, normalized)
        if caps:
            return caps
    except Exception as e:
        _logger.debug("natural 能力池收集失败（用默认通用能力）: %s", e)
    return None


async def plan_natural(
    task: str,
    save_draft: bool = True,
    capabilities: Optional[List[str]] = None,
    domain_id: str = "",
) -> dict:
    """自然语言 → DAG 规划 + Agent 匹配。

    2026-08-10 修复：plan_natural 内部做领域识别 + 能力池收集（/plan 预览和 /run 执行
    两条路径共用），使 natural 能匹配 domain 专用 agent（漫画/音乐等）。之前只传
    11 个通用能力，domain agent 永远匹配不到。结果返回 domain_id。
    """
    # 领域识别（未显式指定时）
    if not domain_id:
        try:
            from api.routes.plan import _match_domain_llm
            matched_id, _, confidence, _ = await _match_domain_llm(task)
            if matched_id:
                domain_id = matched_id
                _logger.info(f"natural LLM 识别 domain_id={domain_id} ({confidence:.2f})")
        except Exception as e:
            _logger.warning("natural domain 识别失败: %s", e)

    # 能力池收集（未显式传入时）
    if not capabilities:
        capabilities = await _gather_domain_capabilities(domain_id)
        if capabilities:
            _logger.info(
                f"natural 领域能力池: {len(capabilities)} 个（domain={domain_id or '全部'}）",
            )

    from core.run.dag_planner import plan_dag_from_natural_language

    plan = await plan_dag_from_natural_language(task, capabilities=capabilities)
    phases_with_agents = await _match_agents(plan, domain_id=domain_id)
    mode = _infer_mode_from_plan(plan)

    result: Dict[str, Any] = {
        "plan": plan,
        "phases": phases_with_agents,
        "mode": mode,
        "plan_id": "",
        "domain_id": domain_id,
    }

    if save_draft:
        from core.storage.database import get_db

        db = await get_db()
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        await db.execute(
            """INSERT INTO plans (id, task, domain_id, mode, status, phases, reasoning)
               VALUES (?, ?, ?, ?, 'draft', ?, ?)""",
            (
                plan_id,
                task,
                domain_id,  # 2026-08-10 fix：识别出的领域存入 draft（之前写死 ''）
                mode,
                json.dumps(phases_with_agents, ensure_ascii=False),
                json.dumps(plan.get("collaboration_config", {}), ensure_ascii=False),
            ),
        )
        await db.commit()
        result["plan_id"] = plan_id

    return result


async def start_natural_run(task: str, project_id: Optional[str] = None) -> dict:
    """一句话启动协作：自然语言 → DAG → 匹配 → 建项目 → 建图 → 排程后台执行。

    返回 {project_id, status, plan, agent_ids, mode, message}。
    """
    from core.agent.types import ProjectConfig
    from core.project.manager import ProjectManager
    from core.run.collaboration_graph import CollaborationGraph
    from core.task_tracker import create_task
    from core.run.background import background_safe_execute

    project_id = project_id or f"project_{int(time.time())}"

    # 领域识别 + 能力池由 plan_natural 内部完成（2026-08-10）：/plan 预览与 /run 执行
    # 共用，natural 能匹配 domain 专用 agent。domain_id 从结果取。
    plan_result = await plan_natural(task, save_draft=False)
    plan = plan_result["plan"]
    phases_with_agents = plan_result["phases"]
    mode = plan_result["mode"]
    domain_id = plan_result.get("domain_id", "")
    agent_ids = [ph["agent_id"] for ph in phases_with_agents]

    # 确保 project 存在
    project = await ProjectManager.get(project_id)
    if not project:
        project = await ProjectManager.create(
            name=task[:60] + ("…" if len(task) > 60 else ""),
            project_id=project_id,
            config=ProjectConfig(
                task_description=task,
                extra={"source": "natural_language"},
            ),
        )

    # dependencies_map
    deps_map: Dict[str, list] = {}
    for phase in plan.get("phases", []):
        deps_map[phase.get("phase_id", "")] = phase.get("dependencies", [])

    # 构建 CollaborationGraph
    collab_graph = await CollaborationGraph.from_request(
        project,
        task,
        agent_ids=agent_ids,
        mode=mode,
        auto_plan=False,
        domain_id=domain_id,
        use_llm_decompose=False,
        dependencies_map=deps_map,
    )

    # ==== 长内容骨架接入（对齐 runner.py:190-255）====
    # natural 路径此前绕过骨架系统 → 多 writer 各写各的（书名/人物/世界观漂移）。
    # 接上骨架后，每章 writer 通过 context_builder 拿到 L4 全书设定 + L1 本章 unit_spec。
    total_units = _infer_total_units(task)
    skeleton = None
    if total_units > 1:
        try:
            from core.skeleton.integration import (
                plan_skeleton_if_needed,
                expand_phases_with_skeleton,
            )
            from core.run.template_compiler import TemplateCompiler as _TC
            from tools.llm_client import LLMClient

            skeleton = await plan_skeleton_if_needed(
                project_id=project_id,
                task=task,
                content_type="novel",
                total_units=total_units,
                llm_client=LLMClient(),
            )

            # 骨架驱动 Phase 扩展：把"写作"phase 扩成 N 个章节 phase（每章独立 unit）
            if skeleton and total_units > 1:
                expanded_specs = expand_phases_with_skeleton(
                    list(collab_graph.phase_specs), skeleton
                )
                if len(expanded_specs) != len(collab_graph.phase_specs):
                    new_graph, new_specs = _TC.compile(
                        specs=expanded_specs,
                        mode=collab_graph.mode,
                        graph_id=f"run_{project_id}",
                    )
                    collab_graph.graph = new_graph
                    collab_graph.phase_specs = new_specs
                    _logger.info(
                        "natural 长内容 Phase 扩展: %d → %d phases（%d 单元）",
                        len(collab_graph.phase_specs), len(new_specs), total_units,
                    )
        except Exception as e:
            _logger.warning("natural 骨架规划失败（非致命，无骨架执行）: %s", e)

    # 注册 live graph：interject 端点靠 graph_registry 找运行中 run_context
    # （natural 路径此前未注册 → 运行中插话恒 404 "项目尚无运行状态"）
    from core.run.graph_registry import set_collaboration_graph
    set_collaboration_graph(project_id, collab_graph)
    # 项目状态 → running：否则执行全程 status 停在 idle，前端显示"空闲"（后台真实执行）
    from core.project.manager import ProjectManager as _PM
    try:
        await _PM.update_status(project_id, "running")
    except Exception as _e:
        _logger.warning("设置项目 running 状态失败（非致命）: %s", _e)

    # 骨架注入 RunContext（触发 context_builder 的 L4/L1 注入）
    if skeleton and total_units > 1:
        collab_graph.run_context.skeleton = skeleton.model_dump()
        collab_graph.run_context.content_type = "novel"
        collab_graph.run_context.total_units = total_units
        collab_graph.run_context.current_unit = 1
        try:
            from core.events import broadcast_event
            await broadcast_event(project_id, "skeleton_ready", {"skeleton": skeleton.model_dump()})
        except Exception:
            pass  # 事件广播失败不阻塞执行

    # 异步执行（强引用跟踪 task，防 GC 静默中止）
    create_task(
        background_safe_execute(
            project_id, execute_natural_collab, collab_graph, project_id,
        ),
        name=f"exec_natural_{project_id}",
    )

    return {
        "project_id": project_id,
        "status": "started",
        "plan": plan,
        "agent_ids": agent_ids,
        "mode": mode,
        "message": f"已规划 {len(plan.get('phases', []))} 个阶段，正在执行...",
    }


async def execute_natural_collab(collab_graph, project_id: str) -> None:
    """执行自然语言生成的 collaboration graph"""
    from core.events import event_bus
    from core.project.manager import ProjectManager as _PM
    from core.run.graph_registry import remove_collaboration_graph
    from core.run.run_lock import run_lock

    async with run_lock(project_id):
        _result, status = await collab_graph.execute()

    # PR-11：运行结束后收集 feedback（质量门结果回流，用于后续 Agent 选择优化）
    try:
        from core.orchestration.feedback import record_run_feedback, RunFeedback, PhaseFeedback
        _rc = collab_graph.run_context
        if _rc and _rc.quality_records:
            phase_results = [
                PhaseFeedback(
                    phase_id=qr.phase_id,
                    agent_id=qr.agent_id,
                    status="success" if qr.passed else "failed",
                    quality_gate_passed=qr.passed,
                )
                for qr in _rc.quality_records
            ]
            if phase_results:
                await record_run_feedback(RunFeedback(
                    plan_id=_rc.config_snapshot.get("plan_id", ""),
                    revision=int(_rc.config_snapshot.get("plan_revision", 1)),
                    run_id=_rc.run_id,
                    phase_results=phase_results,
                ))
    except Exception as _fe:
        _logger.warning("feedback 记录失败（非致命）: %s", _fe)

    await _PM.update_status(project_id, status)
    remove_collaboration_graph(project_id)
    event_bus.broadcast(
        event="workflow_complete",
        data={"project_id": project_id, "status": status},
        project_id=project_id,
    )
