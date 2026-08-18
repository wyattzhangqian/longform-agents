"""BaseAgent — Agent 运行时基类 (Platform-Core v3)

纯通用层，无业务耦合。集成 ContextBuilder、ToolExecutor、MemoryManager。
"""

from __future__ import annotations
import json
import os
from typing import Any, Optional, List
from core.agent.types import AgentDefinition
from core.agent.context_builder import ContextBuilder
from core.agent.tools import ToolExecutor, ToolRegistry
from core.agent.memory import MemoryManager
from core.agent.state import WorkflowState
from core.run.run_context import RunContext
from core.logging import get_logger
from tools.llm_client import LLMClient

_logger = get_logger("agent")

DEFAULT_MAX_TOOL_ITERATIONS = 8


class BaseAgent:
    """Agent 运行时基类 — ContextBuilder + 可选 Tool 循环 + Memory"""

    def __init__(self, definition: AgentDefinition):
        self.definition = definition
        self.id = definition.id
        self.name = definition.name
        self.emoji = definition.emoji
        self.role = definition.role or definition.name
        self.model = definition.model
        self.temperature = definition.temperature
        self.max_tokens = definition.max_tokens
        self.system_prompt = definition.system_prompt
        self.bus = None
        self.output_dir = ""
        self.config: dict = {}

    def plug(self, bus, output_dir: str = "", config: dict = None):
        """接入通信总线和配置"""
        self.bus = bus
        self.output_dir = output_dir
        self.config = config or {}

    def _use_tools(self) -> bool:
        if self.config.get("use_tools") is True:
            return True
        if self.config.get("use_tools") is False:
            return False
        # 配了多模态模型就自动启用工具循环（图片/视频/音乐）
        mp = self.definition.model_profile
        if mp.image or mp.video or mp.music:
            return True
        return bool(self.definition.tools) or bool(self.definition.skill_ids) or bool(self.definition.tool_ids)

    async def _resolve_capabilities(self) -> dict:
        from core.capabilities.resolver import CapabilityResolver
        return await CapabilityResolver.expand(self.definition)

    def _tool_execution_context(self) -> "ToolExecutionContext":
        from core.agent.tool_execution import ToolExecutionContext
        from core.events import event_bus
        from core.observability.trace import get_trace_id

        project_id = getattr(self.bus, "project_id", "") if self.bus else ""

        def emit(event: str, data: dict) -> None:
            if project_id:
                event_bus.broadcast(event=event, data=data, project_id=project_id)

        return ToolExecutionContext(
            project_id=project_id,
            agent_id=self.id,
            trace_id=get_trace_id() or "",
            run_id=getattr(self, "_current_run_id", "") or "",
            emit=emit,
        )

    def _use_memory(self) -> bool:
        mc = self.definition.memory_config
        return mc.episodic.get("enabled", True) if mc else True

    def _workspace_file_manifest(self, limit: int = 20) -> str:
        """工作区真实文件清单 — 注入 prompt，杜绝 Agent 凭空猜文件名

        file_read 的 path 以工作区根为基准，直接使用清单中的文件名即可。
        """
        from pathlib import Path

        from core.agent.tools import ToolRegistry

        rel = ToolRegistry._normalize_output_dir(self.output_dir or "")
        base = ToolRegistry.OUTPUTS_DIR / rel if rel else ToolRegistry.OUTPUTS_DIR
        try:
            if not base.exists():
                return ""
            files = []
            for f in sorted(base.rglob("*")):
                if not f.is_file() or f.name.startswith("."):
                    continue
                try:
                    rel_name = f.relative_to(base).as_posix()
                except ValueError:
                    continue
                files.append(f"- {rel_name}（{f.stat().st_size} 字节）")
                if len(files) >= limit:
                    break
            if not files:
                return (
                    "## 工作区文件清单\n当前工作区为空，没有任何文件。"
                    "**不要尝试 file_read 任何文件**；如需产出文件请用 file_write。"
                )
            return (
                "## 工作区文件清单（真实存在，file_read 的 path 直接用以下文件名）\n"
                + "\n".join(files)
                + "\n\n**只允许读取上述清单中的文件，不要凭空猜测其他文件名。**"
            )
        except Exception:
            return ""

    async def _llm_call(self, prompt: str, system_prompt: str = None) -> str:
        """LLM 调用 — 内部流式，旁路推 SSE，外部仍返回完整字符串"""
        import os
        from core.models.router import ModelRouter
        from core.agent.stream_buffer import _TokenBuffer

        sp = system_prompt or self.system_prompt
        cfg = ModelRouter.resolve_agent(self.definition, "llm")
        force_model = (self.config or {}).get("force_model")
        if force_model and isinstance(cfg, dict):
            cfg = {**cfg, "model": force_model}
        client = LLMClient(cfg)

        # 判断是否启用流式推送（可通过配置开关）
        enable_stream = os.getenv("ENABLE_STREAM_OUTPUT", "true").lower() == "true"
        project_id = getattr(self, "_current_project_id", None)

        if not enable_stream or not project_id:
            return await client.chat(prompt, system_prompt=sp)

        # 流式调用 + 旁路推送 SSE
        from core.events import event_bus

        full_response = ""
        buffer = _TokenBuffer(flush_interval=0.1, max_size=30)

        _agent_name = self.definition.name

        async def _flush_fn(tokens: str) -> None:
            event_bus.broadcast(
                event="agent_token_stream",
                data={
                    "agent_id": self.definition.id,
                    "agent_name": _agent_name,
                    "tokens": tokens,
                },
                project_id=project_id,
            )

        async for chunk in client.chat_stream(prompt, system_prompt=sp):
            # 推理模型输出 __REASONING__: 前缀的思维链 token
            if chunk.startswith("__REASONING__:"):
                reasoning_token = chunk[len("__REASONING__:"):]
                if reasoning_token:
                    event_bus.broadcast(
                        event="agent_thinking",
                        data={
                            "agent_id": self.definition.id,
                            "agent_name": _agent_name,
                            "reasoning_content": reasoning_token,
                        },
                        project_id=project_id,
                    )
            else:
                full_response += chunk
                await buffer.add(chunk, _flush_fn)

        # flush 剩余 buffer
        await buffer.flush(_flush_fn)

        # 推送完成事件
        event_bus.broadcast(
            event="agent_complete",
            data={
                "agent_id": self.definition.id,
                "agent_name": _agent_name,
                "result_length": len(full_response),
            },
            project_id=project_id,
        )

        return full_response

    async def _llm_call_messages(self, messages: List[dict], tools: List[dict] = None, tool_calls_sink: list = None) -> str:
        """LLM 多消息调用 — 内部流式旁路推送 token（R31 补全）

        可选 function calling：传 tools（OpenAI 格式）时，LLM 返回的结构化 tool_calls
        会累积进 tool_calls_sink（若提供），不再依赖文本格式解析。
        """
        from core.models.router import ModelRouter
        from core.events import event_bus
        from core.agent.stream_buffer import _TokenBuffer

        cfg = ModelRouter.resolve_agent(self.definition, "llm")
        if cfg is None or not isinstance(cfg, dict):
            import traceback
            _logger.error(
                "ModelRouter.resolve_agent 返回 None 或非 dict: agent=%s cfg=%s\n%s",
                self.definition.id, cfg, traceback.format_exc(),
            )
            cfg = {}
        force_model = (self.config or {}).get("force_model")
        if force_model:
            cfg = {**cfg, "model": force_model}
        client = LLMClient(cfg)

        enable_stream = os.getenv("ENABLE_STREAM_OUTPUT", "true").lower() == "true"
        project_id = getattr(self, '_current_project_id', None)

        if not enable_stream or not project_id:
            if tools:
                # 非流式 function calling：tool_calls 写入 sink（统一为 {id,name,args}）
                r = await client.chat_with_tools(messages, tools=tools)
                if tool_calls_sink is not None:
                    for tc in (r.get("tool_calls") or []):
                        tool_calls_sink.append({"id": tc.get("id", ""), "name": tc.get("name", ""), "args": tc.get("args") or {}})
                return r.get("content") or ""
            return await client.chat_messages(messages)

        # 流式 + 旁路 SSE 推送（与 _llm_call 保持一致）
        # 注意：必须用 async def 而非 lambda——lambda 会返回 broadcast() 的返回值（None），
        # 而 _TokenBuffer.flush 内部会 await flush_fn(...)，await None 会抛出 TypeError。
        agent_id = self.definition.id
        agent_name = self.definition.display_name if hasattr(self.definition, 'display_name') else self.definition.name

        async def _flush_fn(tokens: str) -> None:
            event_bus.broadcast(
                event="agent_token_stream",
                data={
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "tokens": tokens,
                },
                project_id=project_id,
            )

        full_response = ""
        buffer = _TokenBuffer(flush_interval=0.1, max_size=30)

        async def _consume(stream_tools, stream_sink) -> None:
            nonlocal full_response
            async for chunk in client.chat_messages_stream(messages, tools=stream_tools, tool_calls_sink=stream_sink):
                # 分离推理思维链 token
                if chunk.startswith("__REASONING__:"):
                    reasoning_token = chunk[len("__REASONING__:"):]
                    if reasoning_token:
                        event_bus.broadcast(
                            event="agent_thinking",
                            data={"agent_id": agent_id, "agent_name": agent_name, "reasoning_content": reasoning_token},
                            project_id=project_id,
                        )
                else:
                    full_response += chunk
                    await buffer.add(chunk, _flush_fn)

        try:
            await _consume(tools, tool_calls_sink)
        except RuntimeError as e:
            # 模型不支持 function calling（HTTP 400）时，回退纯文本流式（后续走文本解析兜底）
            if tools and "400" in str(e):
                _logger.warning("Agent %s 模型不支持 tools，回退纯文本流式: %s", agent_id, str(e)[:120])
                full_response = ""
                if tool_calls_sink is not None:
                    tool_calls_sink.clear()
                await _consume(None, None)
            else:
                raise

        await buffer.flush(_flush_fn)

        return full_response

    @staticmethod
    def _assemble_tool_calls(sink: list) -> List[dict]:
        """把 tool_calls_sink 统一组装为 [{id,name,args(dict)}]（兼容流式分片与非流式两种格式）。"""
        import json as _json
        out = []
        for s in (sink or []):
            name = s.get("name") or ""
            if not name:
                continue
            if isinstance(s.get("args"), dict):
                args = s["args"]
            else:
                try:
                    args = _json.loads(s.get("arguments") or "{}")
                except Exception:
                    args = {}
            out.append({"id": s.get("id", ""), "name": name, "args": args})
        return out

    async def execute(self, input_data: dict) -> Any:
        """执行任务：ContextBuilder → 可选 Tool 循环 → Memory 持久化"""
        # #12 设置 Agent 级预算 scope（分层 Token 限制）
        try:
            from core.gateway.budget import set_agent_budget_scope
            set_agent_budget_scope(self.id)
        except Exception as e:
            _logger.debug("Agent 预算 scope 设置失败（非致命）: %s", e)

        # 保存 project_id 供 _llm_call 流式推送使用
        run_ctx = input_data.get("run_context")
        self._current_project_id = input_data.get(
            "project_id",
            run_ctx.project_id if isinstance(run_ctx, RunContext) else None,
        )
        # P0-3：产物归属校验 — 工具登记产物时写入当前 run_id
        self._current_run_id = getattr(run_ctx, "run_id", "") if isinstance(run_ctx, RunContext) else ""

        if not isinstance(run_ctx, RunContext):
            raise ValueError("Agent 执行需要 RunContext（D2）")

        project_id = run_ctx.project_id or (getattr(self.bus, "project_id", "") if self.bus else "")

        # 读取 handoff / workspace 引用
        handoffs = input_data.get("handoffs") or []
        workspace_refs = input_data.get("workspace_refs") or {}
        if not handoffs and self.bus:
            latest = self.bus.get_latest_handoff_for(self.id)
            if latest:
                handoffs = [latest]

        # 加载 episodic 记忆
        memory_context = ""
        if self._use_memory():
            memories = await MemoryManager.recall(self.id, project_id, "episodic", limit=3)
            memory_context = MemoryManager.format_for_context(memories)

        # 领域知识库上下文（DomainAdapter → KnowledgeManager）
        knowledge_context = await self._load_knowledge_context(run_ctx)
        if knowledge_context:
            memory_context = f"{memory_context}\n\n{knowledge_context}".strip()
            # 知识效果反馈：记录注入的知识条目并 SSE 推送
            try:
                from core.knowledge import get_knowledge_manager
                km = get_knowledge_manager()
                domain_id = self.config.get("domain_id") or self.definition.type or "general"
                ak = km.get_agent_knowledge(self.id, domain_id)
                if ak:
                    injected_ids = [item.id for item in getattr(ak, "_context_window", [])]
                    if injected_ids:
                        from core.events import event_bus
                        project_id = getattr(run_ctx, "project_id", None) if run_ctx else None
                        event_bus.broadcast(
                            event="knowledge_injected",
                            data={
                                "agent_id": self.id,
                                "agent_name": self.name,
                                "knowledge_ids": injected_ids,
                                "count": len(injected_ids),
                            },
                            project_id=project_id,
                        )
            except Exception:
                pass  # 知识反馈是辅助功能，失败不影响执行

        tool_results: List[str] = []
        # P1-8补：OpenAI 原生 function calling 工具消息历史
        # （assistant.tool_calls + role:"tool" 结果）。修复前工具结果以 user 文本块
        # 塞回，LLM 收到非规范格式 → 输出"我将继续"叙事而非合成交付。
        tool_history: List[dict] = []
        max_iters = self.config.get("max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS)
        iterations = 0
        raw_text = ""

        cap_ctx = await self._resolve_capabilities() if self._use_tools() else {}
        allowed_tools = cap_ctx.get("tools", [])
        tool_namespaces = cap_ctx.get("namespaces", [])
        skill_prompts = cap_ctx.get("prompt_snippets", [])

        # function calling 工具 schema（OpenAI 格式）；逐工具容错构建，模型不支持时回退文本解析
        openai_tools = None
        if self._use_tools() and allowed_tools:
            try:
                # 局部 import（execute 内 line~459 有 ToolRegistry 局部 import，
                # 顶部引用的名称在该处才绑定，直接用会 UnboundLocalError）
                from core.agent.tools import ToolRegistry as _TR
                registry = _TR.get_instance()
                defs = []
                for raw in allowed_tools:
                    name = raw if isinstance(raw, str) else getattr(raw, "name", None)
                    if not name:
                        continue
                    td = registry.get(name)
                    if td is None:
                        for ns in (tool_namespaces or []):
                            td = registry.get(name, namespace=ns)
                            if td:
                                break
                    if td is None:
                        continue
                    defs.append({
                        "type": "function",
                        "function": {
                            "name": td.name,
                            "description": td.description or "",
                            "parameters": td.parameters or {"type": "object", "properties": {}},
                        },
                    })
                openai_tools = defs or None
                if not openai_tools:
                    _logger.debug("Agent %s 无可用 function calling 工具，回退文本协议", self.id)
            except Exception:
                _logger.exception("Agent %s 构建 openai_tools 失败，回退文本工具协议", self.id)
                openai_tools = None

        extra = memory_context
        shared_thread = input_data.get("shared_thread") or []
        if shared_thread:
            st_lines = ["## 阶段间沟通（shared_thread）"]
            for ex in shared_thread:
                if not isinstance(ex, dict):
                    continue
                st_lines.append(f"**追问**: {ex.get('question', '')}")
                st_lines.append(f"**上游回复**: {ex.get('answer', '')}")
            extra += "\n\n" + "\n".join(st_lines)

        # 协商上下文：已确认约束 + 未决问题 + 用户插话（D6 闭环）
        from core.run import negotiation as _negotiation

        nego_context = _negotiation.format_for_prompt(run_ctx, self.id, run_ctx.current_phase_id)
        if nego_context:
            extra += "\n\n" + nego_context
        consumed = _negotiation.consume_interjections(run_ctx, self.id)
        if consumed:
            from core.run.collaboration_protocol import CollaborationProtocol
            CollaborationProtocol.status(
                run_ctx, self.id,
                f"已接收用户插话 {consumed} 条，纳入本阶段执行",
                phase_id=run_ctx.current_phase_id,
            )

        if skill_prompts:
            extra += "\n\n" + "\n\n".join(skill_prompts)

        # 质量重试反馈（由 GraphRuntime 注入）
        retry_hint = input_data.get("retry_hint") or ""
        if retry_hint:
            extra += "\n\n" + str(retry_hint)

        # Gap1 修复：自评反馈（self_review 未通过时注入，供重试针对性改进）
        self_review_feedback = input_data.get("_self_review_feedback") or ""
        if self_review_feedback:
            extra += "\n\n## 自评反馈（上一轮产出未达标，请针对性改进）\n" + str(self_review_feedback)

        force_model = input_data.get("force_model")
        if force_model:
            self.config = {**(self.config or {}), "force_model": force_model}

        # 工具引导和产出要求由 ContextBuilder._build_collaboration_protocol() 统一生成，
        # 不再在 BaseAgent 中硬编码。
        # S1 修正：注入结构化工具描述（含参数 schema），让 LLM 不用猜参数名
        if self._use_tools() and allowed_tools:
            extra += "\n\n## 可用工具\n"
            extra += "如需使用工具，输出 JSON: {\"needs_tool\": true, \"tool_name\": \"工具名\", \"tool_args\": {参数}}\n\n"
            try:
                from core.agent.tools import ToolRegistry
                registry = ToolRegistry.get_instance()
                tool_descriptions = registry.to_openai_format(
                    tool_names=list(allowed_tools),
                    namespaces=list(cap_ctx.get("namespaces", [])),
                )
                for td in tool_descriptions:
                    func = td.get("function", {})
                    extra += f"### {func.get('name', '')}\n{func.get('description', '')}\n"
                    props = (func.get("parameters", {}) or {}).get("properties", {})
                    required = set((func.get("parameters", {}) or {}).get("required", []))
                    if props:
                        extra += "参数：\n"
                        for pname, pspec in props.items():
                            req_mark = "（必填）" if pname in required else "（可选）"
                            extra += f"  - `{pname}` ({pspec.get('type', 'string')}){req_mark}: {pspec.get('description', '')}\n"
                    extra += "\n"
                described = {td["function"]["name"] for td in tool_descriptions}
                undescribed = set(allowed_tools) - described
                if undescribed:
                    extra += f"其他工具（参数参考提示词上下文）：{', '.join(sorted(undescribed))}\n"
            except Exception:
                # 降级：registry 异常时退回纯名字列表（旧行为）
                extra += f"可用工具: {', '.join(allowed_tools)}。\n"
            manifest = self._workspace_file_manifest()
            if manifest:
                extra += "\n\n" + manifest

        # ask_peer：执行中途自主向同伴提问（不依赖 use_tools 开关）
        ask_peer_budget = int(self.config.get("max_ask_peer", 2))
        peers = [aid for aid in run_ctx.agent_outputs.keys() if aid != self.id]
        ask_peer_enabled = bool(peers) and ask_peer_budget > 0
        if ask_peer_enabled:
            extra += (
                "\n\n## 同伴协作（ask_peer）\n"
                "执行中若对上游产出/约束有疑问，**不要猜测**，"
                "可输出 JSON：{\"needs_tool\": true, \"tool_name\": \"ask_peer\", "
                "\"tool_args\": {\"to\": \"<agent_id>\", \"question\": \"<具体问题>\"}} 向同伴提问。"
                f"可问对象: {', '.join(peers)}；本次最多 {ask_peer_budget} 次。得到回复后继续完成任务。"
            )

        # 跨 Run 记忆：加载历史学习到的偏好/模式/领域知识
        cross_run_memories = []
        try:
            from core.memory.memory_store import CrossRunMemoryStore
            cross_run_memories = await CrossRunMemoryStore.get_relevant_memories(
                self.id, limit=5, min_decay_score=0.3,
            )
        except Exception:
            pass
        # === E3 新增：标记这些记忆被使用了（boost decay_score，有用的活更久）===
        if cross_run_memories:
            try:
                from core.memory.memory_store import CrossRunMemoryStore
                for mem in cross_run_memories:
                    await CrossRunMemoryStore.record_access(mem.id)
            except Exception:
                pass  # 非关键路径

        while iterations < max(1, max_iters):
            iterations += 1
            # function calling 模式：工具结果由 role:"tool" 消息携带，跳过 user 文本块避免重复
            # 最后一轮强制合成：防止 agent 一直研究不交付、撞上限后被截断为空壳
            _extra_round = extra
            if iterations >= max(1, max_iters):
                _extra_round = (
                    extra
                    + "\n\n【最后一轮】这是你本阶段的最后一次回复机会。"
                    "请直接基于已收集的信息产出完整、干净的最终交付（报告/分析/结论正文），"
                    "不要发起新的工具调用，也不要写'我将继续'类的进度说明。"
                )
            _text_tool_results = [] if (openai_tools and tool_history) else tool_results
            messages = ContextBuilder.build(
                self, run_ctx, tool_results=_text_tool_results, extra_instructions=_extra_round,
                handoffs=handoffs, workspace_refs=workspace_refs,
                cross_run_memories=cross_run_memories,
            )
            if openai_tools and tool_history:
                # 追加 OpenAI 工具消息历史（assistant.tool_calls + role:"tool" 结果）
                messages = messages + tool_history
            tool_sink: list = []
            raw_text = await self._llm_call_messages(messages, tools=openai_tools, tool_calls_sink=tool_sink)
            if self._is_llm_error_text(raw_text):
                _logger.error("Agent %s LLM 调用失败，拒绝将错误串当产出: %s", self.id, raw_text[:200])
                return {
                    "agent_id": self.id,
                    "role": self.role,
                    "result": raw_text,
                    "status": "error",
                    "error": raw_text,
                    "raw": raw_text[:2000],
                }

            # function calling：优先结构化 tool_calls；无则回退文本解析（模型不支持 tools 或返回文本格式）
            tool_calls = self._assemble_tool_calls(tool_sink)
            if not tool_calls:
                thought = self._parse_thought(raw_text)
                if thought.get("needs_tool"):
                    tool_calls = [{"id": "", "name": str(thought.get("tool_name") or ""), "args": thought.get("tool_args") or {}}]

            if not tool_calls:
                break

            # 统一按 tool_calls 执行（function calling 一次可能返回多个）
            executed_any = False
            # P1-8补：function calling 模式（tool_sink 有结构化调用）时，
            # 先记录 assistant.tool_calls 消息，供下一轮 role:"tool" 回灌。
            _fc_mode = bool(openai_tools and tool_sink)
            if _fc_mode:
                _assistant_tool_calls = []
                for _i, _tc in enumerate(tool_calls):
                    _tc_id = str(_tc.get("id") or "") or f"call_{iterations}_{_i}"
                    _tc["_call_id"] = _tc_id
                    _assistant_tool_calls.append({
                        "id": _tc_id,
                        "type": "function",
                        "function": {
                            "name": str(_tc.get("name") or ""),
                            "arguments": json.dumps(_tc.get("args") or {}, ensure_ascii=False),
                        },
                    })
                tool_history.append({
                    "role": "assistant",
                    "content": raw_text or None,
                    "tool_calls": _assistant_tool_calls,
                })

            for tc in tool_calls:
                tc_name = str(tc.get("name") or "")
                tc_args = tc.get("args") or {}

                if tc_name == "ask_peer":
                    if ask_peer_budget <= 0:
                        tool_results.append("[ask_peer] 已达提问上限，请基于现有信息直接完成任务")
                        if _fc_mode:
                            # function calling 协议：每个 tool_call 必须有配对 role:"tool" 响应
                            tool_history.append({
                                "role": "tool",
                                "tool_call_id": tc.get("_call_id") or "",
                                "content": "[ask_peer] 已达提问上限，请基于现有信息直接完成任务",
                            })
                        continue
                    ask_peer_budget -= 1
                    peer_answer = await self._ask_peer(run_ctx, tc_args)
                    tool_results.append(f"[ask_peer 回复]\n{peer_answer}")
                    run_ctx.add_observation(self.id, peer_answer[:500], kind="ask_peer")
                    executed_any = True
                    if _fc_mode:
                        # 修复：ask_peer 也是 function calling —— 必须回灌 role:"tool"，
                        # 否则 assistant.tool_calls（含 ask_peer）无配对 tool 消息，
                        # 下一轮 LLM 调用 400: "must be followed by tool messages responding to each tool_call_id"
                        tool_history.append({
                            "role": "tool",
                            "tool_call_id": tc.get("_call_id") or "",
                            "content": f"[ask_peer 回复]\n{peer_answer}",
                        })
                    continue

                if not self._use_tools():
                    break

                ctx = self._tool_execution_context()
                executor = ToolExecutor(
                    ToolRegistry.get_instance(),
                    output_dir=self.output_dir,
                    context=ctx,
                )
                tool_out = await executor.execute(
                    {"needs_tool": True, "tool_name": tc_name, "tool_args": tc_args},
                    allowed_tools=allowed_tools,
                    namespaces=tool_namespaces,
                )
                _fc_tool_content = ""
                if tool_out:
                    tool_results.append(tool_out)
                    executed_any = True
                    if hasattr(run_ctx, "add_observation"):
                        run_ctx.add_observation(self.id, tool_out, kind="tool_result")
                    if self.bus:
                        await self.bus.send_status(f"  🔧 {self.name} 工具 {tc_name}: {tool_out[:80]}...")
                    _fc_tool_content = tool_out
                elif _fc_mode:
                    # function calling 协议：工具返回空也必须回灌 role:"tool"，
                    # 否则该 tool_call_id 无配对响应 → 下一轮 LLM 400
                    _fc_tool_content = f"[工具 {tc_name} 返回空结果]"
                if _fc_mode:
                    # 按 OpenAI 规范回灌工具结果（role:"tool" + tool_call_id）
                    tool_history.append({
                        "role": "tool",
                        "tool_call_id": tc.get("_call_id") or "",
                        "content": _fc_tool_content,
                    })

            if not executed_any:
                break

        result = self._build_result(raw_text, input_data)
        # #9 Agent 输出长度限制（防爆内存 / 超长 prompt 污染下游）
        _MAX_AGENT_OUTPUT = 50_000
        if isinstance(result.get("result"), str) and len(result["result"]) > _MAX_AGENT_OUTPUT:
            result["result"] = result["result"][:_MAX_AGENT_OUTPUT] + f"\n\n…[输出已截断，原始长度 {len(result['result'])} 字符]"
        run_ctx.set_agent_result(
            self.id, result, iterations=iterations,
            agent_name=self.name,
        )

        if self._use_memory():
            await MemoryManager.store_execution_summary(
                self.id, project_id, result, task=run_ctx.task,
            )

        # === E2 新增：每次执行都记录进化数据（不依赖 QualityGate）===
        try:
            from core.evolution import EvolutionEngine
            engine = EvolutionEngine()
            engine.set_project(project_id)
            evo_score = max(5.0, 8.0 - (iterations - 1) * 1.0)
            await engine.log_execution(
                agent_id=self.id,
                result=result,
                quality_score=evo_score,
                iterations=iterations,
                project_id=project_id,
            )
        except Exception:
            pass  # 进化数据采集不应阻断主流程

        # #5 知识效果反馈：检测引用并写入统计
        try:
            from core.knowledge import get_knowledge_manager
            from core.knowledge.reference_detector import KnowledgeReferenceDetector
            km = get_knowledge_manager()
            domain_id = self.config.get("domain_id") or self.definition.type or "general"
            ak = km.get_agent_knowledge(self.id, domain_id)
            if ak:
                injected_items = getattr(ak, "_context_window", [])
                content_str = result.get("result", "") if isinstance(result, dict) else str(result)
                if injected_items and content_str:
                    refs = KnowledgeReferenceDetector.detect(injected_items, str(content_str))
                    from core.storage.database import get_db
                    db = await get_db()
                    run_id = getattr(run_ctx, "run_id", "") if run_ctx else ""
                    pid = getattr(run_ctx, "project_id", "") if run_ctx else ""
                    for item in injected_items:
                        await db.execute(
                            "INSERT INTO knowledge_stats (knowledge_id, agent_id, project_id, run_id, injected, referenced) "
                            "VALUES (?, ?, ?, ?, 1, ?)",
                            (item.id, self.id, pid, run_id, 1 if refs.get(item.id) else 0),
                        )
                    await db.commit()
                    from core.events import event_bus
                    event_bus.broadcast(
                        event="knowledge_referenced",
                        data={
                            "agent_id": self.id,
                            "agent_name": self.name,
                            "total": len(injected_items),
                            "referenced": sum(1 for v in refs.values() if v),
                        },
                        project_id=pid or None,
                    )
        except Exception:
            pass  # 统计失败不影响执行

        # --- Phase 3 新增：Agent 自评 ---
        self_review_enabled = self.definition.extra.get("self_review_enabled", False)
        is_retry = input_data.get("_is_self_review_retry", False)
        run_ctx = input_data.get("run_context")

        if self_review_enabled and run_ctx and getattr(run_ctx, 'skeleton', None) and not is_retry:
            try:
                from core.agent.self_review import self_review
                from core.skeleton.models import Skeleton
                from tools.llm_client import LLMClient

                unit_brief = ""
                if run_ctx.current_unit and run_ctx.skeleton:
                    sk = Skeleton.model_validate(run_ctx.skeleton)
                    unit_spec = sk.get_unit(run_ctx.current_unit)
                    if unit_spec:
                        unit_brief = unit_spec.goal

                if unit_brief:
                    output_text = ""
                    if isinstance(result, dict):
                        output_text = result.get("content", "") or result.get("result", "") or str(result)[:2000]
                    else:
                        output_text = str(result)[:2000]
                    passed, score, issues = await self_review(
                        llm_client=LLMClient(),
                        output_text=output_text,
                        unit_brief=unit_brief,
                        threshold=self.definition.extra.get("self_review_threshold", 0.7),
                    )
                    # --- 修正2：emit self_review SSE ---
                    try:
                        from core.events import broadcast_event
                        await broadcast_event(run_ctx.project_id, "self_review", {
                            "agent_id": self.id,
                            "agent_name": self.definition.name,
                            "agent_emoji": getattr(self.definition, 'emoji', None) or "🔍",
                            "passed": passed,
                            "score": score,
                            "issues": issues,
                            "retrying": not passed,
                        })
                    except Exception:
                        pass
                    if not passed:
                        _logger.info(f"Self-review failed (score={score}), retrying with feedback")
                        input_data["_is_self_review_retry"] = True
                        input_data["_self_review_feedback"] = "；".join(issues)
                        return await self.execute(input_data=input_data)
            except Exception as e:
                _logger.warning(f"Self-review error: {e}")

        return result

    async def _ask_peer(self, run_ctx: RunContext, args: dict) -> str:
        """执行中途向同伴提问（子图嵌套异步化，对标 Loopit 事件驱动）

        使用 RunContext.execute_subgraph 真正调用同伴 Agent.execute()，
        超时标记 status=open 而非假装确认。
        """
        from core.run import negotiation as _negotiation
        from core.run.collaboration_protocol import CollaborationProtocol
        from core.run.run_context import NegotiationEntry

        question = str(args.get("question") or "").strip()
        if not question:
            return "[错误] ask_peer 需要 question 参数"

        peers = [aid for aid in run_ctx.agent_outputs.keys() if aid != self.id]
        to_agent = str(args.get("to") or args.get("agent_id") or "").strip()
        if not to_agent or to_agent == self.id or (peers and to_agent not in peers):
            to_agent = peers[-1] if peers else ""
        if not to_agent:
            return "[错误] 当前没有可提问的同伴"

        phase_id = run_ctx.current_phase_id
        body = f"**[{self.name} · 执行中追问]** {question}"

        # 1. 提问落盘（ConversationBus + collaboration_thread）
        q_msg = None
        if self.bus:
            try:
                q_msg = await self.bus.send_question(self.id, to_agent, body, metadata={
                    "protocol": "ask_peer",
                    "dialogue_role": "question",
                    "phase_id": phase_id,
                })
            except Exception as e:
                _logger.debug("ask_peer 提问落盘失败: %s", e)
        await CollaborationProtocol.dialogue_turn(
            run_ctx, self.id, to_agent, "question", body, phase_id=phase_id,
        )

        # 2. 通过 execute_subgraph 真正执行同伴 Agent（子图嵌套）
        peer_task = (
            f"你的协作伙伴「{self.definition.name}」在执行任务时向你提问：\n\n"
            f"**问题：** {question}\n\n"
            f"请基于你的角色定位给出专业回复。如有不确定之处请明确说明。"
        )
        result = await run_ctx.execute_subgraph(
            agent_id=to_agent,
            task=peer_task,
            timeout=30,
        )

        if result.success:
            answer = f"**[{to_agent} · 回复]**\n\n{result.output}"

            # 3. 回复落盘
            if self.bus:
                try:
                    await self.bus.send_answer(
                        to_agent, self.id, answer,
                        parent_id=getattr(q_msg, "id", None),
                        metadata={
                            "protocol": "ask_peer",
                            "dialogue_role": "answer",
                            "phase_id": phase_id,
                        },
                    )
                except Exception as e:
                    _logger.debug("ask_peer 回复落盘失败: %s", e)
            await CollaborationProtocol.dialogue_turn(
                run_ctx, to_agent, self.id, "answer", answer, phase_id=phase_id,
            )

            # 4. 提取约束
            constraints = _negotiation.extract_constraints_heuristic(answer)
            if constraints:
                _negotiation.add_constraints(
                    run_ctx, constraints,
                    phase_id=phase_id, from_agent=to_agent, to_agent=self.id,
                    source="ask_peer",
                )

            # 5. 推送 SSE 事件
            from core.events import event_bus
            event_bus.broadcast(
                event="peer_reply",
                data={
                    "from_agent": to_agent,
                    "to_agent": self.id,
                    "status": "confirmed",
                    "reply_preview": result.output[:100],
                    "constraints_count": len(constraints),
                },
                project_id=run_ctx.project_id,
            )

            return answer

        else:
            # 超时/失败：标记 open，不假装确认
            run_ctx.add_negotiation_entry(NegotiationEntry(
                kind="open_question",
                phase_id=phase_id,
                from_agent=self.id,
                to_agent=to_agent,
                text=f"未解决: {question[:80]}",
                status="open",
                source="ask_peer",
            ))
            run_ctx.add_observation(
                source="ask_peer",
                content=f"ask_peer timeout: {self.id}→{to_agent}: {question[:50]}",
                kind="system",
            )

            from core.events import event_bus
            event_bus.broadcast(
                event="peer_reply",
                data={
                    "from_agent": to_agent,
                    "to_agent": self.id,
                    "status": "open",
                    "reply_preview": f"[{result.error}]",
                    "constraints_count": 0,
                },
                project_id=run_ctx.project_id,
            )

            return f"[同伴 {to_agent} 未能回复: {result.error}]"

    def _parse_thought(self, text: str) -> dict:
        """从 LLM 输出解析 tool call 意图（支持多个 ```json 代码块）。"""
        from core.communication.content_format import iter_tool_intents_from_text

        intents = list(iter_tool_intents_from_text(text))
        if intents:
            return intents[0]

        from core.agent.tools import ToolExecutor
        parsed = ToolExecutor.parse_tool_call_from_text(text)
        if parsed:
            return {"needs_tool": True, **parsed}
        return {"needs_tool": False, "content": text}

    async def _load_knowledge_context(self, state) -> str:
        """从 Phase 只读注入或 KnowledgeManager 加载任务相关知识 (D4)"""
        phase_knowledge = self.config.get("_phase_knowledge")
        if phase_knowledge:
            return str(phase_knowledge)

        try:
            from core.knowledge import get_knowledge_manager

            km = get_knowledge_manager()
            domain_id = self.config.get("domain_id") or self.definition.type or "general"
            ak = km.get_agent_knowledge(self.id, domain_id)
            query = getattr(state, "task", None) or self.config.get("task_description", "")
            if not query:
                return ""
            return await ak.build_context(query, domain_id=domain_id)
        except Exception as e:
            _logger.debug("知识上下文加载失败: %s", e)
            # 2026-08-09 P1：agent 缺知识不静默
            if hasattr(state, "record_degradation"):
                try:
                    state.record_degradation(
                        "warning", "knowledge",
                        f"Agent {self.id} 知识上下文加载失败: {e}",
                        "agent 缺领域知识",
                    )
                except Exception:
                    pass
            return ""

    @staticmethod
    def _is_llm_error_text(text: str) -> bool:
        """LLM 客户端失败时返回魔法错误串，不可当作正常产出落盘"""
        if not text:
            return False
        prefixes = ("[LLM Error]", "[LLM CircuitOpen]", "[LLM Budget")
        return any(text.startswith(p) for p in prefixes)

    def _build_result(self, raw_text: str, input_data: dict) -> dict:
        """构建结构化结果"""
        thought = self._parse_thought(raw_text)
        if isinstance(thought, dict) and thought.get("content") and not thought.get("needs_tool"):
            content = thought["content"]
        else:
            content = raw_text

        return {
            "agent_id": self.id,
            "role": self.role,
            "result": content,
            "raw": raw_text[:2000],
        }

    @staticmethod
    def _history_line(h: Any) -> str:
        """从 ConversationMessage / dict 安全取发言行"""
        if isinstance(h, dict):
            sender = h.get("agent_id") or h.get("sender_id") or "unknown"
            content = h.get("content", "")
        else:
            sender = getattr(h, "agent_id", None) or getattr(h, "sender_id", None) or "unknown"
            content = getattr(h, "content", "") or ""
        return f"[{sender}]: {content}"

    async def discuss_topic(self, topic: str, history: list) -> str:
        """圆桌讨论发言"""
        hist = history if isinstance(history, list) else list(history or [])
        lines: List[str] = [self._history_line(h) for h in hist[-20:]]
        history_text = "\n".join(lines) if lines else "(无历史)"
        response = await self._llm_call(
            f"议题: {topic}\n\n讨论历史:\n{history_text}\n\n请发表你的观点。",
            system_prompt=self.system_prompt,
        )
        return response
