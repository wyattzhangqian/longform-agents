"""ContextBuilder — 集中式上下文构建策略

根据 Agent 角色 + WorkflowState + 历史消息 + 工具结果，按 token 预算裁剪，
构建最优的 LLM messages 列表。

优先级排序（从高到低）：
1. System Prompt（不变）
2. 当前任务描述
3. 最近工具执行结果
4. 上游 Agent 产出摘要
5. 相关历史对话
6. 观察记录

估算规则：中文 1 字符 ≈ 1.5 token
"""

from __future__ import annotations
from typing import List, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent.base import BaseAgent
    from core.run.run_context import RunContext


class ContextBuilder:
    """集中式上下文构建器

    用法：
        messages = ContextBuilder.build(agent, state)
        response = await llm.chat(messages=messages)  # 或拼接为 prompt
    """

    # 默认 token 预算
    DEFAULT_TOKEN_BUDGET = 6000

    # 中文字符 → token 的估算比例
    CHAR_TO_TOKEN_RATIO = 1.5

    @staticmethod
    def build(
        agent: "BaseAgent",
        state: "RunContext",
        tool_results: List[str] = None,
        token_budget: int = None,
        extra_instructions: str = "",
        handoffs: List = None,
        workspace_refs: Dict[str, str] = None,
        cross_run_memories: List = None,
        domain_id: str = "",
        phase_id: str = "",
        agent_role: str = "",
        task: str = "",
    ) -> List[Dict[str, str]]:
        """构建 LLM messages 列表

        Args:
            agent: 当前执行的 Agent 实例
            state: 工作流全局状态
            tool_results: 最近工具执行结果列表
            token_budget: token 预算上限（默认 6000）
            extra_instructions: 额外指令（如系统级约束）
            handoffs: 收到的 HandoffPayload 列表
            cross_run_memories: 跨 run 记忆列表（MemoryEntry 对象）
            domain_id: 领域 ID（用于知识注入）
            phase_id: 当前阶段 ID
            agent_role: Agent 角色
            task: 任务描述（用于知识相关性匹配）
        """
        budget = token_budget or ContextBuilder.DEFAULT_TOKEN_BUDGET
        messages: List[Dict[str, str]] = []

        # ---- 第 1 层：System Prompt（纯 Agent 领域知识）----
        system_prompt = ContextBuilder._build_system_prompt(agent, extra_instructions)
        messages.append({"role": "system", "content": system_prompt})
        system_tokens = ContextBuilder._estimate_tokens(system_prompt)

        # ---- 剩余预算供后续层级分配 ----
        remaining = budget - system_tokens

        # ---- 第 1.5 层：协作协议（框架动态生成，PhaseSpec 驱动）----
        from core.run.phase_spec import PhaseSpec
        spec = getattr(state, '_phase_specs', {}).get(state.current_phase_id) if hasattr(state, 'current_phase_id') else None
        if spec and isinstance(spec, PhaseSpec):
            protocol = ContextBuilder._build_collaboration_protocol(spec, agent, state)
        else:
            # 兜底：无 PhaseSpec 时（如 Studio 预览），尝试从 state 推断
            protocol = ContextBuilder._build_collaboration_protocol(None, agent, state)
        if protocol:
            messages.append({"role": "system", "content": protocol})
            remaining -= ContextBuilder._estimate_tokens(protocol)

        # ==== Phase 2 新增：L4 全局设定（硬保障，不受 budget 裁剪）====
        if hasattr(state, 'layered_memory') and state.layered_memory and state.layered_memory.get("L4"):
            l4_text = state.layered_memory["L4"]
            messages.append({"role": "system", "content": l4_text})

        # ---- 第 2 层：当前任务（固定优先级）----
        task_block = ContextBuilder._build_task_block(state)
        task_tokens = ContextBuilder._estimate_tokens(task_block)
        messages.append({"role": "user", "content": task_block})

        # ==== Phase 2 新增：L1 当前单元 Brief ====
        if hasattr(state, 'skeleton') and state.skeleton and getattr(state, 'current_unit', None):
            try:
                from core.skeleton.integration import format_unit_spec_for_context
                from core.skeleton.models import Skeleton
                skeleton_obj = Skeleton.model_validate(state.skeleton)
                unit_spec = skeleton_obj.get_unit(state.current_unit)
                if unit_spec:
                    l1_text = format_unit_spec_for_context(unit_spec, skeleton_obj)
                    l1_tokens = ContextBuilder._estimate_tokens(l1_text)
                    if l1_tokens < remaining * 0.3:
                        messages.append({"role": "system", "content": l1_text})
                        remaining -= l1_tokens
            except Exception:
                # 2026-08-09 P1：context 层构建失败不静默
                ContextBuilder._record_ctx_degradation(state, "L1 单元 brief 构建失败")

        # ---- 第 2.5 层：Handoff 交接上下文 ----
        if handoffs and remaining > 400:
            handoff_block = ContextBuilder._build_handoff_block(handoffs)
            if handoff_block:
                messages.append({"role": "user", "content": handoff_block})
                remaining -= ContextBuilder._estimate_tokens(handoff_block)

        # ---- 第 2.6 层：Workspace 产物引用（摘要，非全文）----
        if workspace_refs and remaining > 300:
            ref_block = ContextBuilder._build_workspace_refs_block(workspace_refs)
            if ref_block:
                messages.append({"role": "user", "content": ref_block})
                remaining -= ContextBuilder._estimate_tokens(ref_block)

        # ---- 第 2.7 层：跨 Run 记忆（注入学习到的偏好与模式）----
        if cross_run_memories and remaining > 200:
            memory_block = ContextBuilder._build_cross_run_memory_block(cross_run_memories)
            if memory_block:
                messages.append({"role": "user", "content": memory_block})
                remaining -= ContextBuilder._estimate_tokens(memory_block)

        # ---- 第 3 层：工具结果（如果有）----
        if tool_results and remaining > 500:
            tool_block = ContextBuilder._build_tool_results_block(tool_results)
            messages.append({"role": "user", "content": tool_block})
            remaining -= ContextBuilder._estimate_tokens(tool_block)

        # ---- 第 4 层：上游 Agent 摘要 / L2 滑动窗口 ----
        if hasattr(state, 'layered_memory') and state.layered_memory and state.layered_memory.get("L2"):
            from core.memory.layered_memory import LayeredMemory
            mem = LayeredMemory.from_run_context(state.layered_memory)
            l2_text = mem.format_L2()
            if l2_text:
                l2_tokens = ContextBuilder._estimate_tokens(l2_text)
                if l2_tokens <= remaining * 0.6:
                    messages.append({"role": "user", "content": l2_text})
                    remaining -= l2_tokens
        elif remaining > 300:
            upstream = ContextBuilder._build_upstream_summary(agent, state)
            upstream_tokens = ContextBuilder._estimate_tokens(upstream)
            if upstream_tokens <= remaining * 0.6:  # 不超过剩余预算的 60%
                messages.append({"role": "user", "content": upstream})
                remaining -= upstream_tokens

        # ==== 修正5：计算本单元出场角色（氛围+Profile 共用，避免重复）====
        appearing: list = []
        if hasattr(state, 'skeleton') and state.skeleton and getattr(state, 'current_unit', None):
            try:
                from core.skeleton.models import Skeleton
                sk = Skeleton.model_validate(state.skeleton)
                unit_spec = sk.get_unit(state.current_unit)
                if unit_spec:
                    appearing = unit_spec.type_specific.get("cast", [])
            except Exception:
                ContextBuilder._record_ctx_degradation(state, "本单元出场角色计算失败")

        # ==== Phase 3 新增：氛围指导 ====
        if hasattr(state, 'atmosphere_state') and state.atmosphere_state:
            try:
                from core.memory.atmosphere_tracker import AtmosphereTracker
                atm = AtmosphereTracker(state=state.atmosphere_state)
                atm_text = atm.format_for_context(appearing_characters=appearing or None)
                if atm_text:
                    atm_tokens = ContextBuilder._estimate_tokens(atm_text)
                    if atm_tokens < remaining * 0.15:
                        messages.append({"role": "system", "content": atm_text})
                        remaining -= atm_tokens
            except Exception:
                ContextBuilder._record_ctx_degradation(state, "氛围指导上下文构建失败")

        # ==== Phase 3 新增：角色行为 Profile ====
        if hasattr(state, 'character_profiles') and state.character_profiles:
            try:
                from core.agent.character_profile import CharacterProfileStore
                store = CharacterProfileStore(profiles=state.character_profiles)
                profile_text = store.format_for_context(
                    appearing_characters=appearing[:3],  # 最多加载 3 个完整 Profile
                    all_appearing=appearing,
                )
                if profile_text:
                    profile_tokens = ContextBuilder._estimate_tokens(profile_text)
                    if profile_tokens < remaining * 0.2:
                        messages.append({"role": "system", "content": profile_text})
                        remaining -= profile_tokens
            except Exception:
                ContextBuilder._record_ctx_degradation(state, "角色行为 Profile 上下文构建失败")

        # ---- 第 5 层：相关历史对话（最近几轮）----
        if remaining > 200:
            history = ContextBuilder._build_history_block(agent, state, max_chars=int(remaining / ContextBuilder.CHAR_TO_TOKEN_RATIO))
            if history:
                messages.append({"role": "user", "content": history})

        return messages

    @staticmethod
    def build_simple_prompt(agent: "BaseAgent", state: "RunContext",
                            extra: str = "") -> str:
        """构建简单的纯文本 prompt（兼容旧 domain agent）

        将 messages 列表合并为一个文本块。
        """
        messages = ContextBuilder.build(agent, state, extra_instructions=extra)
        parts = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system":
                parts.append(f"[系统指令]\n{content}")
            else:
                parts.append(content)
        return "\n\n---\n\n".join(parts)

    # ==================== 内部构建方法 ====================

    @staticmethod
    def _build_system_prompt(agent: "BaseAgent", extra: str = "") -> str:
        """构建 System Prompt — Agent 领域知识 + 进化优化提示（有上限）"""
        definition = getattr(agent, "definition", None)
        base = ""
        if definition and getattr(definition, "system_prompt", ""):
            base = definition.system_prompt
        else:
            name = getattr(agent, "name", "Agent")
            role = getattr(agent, "role", "")
            base = f"你是 {name}（{role}）。请专业、准确地完成任务。"

        # [新增] 注入 optimization_hints（最多 3 条，限 300 字）
        if definition:
            hints = (getattr(definition, "extra", None) or {}).get("optimization_hints", [])
            if hints:
                top_hints = sorted(hints, key=lambda h: h.get("confidence", 0), reverse=True)[:3]
                hint_text = "\n".join(f"- {h['text']}" for h in top_hints if h.get("text"))
                if hint_text and len(hint_text) <= 300:
                    base += f"\n\n## 经验优化提示\n{hint_text}"

        if extra:
            base = base + "\n\n" + extra

        return base

    @staticmethod
    def _record_ctx_degradation(state, message: str) -> None:
        """记录 context 层降级（2026-08-09 P1）——state 若为 RunContext 则记 diagnostics。

        context_builder 是纯函数构建层，多处 except:pass 会让上下文部分缺失却无痕迹。
        这里统一收口：能记则记，不能记（如 Studio 预览）则忽略。
        """
        if state is None:
            return
        if hasattr(state, "record_degradation"):
            try:
                state.record_degradation(
                    "warning", "context_builder", message, "跳过该层上下文（影响上下文完整性）",
                )
            except Exception:
                pass

    @staticmethod
    def _build_collaboration_protocol(
        spec: Optional["PhaseSpec"],
        agent: "BaseAgent",
        state: "RunContext",
    ) -> str:
        """根据 PhaseSpec 契约生成协作协议指令。

        由框架在运行时动态生成，不来自 Agent system_prompt。
        包括：上游产出读取要求、下游交付要求、工具使用策略。
        """
        if spec is None:
            return ""

        lines = ["## 协作协议"]

        # 1) 上游：需读取的文件
        if spec.upstream_phase_id and spec.expected_inputs:
            upstream_label = spec.upstream_phase_id
            files = ", ".join(spec.expected_inputs)
            lines.append(f"- 上游阶段已完成。请使用 file_read 读取交付文件: {files}")
            lines.append("- 以磁盘文件为唯一真相，不要依赖口头描述。")
        elif spec.upstream_phase_id:
            lines.append(f"- 上游阶段已完成，请查看工作区文件。")

        # 2) 下游：需产出的文件
        # 【强制落盘】期望产出文件时，必须调 file_write 落盘正文，禁止只返回文本。
        # （2026-08-09 P0：修复 writer 完成却没产出正文——仅靠“请写入文件”提示，LLM 常
        #   只回文本不落盘；必须显式要求 file_write 工具调用，才能保证产物可被下游读取。）
        if spec.downstream_phase_id and spec.expected_outputs:
            downstream_label = spec.downstream_phase_id
            files = ", ".join(spec.expected_outputs)
            lines.append(f"- 你的产出将交付给下游阶段。请将最终专业产出写入文件: {files}")
            lines.append("- 只写最终产出，不要写思考过程或草稿。")
            lines.append("- 【强制】完成正文后，必须调用 file_write 工具将完整正文写入上述文件路径；不得仅返回文本而不落盘。")
        elif spec.expected_outputs:
            files = ", ".join(spec.expected_outputs)
            lines.append(f"- 请将最终专业产出写入文件: {files}")
            lines.append("- 【强制】完成正文后，必须调用 file_write 工具将完整正文写入上述文件路径；不得仅返回文本而不落盘。")

        # 3) 工具策略
        definition = getattr(agent, "definition", None)
        tools = getattr(definition, "tool_ids", []) or []
        if tools and spec.tool_policy != "disabled":
            tool_list = ", ".join(tools)
            if spec.tool_policy == "auto":
                lines.append(f"- 可用工具: {tool_list}")
                lines.append('- 需要时输出 JSON: {"needs_tool": true, "tool_name": "...", "tool_args": {...}}')
            elif spec.tool_policy == "on_demand":
                lines.append(f"- 你已配备工具: {tool_list}。仅在明确需要时使用。")

        return "\n".join(lines)

    @staticmethod
    def _build_task_block(state: "RunContext") -> str:
        """构建当前任务描述"""
        task = state.task or "执行你的专业任务"
        lines = [f"## 当前任务\n{task}"]

        if state.workflow_mode == "roundtable":
            rt = state.roundtable_state
            if rt:
                lines.append(f"\n讨论议题: {rt.topic}")
                lines.append(f"当前轮次: {rt.current_round}/{rt.max_rounds}")

        return "\n".join(lines)

    @staticmethod
    def _build_handoff_block(handoffs: List) -> str:
        """构建 handoff 交接上下文块"""
        if not handoffs:
            return ""
        parts = []
        for h in handoffs[-3:]:  # 最近 3 条
            if hasattr(h, "to_context_text"):
                parts.append(h.to_context_text())
            elif isinstance(h, dict):
                from_agent = h.get("from_agent") or h.get("from") or "上游"
                summary = h.get("summary") or h.get("content") or ""
                instructions = h.get("instructions") or ""
                result = h.get("result")
                lines = [f"## 来自 {from_agent} 的工作交接", f"**摘要**: {summary}"]
                if instructions:
                    lines.append(f"**对你的要求**: {instructions}")
                if result:
                    preview = result.get("result") if isinstance(result, dict) else str(result)
                    if preview:
                        lines.append(f"**产出预览**: {str(preview)[:400]}")
                arts = h.get("artifact_keys") or []
                if arts:
                    lines.append(f"**相关产物**: {', '.join(str(a) for a in arts[:8])}")
                parts.append("\n".join(lines))
        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _build_tool_results_block(results: List[str]) -> str:
        """构建工具结果块"""
        parts = ["## 工具执行结果"]
        for i, r in enumerate(results, 1):
            parts.append(f"{i}. {r}")
        return "\n".join(parts)

    @staticmethod
    def _build_workspace_refs_block(workspace_refs: Dict[str, str]) -> str:
        """构建 workspace 产物引用块（D6 — 不传全文）"""
        if not workspace_refs:
            return ""
        parts = ["## 工作区产物引用（摘要）"]
        for key, summary in list(workspace_refs.items())[:8]:
            parts.append(f"- {key}: {summary or key}")
        return "\n".join(parts)

    @staticmethod
    def _build_upstream_summary(agent: "BaseAgent", state: "RunContext") -> str:
        """构建上游 Agent 产出摘要

        动态从 state.agent_outputs 获取所有已完成的 Agent 产出，
        不再硬编码 Agent ID 列表，与具体工作流解耦。
        """
        parts = ["## 上游 Agent 产出摘要"]

        agent_id = getattr(agent, "id", "")
        has_content = False

        # 遍历所有已完成的 Agent（动态，不硬编码 ID 列表）
        for uid, output in state.agent_outputs.items():
            # 跳过自己
            if uid == agent_id:
                continue
            if output.termination_reason != "completed":
                continue

            has_content = True
            # 摘要：取 result 的关键字段
            summary_parts = []
            for k, v in output.result.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, str):
                    summary_parts.append(f"  - {k}: {v[:80]}{'...' if len(v) > 80 else ''}")
                elif isinstance(v, list):
                    summary_parts.append(f"  - {k}: [{len(v)} 项]")
                elif isinstance(v, (int, float)):
                    summary_parts.append(f"  - {k}: {v}")
                elif isinstance(v, dict):
                    summary_parts.append(f"  - {k}: {{...{len(v)} 字段}}")

            if summary_parts:
                parts.append(f"\n**{output.agent_name or uid}** ({output.iterations} 轮完成):")
                parts.extend(summary_parts)

        if not has_content:
            return ""

        return "\n".join(parts)

    @staticmethod
    def _build_cross_run_memory_block(memories: List) -> str:
        """构建跨 Run 记忆块（注入学习到的偏好与模式）"""
        if not memories:
            return ""
        parts = ["## 学习到的偏好与模式（跨 Run 记忆）"]
        for mem in memories:
            # MemoryEntry 对象有 memory_type 和 content 属性
            if hasattr(mem, "memory_type") and hasattr(mem, "content"):
                mem_type = mem.memory_type.value if hasattr(mem.memory_type, "value") else str(mem.memory_type)
                parts.append(f"- [{mem_type}] {mem.content}")
            elif isinstance(mem, dict):
                mem_type = mem.get("memory_type", "unknown")
                parts.append(f"- [{mem_type}] {mem.get('content', '')}")
        return "\n".join(parts) if len(parts) > 1 else ""

    @staticmethod
    def _build_history_block(agent: "BaseAgent", state: "RunContext",
                             max_chars: int = 1000) -> str:
        """构建相关历史对话块（从观察记录中提取）"""
        parts = ["## 近期上下文"]

        # 取最近的观察记录
        recent = state.get_recent_observations(n=5)
        if not recent:
            return ""

        added = 0
        for obs in recent:
            line = f"[{obs.source}] {obs.content[:100]}"
            added += len(line)
            if added > max_chars:
                break
            parts.append(line)

        return "\n".join(parts) if len(parts) > 1 else ""

    # ==================== Token 估算 ====================

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """估算文本的 token 数（中文 1 字符 ≈ 1.5 token）"""
        if not text:
            return 0
        return int(len(text) * ContextBuilder.CHAR_TO_TOKEN_RATIO)
