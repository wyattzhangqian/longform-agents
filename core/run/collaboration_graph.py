"""CollaborationGraph — GraphRuntime 唯一调度内核 (D1/D2)"""

from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.agent.state_manager import StateManager
from core.graph.runtime import GraphRuntime, GraphExecutionResult
from core.graph.types import GraphRunConfig, GraphState, GraphStatus, WorkflowGraph
from core.logging import get_logger
from core.run.legacy_adapter import resolve_run_plan
from core.run.phase_spec import PhaseSpec
from core.run.run_context import RunContext
from core.run.template_compiler import TemplateCompiler

_logger = get_logger("run.collaboration_graph")


def graph_status_to_project(status: GraphStatus) -> str:
    return {
        GraphStatus.COMPLETED: "completed",
        GraphStatus.PAUSED: "paused",
        GraphStatus.FAILED: "failed",
        GraphStatus.CANCELLED: "failed",
    }.get(status, "failed")


class CollaborationGraph:
    """PhaseSpec → WorkflowGraph → GraphRuntime，RunContext 为唯一状态真相"""

    def __init__(
        self,
        graph,
        phase_specs: list,
        run_context: RunContext,
        mode: str = "sequential",
    ):
        self.graph = graph
        self.phase_specs = phase_specs
        self.run_context = run_context
        self.mode = mode
        self.runtime: Optional[GraphRuntime] = None
        self.last_result: Optional[GraphExecutionResult] = None

    def inject_interjection(self, interjection) -> None:
        """P0-2: 注入人类插话 —— 统一写入 run_context（A），并刷新 runtime 配置。

        修复前：路由只写 A（collab.run_context），而运行中 agent 读 B
        （GraphRuntime._live_run_ctx，独立反序列化副本），A→B 零同步导致
        运行中插话丢失、且后续 checkpoint 用 B 覆盖 DB。
        修复后：A/B 指向同一对象（见 _build_run_config / from_checkpoint），
        此处追加到 A 即对 B 可见；同时刷新 config.run_context，保证暂停后
        resume 重建 B 时不丢。
        """
        if getattr(self, "run_context", None) is None:
            _logger.warning("inject_interjection 跳过：graph 无 run_context project=%s", getattr(self, "run_context", "?"))
            return
        self.run_context.human_interjections.append(interjection)
        self.run_context.touch()
        runtime = getattr(self, "runtime", None)
        if runtime is not None and getattr(runtime, "config", None) is not None:
            # 保持 config.run_context 指向同一对象，resume 的 _init_live_run_ctx 会复用
            runtime.config.run_context = self.run_context
        # 诊断：确认注入是否落到运行中 live ctx（P0-2 验证）
        _live = getattr(runtime, "_live_run_ctx", None) if runtime else None
        _logger.info(
            "inject_interjection: ij={} run_context_has={} live_has={} same_object={}",
            getattr(interjection, "id", "?"),
            len(self.run_context.human_interjections),
            len(_live.human_interjections) if _live else -1,
            (_live is self.run_context) if _live is not None else "no-live",
        )

    @classmethod
    async def from_request(
        cls,
        project,
        task: str,
        *,
        template=None,
        agent_ids: Optional[List[str]] = None,
        mode: str = "auto",
        strategy: str = "",
        review_mode: bool = False,
        domain_id: str = "",
        roundtable_config: Optional[Dict[str, Any]] = None,
        parallel_config: Optional[Dict[str, Any]] = None,
        auto_plan: bool = False,
        use_llm_decompose: bool = True,
        dependencies_map: Optional[Dict[str, List[str]]] = None,
    ) -> "CollaborationGraph":
        specs, resolved_mode = await resolve_run_plan(
            task,
            template=template,
            agent_ids=agent_ids,
            auto_plan=auto_plan,
            use_llm_decompose=use_llm_decompose,
            domain_id=domain_id,
        )
        mode = resolved_mode or mode

        if template is not None:
            graph, specs = TemplateCompiler.compile(
                template,
                mode=mode,
                strategy=strategy,
                review_mode=review_mode,
                domain_id=domain_id,
                roundtable_config=roundtable_config,
                parallel_config=parallel_config,
            )
        else:
            ids = [s.agent_id for s in specs]
            # 有显式 dependencies_map 时重建；否则保留 resolve_run_plan / auto_plan 的富契约 specs
            if dependencies_map and ids:
                specs = PhaseSpec.from_agent_ids(ids, dependencies_map=dependencies_map)
            graph, specs = TemplateCompiler.compile(
                specs=specs,
                mode=mode,
                strategy=strategy,
                review_mode=review_mode,
                domain_id=domain_id,
                roundtable_config=roundtable_config,
                parallel_config=parallel_config,
                graph_id=f"run_{project.id}",
            )

        # PhaseSpec.label 常为 agent_id：编译后用 registry 中文名回填
        try:
            from core.agent.registry import get_registry
            reg = get_registry()
            for spec in specs:
                if spec.label and spec.label != spec.agent_id and not str(spec.label).startswith("agent_"):
                    continue
                agent_def = await reg.get(spec.agent_id)
                if agent_def and getattr(agent_def, "name", None):
                    spec.label = agent_def.name
        except Exception:
            pass

        ctx = RunContext(
            run_id=str(uuid.uuid4())[:8],
            project_id=project.id,
            task=task,
            phase_specs=[s.model_dump() for s in specs],
            config_snapshot={
                "mode": mode,
                "strategy": strategy or "pipeline",
                "review_mode": review_mode,
                "domain_id": domain_id or (getattr(project.config, "domain_id", "") or ""),
            },
        )
        _logger.info(
            "CollaborationGraph 编译: graph=%s phases=%d mode=%s",
            graph.graph_id,
            len(specs),
            mode,
        )
        return cls(graph, specs, ctx, mode=mode)

    @classmethod
    async def from_checkpoint(
        cls,
        project_id: str,
        ctx: Optional["RunContext"] = None,
    ) -> "CollaborationGraph":
        """从 RunContext checkpoint 重建（跨进程 review/resume）

        P1-10: 支持传入历史 RunContext（run_checkpoints 恢复），实现按 run 回放。
        """
        if ctx is None:
            ctx = await StateManager.load_run_context(project_id)
        if not ctx or not ctx.graph.get("graph_def"):
            raise ValueError(f"项目 {project_id} 无 Graph checkpoint，无法恢复")

        graph = WorkflowGraph.from_dict(ctx.graph["graph_def"])
        specs = [PhaseSpec.model_validate(p) for p in ctx.phase_specs]
        mode = ctx.config_snapshot.get("mode", "sequential")
        collab = cls(graph, specs, ctx, mode=mode)

        runtime = GraphRuntime(graph)
        runtime.state = GraphState.from_snapshot(ctx.graph["graph_state"])
        runtime.scheduler.restore_from_snapshot(ctx.graph["scheduler"])
        cfg_data = ctx.graph.get("graph_run_config") or {}
        runtime.config = GraphRunConfig.model_validate(cfg_data)
        # P0-2: 冷恢复也传对象引用，保证 A（collab.run_context）与 B（runtime live ctx）合一
        runtime.config.run_context = ctx
        runtime.config.project_id = project_id
        runtime.run_id = ctx.run_id or ctx.graph.get("run_id", str(uuid.uuid4())[:8])
        runtime.status = GraphStatus.PAUSED
        runtime.start_time = None

        collab.runtime = runtime
        _logger.info("CollaborationGraph 从 checkpoint 恢复: project=%s", project_id)
        return collab

    def _build_run_config(self) -> GraphRunConfig:
        return GraphRunConfig(
            project_id=self.run_context.project_id,
            checkpoint_enabled=True,
            initial_state={
                "task": self.run_context.task,
                "project_id": self.run_context.project_id,
            },
            phase_specs=[s.model_dump() for s in self.phase_specs],
            run_context=self.run_context,  # P0-2: 传对象引用（A/B 合一）
            mode=self.mode,
            review_mode=bool(self.run_context.config_snapshot.get("review_mode")),
        )

    def reset_from_phase(self, phase_id: str) -> list:
        """重置目标节点及其所有下游节点为 PENDING，保留上游输出。

        Args:
            phase_id: 要回放的起始节点 ID

        Returns:
            被重置的节点 ID 列表
        """
        target_node = self.graph.get_node(phase_id)
        if not target_node:
            raise ValueError(f"Node {phase_id} not found in graph")

        # 收集目标 + 所有下游节点（BFS 拓扑遍历）
        nodes_to_reset = self._collect_downstream(phase_id, include_self=True)

        # 重置 RunContext 中的状态
        for node_id in nodes_to_reset:
            if node_id in self.run_context.agent_outputs:
                del self.run_context.agent_outputs[node_id]
            if node_id in self.run_context.completed_phase_ids:
                self.run_context.completed_phase_ids.remove(node_id)

        # 重置相关质量记录
        self.run_context.quality_records = [
            r for r in self.run_context.quality_records
            if r.phase_id not in nodes_to_reset
        ]

        # 重置控制状态
        self.run_context.control.pending_human = False
        self.run_context.control.pause_reason = ""
        self.run_context.control.pending_decision_id = None
        self.run_context.control.termination_reason = ""

        # 如果 runtime 已存在，同步重置 GraphState
        if self.runtime and self.runtime.state:
            state = self.runtime.state
            for node_id in nodes_to_reset:
                if node_id in state.node_outputs:
                    del state.node_outputs[node_id]
                if node_id in state.completed_nodes:
                    state.completed_nodes.remove(node_id)
                if node_id in state.failed_nodes:
                    state.failed_nodes.remove(node_id)
            state.termination_reason = None
            state.termination_message = ""
            state.current_node_id = phase_id

            # 重置调度器
            self.runtime.scheduler.completed.difference_update(nodes_to_reset)
            self.runtime.scheduler.failed.difference_update(nodes_to_reset)
            self.runtime.scheduler.ready.clear()
            self.runtime.scheduler.running.clear()
            # 将目标节点设为就绪
            self.runtime.scheduler.ready.add(phase_id)
            for nid in nodes_to_reset:
                self.runtime.scheduler.pending[nid] = 0

            self.runtime.status = GraphStatus.READY

        self.run_context.touch()
        _logger.info(
            "reset_from_phase: phase=%s reset_nodes=%s",
            phase_id, sorted(nodes_to_reset),
        )
        return sorted(nodes_to_reset)

    def _collect_downstream(self, node_id: str, include_self: bool = False) -> set:
        """BFS 收集所有下游依赖节点"""
        from core.graph.types import START_NODE_ID, END_NODE_ID

        visited = set()
        queue = [node_id] if include_self else list(self.graph.get_successors(node_id))

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            # 跳过隐式 START/END 节点
            if current in (START_NODE_ID, END_NODE_ID):
                continue
            visited.add(current)
            queue.extend(self.graph.get_successors(current))

        return visited

    async def execute(self) -> Tuple[Dict[str, Any], str]:
        from core.gateway.budget import reset_budget, set_budget_scope

        reset_budget(self.run_context.project_id)
        set_budget_scope(self.run_context.project_id)

        config = self._build_run_config()
        self.runtime = GraphRuntime(self.graph)
        result = await self.runtime.execute(run_config=config)
        return self._finalize(result)

    async def resume(self, user_input: Optional[Any] = None) -> Tuple[Dict[str, Any], str]:
        """审核 / 决策 / 人工门闩后继续执行"""
        if not self.runtime or self.runtime.status != GraphStatus.PAUSED:
            raise RuntimeError("Graph 未处于 PAUSED，无法 resume")

        # 恢复执行沿用同一预算 scope（不清零，跨暂停累计）
        from core.gateway.budget import set_budget_scope
        set_budget_scope(self.run_context.project_id)

        self.run_context.control.pending_human = False
        self.run_context.control.pause_reason = ""
        self.run_context.control.pending_decision_id = None
        result = await self.runtime.resume(user_input=user_input)
        return self._finalize(result)

    async def replay_from(
        self,
        from_phase_id: str,
        modifications: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """从指定 phase 回溯重放：重置目标+下游节点，可选注入修改指令，重新执行。

        Args:
            from_phase_id: 回放起始节点 ID
            modifications: 可选的修改指令（revision mode）

        Returns:
            (payload, project_status) — 同 execute()
        """
        # 1. 重置目标及下游节点
        reset_nodes = self.reset_from_phase(from_phase_id)

        # 2. 注入修改上下文（如果有）
        if modifications:
            self.run_context.inject_phase_context(from_phase_id, modifications)

        # 3. 发出 replay_started SSE 事件
        from core.run.events import ReplayStartedEvent
        from core.events import event_bus

        event = ReplayStartedEvent(
            run_id=self.run_context.run_id,
            from_phase_id=from_phase_id,
            phases_to_rerun=reset_nodes,
        )
        event_bus.broadcast(
            event="replay_started",
            data=event.model_dump(),
            project_id=self.run_context.project_id or None,
        )

        _logger.info(
            "replay_from: project=%s from_phase=%s reset=%s modifications=%s",
            self.run_context.project_id,
            from_phase_id,
            reset_nodes,
            bool(modifications),
        )

        # 4. 构建 runtime 并执行
        from core.gateway.budget import reset_budget, set_budget_scope

        reset_budget(self.run_context.project_id)
        set_budget_scope(self.run_context.project_id)

        config = self._build_run_config()
        # 确保 run_context 中最新的 revision context 被传入（P0-2: 传对象引用）
        config.run_context = self.run_context

        # 如果 runtime 不存在，创建新的
        if not self.runtime:
            self.runtime = GraphRuntime(self.graph)

        # 重新初始化调度器，从目标节点开始
        self.runtime.scheduler.initialize([from_phase_id])

        result = await self.runtime.execute(
            run_config=config,
        )
        return self._finalize(result)

    def _finalize(self, result: GraphExecutionResult) -> Tuple[Dict[str, Any], str]:
        self.last_result = result

        if result.state:
            snap = result.state.snapshot()
            self.run_context.sync_from_graph_state(
                snap.get("values", {}),
                {
                    "run_id": result.run_id,
                    "status": result.status.value,
                    "completed_nodes": list(result.state.completed_nodes),
                    "current_node": result.state.current_node_id,
                },
            )

        if result.status == GraphStatus.PAUSED:
            self.run_context.control.pending_human = True
        elif result.status == GraphStatus.COMPLETED:
            self.run_context.control.pending_human = False
            # Run 完成后触发记忆提取（fire-and-forget）
            self._trigger_post_run_hooks(result)

        # 状态诚实：completed 不等于成功——统计失败节点/error 产物，区分 completed/partial/failed
        if result.status == GraphStatus.COMPLETED:
            failed = list(getattr(result.state, "failed_nodes", None) or [])
            # AgentOutput.error 非空或 result.status == error 的节点（修复 3 后 timeout/exception 带 error）
            error_outputs = []
            for aid, out in (self.run_context.agent_outputs or {}).items():
                is_err = bool(getattr(out, "error", None))
                r = getattr(out, "result", None) or {}
                if not is_err and isinstance(r, dict) and r.get("status") == "error":
                    is_err = True
                if is_err:
                    error_outputs.append(aid)
            n_fail = len(set(failed) | set(error_outputs))
            n_total = max(len(self.phase_specs), 1)
            if n_fail == 0:
                project_status = "completed"
            elif n_fail < n_total:
                project_status = "partial"   # 部分成功
            else:
                project_status = "failed"    # 全部失败
        else:
            project_status = graph_status_to_project(result.status)
        payload = {
            "run_id": result.run_id,
            "status": result.status.value,
            "graph_id": self.graph.graph_id,
            "iterations": result.iterations,
            "duration_seconds": result.duration_seconds,
            "phase_ids": [s.id for s in self.phase_specs],
            "run_context": self.run_context.model_dump(),
        }
        return payload, project_status

    def _trigger_post_run_hooks(self, result: GraphExecutionResult) -> None:
        """Run 完成后触发 PostRunHookManager（fire-and-forget，不阻塞）"""
        try:
            from core.run.post_run_hooks import PostRunHookManager
            from core.memory.cross_run_pipeline import CrossRunMemoryPipeline

            # 构建 pipeline（LLM 客户端可选）
            llm_client = None
            try:
                from tools.llm_client import LLMClient
                llm_client = LLMClient()
            except Exception as e:
                _logger.warning("LLM 客户端可选加载失败，能力降级: {}", e)

            pipeline = CrossRunMemoryPipeline(llm_client=llm_client)

            # 构建 SelfOptimizer（闭环进化：分析→自动应用→持久化）
            optimizer = None
            try:
                from core.agent.self_optimizer import SelfOptimizer
                optimizer = SelfOptimizer(llm_client=llm_client)
            except Exception as e:
                _logger.warning("SelfOptimizer 可选加载失败，进化降级: {}", e)

            hook_manager = PostRunHookManager(memory_pipeline=pipeline, optimizer=optimizer)

            # 收集 run 数据
            agent_id = self.run_context.agent_id or (
                self.phase_specs[0].agent_id if self.phase_specs else ""
            )
            agent_name = getattr(self, "_agent_name", agent_id)
            task = self.run_context.task or ""
            phases_summary = ", ".join(
                s.id for s in self.phase_specs if s.id in (self.run_context.completed_phase_ids or [])
            ) or f"{len(self.run_context.completed_phase_ids or [])} 个阶段"

            # 从 run_context.values 提取质量事件
            values = self.run_context.values or {}
            quality_events = []
            if isinstance(values, dict):
                qr = values.get("quality_report")
                if qr and isinstance(qr, dict):
                    quality_events.append({"type": "quality_check", "report": qr})

            # 提取产出特征（供 CrossRunMemory 跨 run 学习）
            output_characteristics = ""
            for aid, out in (self.run_context.agent_outputs or {}).items():
                preview = str(out.result if hasattr(out, "result") else out)[:300] if out else ""
                if preview.strip():
                    output_characteristics += f"[{aid}]: {preview}\n"
            output_characteristics = output_characteristics[:2000]

            # 提取人工反馈（从 revision_contexts 等）
            human_feedback = []
            revision_ctx = getattr(self.run_context, "_revision_contexts", None)
            if revision_ctx:
                for phase_id, ctx in revision_ctx.items():
                    instructions = ctx.get("instructions", "") if isinstance(ctx, dict) else ""
                    if instructions:
                        human_feedback.append(f"Phase '{phase_id}': {instructions}")

            # 触发（item8：改用强引用 fire-and-forget，避免 ensure_future 孤儿任务
            # 被 GC 回收导致 cross_run 提取/进化永不执行——与被跑飞 run 的同类 bug）
            try:
                hook_manager.on_run_completed_fire_and_forget(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    run_id=result.run_id,
                    task=task,
                    phases_summary=phases_summary,
                    avg_quality_score=values.get("quality_score", 0.0) if isinstance(values, dict) else 0.0,
                    run_context=self.run_context.model_dump(),
                    quality_events=quality_events,
                    human_feedback=human_feedback,
                    output_characteristics=output_characteristics,
                )

                # 进化检查：run 完成后检查 Agent 是否达到进化阈值，触发 SelfOptimizer
                async def _check_evolution():
                    try:
                        from core.evolution import EvolutionEngine
                        engine = EvolutionEngine()
                        should = await engine.should_evolve(agent_id)
                        if should and optimizer:
                            # 拉取累积的成功案例（而非仅当前 run），触发 SelfOptimizer 分析
                            accumulated = await engine.get_success_cases(agent_id, limit=20)
                            cases = [
                                {"quality_score": c.quality_score, "task": c.prompt_used or "",
                                 "iterations": c.iterations, "duration_seconds": c.duration_seconds}
                                for c in accumulated
                            ]
                            report = await optimizer.analyze(
                                agent_id=agent_id,
                                success_cases=cases,
                                decision_logs=[],
                                llm_client=llm_client,
                            )
                            if report.suggestions:
                                # 获取 agent_definition（auto_apply 需要）
                                from core.agent.registry import get_registry
                                _registry = get_registry()
                                agent_definition = await _registry.get(agent_id)
                                if agent_definition:
                                    applied = await optimizer.auto_apply(
                                        report, agent_definition, llm_client
                                    )
                                else:
                                    applied = []
                                self._emit("evolution_applied", {
                                    "agent_id": agent_id,
                                    "total_suggestions": len(report.suggestions),
                                    "applied": len(applied),
                                    "message": f"Agent {agent_name} 进化优化完成: {len(applied)}/{len(report.suggestions)} 条建议已应用",
                                })
                                # === E7 新增：SSE 进化事件（前端实时通知）===
                                if applied:
                                    try:
                                        from core.events import broadcast_event
                                        await broadcast_event(self.run_context.project_id, "agent_evolved", {
                                            "agent_id": agent_id,
                                            "agent_name": agent_name,
                                            "suggestions_applied": len(applied),
                                            "descriptions": [s.description for s in applied[:3] if getattr(s, 'description', '')],
                                        })
                                    except Exception:
                                        pass
                    except Exception as e:
                        _logger.debug("进化检查失败（非致命）: %s", e)

                from core.task_tracker import create_task as _tracked_task2
                _tracked_task2(_check_evolution(), name="evolution_check")  # 强引用防 GC
            except RuntimeError:
                # 没有 running loop（非异步上下文），同步执行
                loop = asyncio.new_event_loop()
                loop.run_until_complete(hook_manager.on_run_completed(
                    agent_id=agent_id, agent_name=agent_name, run_id=result.run_id,
                    task=task, phases_summary=phases_summary,
                    avg_quality_score=values.get("quality_score", 0.0) if isinstance(values, dict) else 0.0,
                    run_context=self.run_context.model_dump(),
                    quality_events=quality_events, human_feedback=human_feedback,
                    output_characteristics=str(values.get("final_output", ""))[:500] if isinstance(values, dict) else "",
                ))
                loop.close()
        except Exception as e:
            _logger.warning("Post-run hook 触发失败（非致命）: %s", e)
