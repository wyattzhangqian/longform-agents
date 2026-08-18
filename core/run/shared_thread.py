"""shared_thread — 阶段间多轮沟通（dialogue_policy=shared_thread）

接收方确认 handoff 后，基于上游产物提出质疑/追问，上游回复后再 execute。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.logging import get_logger
from core.run.collaboration_protocol import CollaborationProtocol
from core.run.phase_spec import PhaseSpec
from core.run.run_context import RunContext

if TYPE_CHECKING:
    from core.agent.base import BaseAgent
    from core.communication.bus import ConversationBus

_logger = get_logger("shared_thread")

# 各阶段默认「质疑/深挖」探针 — 按 agent_id 前缀匹配（domain-agnostic）
DEFAULT_PROBE_HINTS: Dict[str, List[str]] = {
    # 网文创作
    "world_builder": [
        "修炼体系的核心阶位和突破条件是否定义清楚？每个阶位的力量上限在哪？",
        "你的世界观设定中是否有明显的逻辑漏洞或前后矛盾？请指明可能需要补充的细节。",
    ],
    "character_designer": [
        "角色的核心欲望和深层需求是否足够冲突？有哪些性格特征可能与其他角色雷同？",
        "角色的成长弧线是否足够清晰？前世今生的衔接点在哪里？",
    ],
    "plot_architect": [
        "三幕/四幕结构的高潮点和转折点是否明确？有没有情节节奏过于平缓的段落？",
        "主要反派与主角的前世关系是否已定义？是否有未回收的伏笔？",
    ],
    "chapter_writer": [
        "你的章节开头是否有足够有力的钩子？前500字能否抓住读者？",
        "对话和描写的比例是否合理？有没有信息密度过低的段落？",
    ],
    "style_editor": [
        "你的风格基准中哪些约束可能与网文平台读者的偏好冲突？",
        "对话部分的文风是否与叙述部分一致？有没有语气断裂的地方？",
    ],
    "copy_editor": [
        "校对中发现的主要问题模式是什么？高频错误类型有哪些？",
        "是否有角色名称/地名/设定术语不一致的情况？",
    ],
    # 通用
    "researcher": [
        "你的调研数据是否完整？有没有遗漏关键信息源？",
        "数据来源的可靠性如何？是否需要交叉验证？",
    ],
    "writer": [
        "文章结构是否清晰？核心论点是否有足够的证据支撑？",
        "内容的深度和广度是否满足任务要求？有没有过于浅显的部分？",
    ],
}


def find_pending_handoff(ctx: RunContext, receiver_id: str):
    """最近一条指向 receiver 的 handoff（含定向和广播）"""
    for entry in reversed(ctx.collaboration_thread):
        if entry.protocol == "handoff":
            is_directed = entry.to_agent == receiver_id
            is_broadcast = entry.to_agent == "*" and entry.from_agent != receiver_id
            if is_directed or is_broadcast:
                return entry
    return None


def _llm_available() -> bool:
    try:
        from config import DEEPSEEK_API_KEY, LLM_CONFIG
        return bool(DEEPSEEK_API_KEY or LLM_CONFIG.get("api_key"))
    except Exception:
        return False


def _upstream_output_preview(ctx: RunContext, sender_id: str) -> str:
    out = ctx.agent_outputs.get(sender_id)
    if not out:
        return ""
    data = out.result if hasattr(out, "result") else out
    if isinstance(data, dict):
        return str(data.get("result") or data.get("content") or data)[:400]
    return str(data)[:400]


def _artifact_candidates(
    ctx: RunContext,
    handoff_entry,
    spec: PhaseSpec,
) -> List[str]:
    names: List[str] = []
    meta = handoff_entry.metadata if handoff_entry and isinstance(handoff_entry.metadata, dict) else {}
    for key in ("artifact_keys",):
        val = meta.get(key)
        if isinstance(val, list):
            names.extend(str(x) for x in val if x)
    expected = spec.metadata.get("expected_artifacts") or []
    if isinstance(expected, list):
        names.extend(str(x) for x in expected)
    # 上游阶段常见产物
    if handoff_entry and handoff_entry.phase_id == "script":
        names.append("script.md")
    if handoff_entry and handoff_entry.phase_id == "visual_director":
        names.extend(["style-bible.md", "script.md"])
    seen: set[str] = set()
    out: List[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out[:4]


def _read_artifact_excerpt(project_id: str, filenames: List[str], limit: int = 1200) -> str:
    if not project_id:
        return ""
    roots = [
        Path(f"outputs/{project_id}"),
        Path("outputs") / project_id,
    ]
    for name in filenames:
        for root in roots:
            path = root / name
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8").strip()
                    if text:
                        return text[:limit]
                except Exception:
                    continue
    return ""


def _probe_hints(spec: PhaseSpec) -> List[str]:
    custom = spec.metadata.get("dialogue_probe_hints")
    if isinstance(custom, list) and custom:
        return [str(x) for x in custom]
    # 精确匹配 spec.id 或 spec.agent_id
    for key in (spec.id, spec.agent_id):
        if key and key in DEFAULT_PROBE_HINTS:
            return DEFAULT_PROBE_HINTS[key]
    # 前缀匹配（agent_id 通常是 agent_tpl_novel_world_builder 格式）
    agent_id = spec.agent_id or ""
    for prefix, hints in DEFAULT_PROBE_HINTS.items():
        if prefix in agent_id:
            return hints
    return []


def _fallback_question(
    *,
    receiver_name: str,
    sender_name: str,
    spec: PhaseSpec,
    handoff_summary: str,
    round_idx: int,
    artifact_excerpt: str,
    prior_answer: str,
    probe_hints: List[str],
) -> str:
    hint = probe_hints[round_idx] if round_idx < len(probe_hints) else (
        probe_hints[-1] if probe_hints else "请给出可执行的优先级与边界，避免我猜测。"
    )
    if round_idx == 0:
        lines = [
            f"**[{receiver_name} · 质疑]** 已读交接，开始 **{spec.label or spec.id}** 前有几点必须核实：",
            "",
            f"1. {hint}",
        ]
        if artifact_excerpt:
            first_line = next((ln.strip() for ln in artifact_excerpt.splitlines() if ln.strip() and not ln.startswith("#")), "")
            if first_line:
                lines.append(f"2. 我在上游产物中看到「{first_line[:100]}…」，是否与交接摘要一致？")
        elif handoff_summary:
            lines.append(f"2. 交接摘要提到「{handoff_summary[:90]}…」，是否有未写入产物文件的硬约束？")
        return "\n".join(lines)

    # 第二轮：深挖
    lines = [
        f"**[{receiver_name} · 深挖]** 基于你上一轮回复，还需明确：",
        "",
        f"• {hint}",
    ]
    if prior_answer:
        lines.append(f"\n> 你刚才说：{prior_answer[:180]}{'…' if len(prior_answer) > 180 else ''}")
        lines.append("\n请补充**可量化/可检查**的细节（例如场次编号、色彩 hex、必含元素），我再开始执行。")
    return "\n".join(lines)


def _fallback_answer(
    *,
    sender_name: str,
    receiver_name: str,
    question: str,
    preview: str,
    handoff_summary: str,
    artifact_excerpt: str,
    round_idx: int,
) -> str:
    basis = artifact_excerpt[:220] or preview[:220] or handoff_summary[:220]
    if round_idx == 0:
        return (
            f"**[{sender_name} · 回复]**\n\n"
            f"关于质疑：{question.split(chr(10))[0][:120]}…\n\n"
            f"**结论**：以上游产物为准。核心不可变点：\n"
            f"- {basis[:200]}{'…' if len(basis) > 200 else ''}\n"
            f"- 若与交接摘要冲突，以 **产物文件** 为唯一真相。\n\n"
            f"请 {receiver_name} 按此边界开始本阶段；若有歧义再追问。"
        )
    return (
        f"**[{sender_name} · 补充]**\n\n"
        f"深挖答复：\n"
        f"1. 优先级：产物已写明的约束 > 交接摘要 > 任务口头描述。\n"
        f"2. 可检查项：请对照 `{basis.split()[0] if basis else '上游产物'}` 中的标题/字段逐项落实。\n"
        f"3. 可以开始执行，不必等待进一步确认。"
    )


async def _generate_question(
    *,
    receiver: Optional["BaseAgent"],
    sender_name: str,
    spec: PhaseSpec,
    handoff_summary: str,
    task: str,
    round_idx: int,
    artifact_excerpt: str,
    prior_answer: str,
    probe_hints: List[str],
) -> str:
    if receiver and _llm_available():
        try:
            role = "质疑" if round_idx == 0 else "深挖细节"
            prompt = (
                f"项目任务：{task}\n"
                f"上游 {sender_name} 交接摘要：{handoff_summary[:300]}\n"
                f"上游产物摘录：{artifact_excerpt[:500] or '（暂无文件）'}\n"
                f"你将开始阶段：{spec.label or spec.id}\n"
                f"探针提示：{probe_hints[round_idx] if round_idx < len(probe_hints) else ''}\n"
                f"上一轮上游回复：{prior_answer[:300] or '（无）'}\n\n"
                f"请以接收方身份写一段 **{role}**（2-4 条 bullet），"
                f"必须具体、可执行，挑战模糊表述，不要寒暄。"
            )
            text = (await receiver._llm_call(
                prompt,
                system_prompt="你是严谨的下游 Agent，只输出质疑/追问正文（Markdown bullet）。",
            )).strip()
            if text:
                prefix = f"**[{getattr(receiver, 'name', spec.agent_id)} · {'质疑' if round_idx == 0 else '深挖'}]**\n\n"
                return prefix + text
        except Exception as e:
            _logger.debug("shared_thread LLM question failed: %s", e)
    return _fallback_question(
        receiver_name=getattr(receiver, "name", spec.agent_id),
        sender_name=sender_name,
        spec=spec,
        handoff_summary=handoff_summary,
        round_idx=round_idx,
        artifact_excerpt=artifact_excerpt,
        prior_answer=prior_answer,
        probe_hints=probe_hints,
    )


async def _generate_answer(
    *,
    sender: Optional["BaseAgent"],
    receiver_name: str,
    question: str,
    handoff_summary: str,
    preview: str,
    artifact_excerpt: str,
    round_idx: int,
) -> str:
    if sender and _llm_available():
        try:
            prompt = (
                f"{receiver_name} 的{'质疑' if round_idx == 0 else '深挖'}：\n{question}\n\n"
                f"你的产物摘录：{artifact_excerpt[:400] or preview[:400]}\n"
                f"交接摘要：{handoff_summary[:300]}\n"
                f"请给出明确、可执行的回复（分点，含优先级与可检查项）。"
            )
            text = (await sender._llm_call(
                prompt,
                system_prompt="你是上游 Agent，直面质疑，不回避，只输出回复正文。",
            )).strip()
            if text:
                label = "回复" if round_idx == 0 else "补充"
                return f"**[{getattr(sender, 'name', '上游')} · {label}]**\n\n{text}"
        except Exception as e:
            _logger.debug("shared_thread LLM answer failed: %s", e)
    return _fallback_answer(
        sender_name=getattr(sender, "name", "上游"),
        receiver_name=receiver_name,
        question=question,
        preview=preview,
        handoff_summary=handoff_summary,
        artifact_excerpt=artifact_excerpt,
        round_idx=round_idx,
    )


async def run_shared_thread_dialogue(
    ctx: RunContext,
    spec: PhaseSpec,
    receiver_id: str,
    sender_id: str,
    bus: "ConversationBus",
    *,
    receiver_name: str = "",
    sender_name: str = "",
    handoff_summary: str = "",
    receiver_agent: Optional["BaseAgent"] = None,
    sender_agent: Optional["BaseAgent"] = None,
    handoff_entry=None,
) -> List[Dict[str, Any]]:
    """执行 shared_thread 问答（默认 2 轮：质疑 + 深挖）"""
    max_rounds = max(1, min(int(spec.metadata.get("dialogue_max_rounds") or 2), 3))
    preview = _upstream_output_preview(ctx, sender_id)
    pending = handoff_entry or find_pending_handoff(ctx, receiver_id)
    artifact_files = _artifact_candidates(ctx, pending, spec)
    artifact_excerpt = _read_artifact_excerpt(ctx.project_id, artifact_files)
    probe_hints = _probe_hints(spec)
    exchanges: List[Dict[str, Any]] = []
    prior_answer = ""

    for round_idx in range(max_rounds):
        question = await _generate_question(
            receiver=receiver_agent,
            sender_name=sender_name or sender_id,
            spec=spec,
            handoff_summary=handoff_summary,
            task=ctx.task or "",
            round_idx=round_idx,
            artifact_excerpt=artifact_excerpt,
            prior_answer=prior_answer,
            probe_hints=probe_hints,
        )
        q_msg = await bus.send_question(
            receiver_id,
            sender_id,
            question,
            metadata={
                "protocol": "shared_thread",
                "dialogue_role": "question",
                "phase_id": spec.id,
                "round": round_idx + 1,
                "intent": "challenge" if round_idx == 0 else "drill_down",
            },
        )
        await CollaborationProtocol.dialogue_turn(
            ctx,
            receiver_id,
            sender_id,
            "question",
            question,
            phase_id=spec.id,
            round_num=round_idx + 1,
        )
        answer = await _generate_answer(
            sender=sender_agent,
            receiver_name=receiver_name or receiver_id,
            question=question,
            handoff_summary=handoff_summary,
            preview=preview,
            artifact_excerpt=artifact_excerpt,
            round_idx=round_idx,
        )
        prior_answer = answer
        await bus.send_answer(
            sender_id,
            receiver_id,
            answer,
            parent_id=getattr(q_msg, "id", None),
            metadata={
                "protocol": "shared_thread",
                "dialogue_role": "answer",
                "phase_id": spec.id,
                "round": round_idx + 1,
                "intent": "challenge" if round_idx == 0 else "drill_down",
            },
        )
        await CollaborationProtocol.dialogue_turn(
            ctx,
            sender_id,
            receiver_id,
            "answer",
            answer,
            phase_id=spec.id,
            round_num=round_idx + 1,
        )
        exchanges.append({
            "round": round_idx + 1,
            "intent": "challenge" if round_idx == 0 else "drill_down",
            "question": question,
            "answer": answer,
            "from": receiver_id,
            "to": sender_id,
        })

    # 结论结构化回灌：问答中达成的一致 → 已确认约束（质量门禁将校验）
    try:
        from core.run import negotiation

        constraints = await negotiation.extract_constraints(
            exchanges, agent=receiver_agent,
        )
        added = negotiation.add_constraints(
            ctx,
            constraints,
            phase_id=spec.id,
            from_agent=sender_id,
            to_agent=receiver_id,
            source="shared_thread",
        )
        if added:
            summary_lines = "\n".join(f"{i}. {e.text}" for i, e in enumerate(added, 1))
            await bus.send_answer(
                receiver_id,
                sender_id,
                f"**[约束确认]** 本轮沟通沉淀 {len(added)} 条已确认约束，已登记台账：\n\n{summary_lines}",
                metadata={
                    "protocol": "constraints_confirmed",
                    "phase_id": spec.id,
                    "constraint_ids": [e.id for e in added],
                },
            )
    except Exception as e:
        _logger.warning("shared_thread 约束回灌失败: %s", e)
        # 2026-08-09 P1：协商约束不回灌不静默
        if hasattr(ctx, "record_degradation"):
            try:
                ctx.record_degradation(
                    "critical", "shared_thread",
                    f"shared_thread 约束回灌失败: {e}",
                    "协商约束不回灌，质量门禁可能漏检",
                )
            except Exception:
                pass

    # 共识检测：多轮对话后检查 Agent 间是否达成一致
    consensus_reached = False
    try:
        from core.communication.bus import ConversationBus
        from core.events import event_bus as _eb
        bus = ConversationBus(ctx.project_id)
        # 将 exchanges 转为 has_consensus_async 需要的 messages 格式
        messages = []
        for ex in exchanges:
            messages.append({"sender_id": ex["from"], "content": ex.get("question", "")})
            messages.append({"sender_id": ex["to"], "content": ex.get("answer", "")})
        consensus = await bus.has_consensus_async(
            messages, topic=spec.label or spec.id,
        )
        if consensus.get("reached"):
            consensus_reached = True
            if ctx.roundtable_state:
                ctx.roundtable_state.consensus_reached = True
                ctx.roundtable_state.consensus_confidence = consensus.get("confidence", 0.0)
                ctx.roundtable_state.consensus_conclusion = consensus.get("consensus", "") or ""
                ctx.roundtable_state.status = "consensus_reached"
            _eb.broadcast(
                event="consensus_reached",
                data={
                    "phase_id": spec.id,
                    "confidence": consensus.get("confidence", 0.0),
                    "conclusion": consensus.get("consensus", ""),
                },
                project_id=ctx.project_id,
            )
    except Exception as e:
        _logger.debug("共识检测失败: %s", e)
        # 2026-08-09 P1：agent 间共识不检测不静默（WARNING 降级）
        if hasattr(ctx, "record_degradation"):
            try:
                ctx.record_degradation(
                    "warning", "shared_thread",
                    f"共识检测失败: {e}",
                    "agent 间共识不检测",
                )
            except Exception:
                pass

    CollaborationProtocol.status(
        ctx,
        receiver_id,
        f"shared_thread 质疑/深挖完成（{len(exchanges)} 轮）{'，达成共识' if consensus_reached else ''}，开始执行",
        phase_id=spec.id,
    )
    return exchanges
