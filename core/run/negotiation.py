"""Negotiation — 协商结论结构化回灌（对标 AutoGen/CrewAI 的协作闭环）

职责：
1. 从 shared_thread / ask_peer 问答中提取「已确认约束」→ RunContext.negotiation_ledger
2. 将约束 / 未决问题 / 人类插话格式化注入下游 Agent prompt
3. 质量门禁约束覆盖校验（constraints_coverage）
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.logging import get_logger
from core.run.run_context import NegotiationEntry, RunContext

if TYPE_CHECKING:
    from core.agent.base import BaseAgent

_logger = get_logger("negotiation")

# `*` 不匹配 `**`（粗体标记非 bullet）
_BULLET_RE = re.compile(r"^\s*(?:[-•]|\*(?!\*)|\d+[.)、])\s*(.+)$")
# 元话语行（角色署名 / 引导语），非约束本体
_META_LINE_RE = re.compile(r"^\[?[^\[\]]{0,30}·\s*(回复|补充|质疑|深挖|追问|确认)\]?")
_MAX_CONSTRAINTS_PER_DIALOGUE = 5
_MIN_CONSTRAINT_LEN = 8


def _llm_available() -> bool:
    try:
        from config import DEEPSEEK_API_KEY, LLM_CONFIG
        return bool(DEEPSEEK_API_KEY or LLM_CONFIG.get("api_key"))
    except Exception:
        return False


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    return text.strip()


def extract_constraints_heuristic(answer: str) -> List[str]:
    """无 LLM 时的兜底：从回复中抓取 bullet 行作为约束"""
    out: List[str] = []
    for line in (answer or "").splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        item = _strip_markdown(m.group(1))
        # 跳过引导语 / 元话语 / 标题 / 引用残片
        if len(item) < _MIN_CONSTRAINT_LEN:
            continue
        if item.startswith(("#", "[", "*", ">")):
            continue
        if item.endswith(("：", ":")):
            continue
        if _META_LINE_RE.match(item):
            continue
        if any(kw in item for kw in ("可以开始执行", "再追问", "不必等待", "以上游产物为准", "唯一真相")):
            continue
        out.append(item[:200])
        if len(out) >= _MAX_CONSTRAINTS_PER_DIALOGUE:
            break
    return out


async def extract_constraints(
    exchanges: List[Dict[str, Any]],
    *,
    agent: Optional["BaseAgent"] = None,
) -> List[str]:
    """从问答轮次中提取可校验约束（LLM 优先，bullet 兜底）"""
    if not exchanges:
        return []
    if agent and _llm_available():
        try:
            qa_text = "\n\n".join(
                f"Q{ex.get('round', i + 1)}: {ex.get('question', '')}\nA{ex.get('round', i + 1)}: {ex.get('answer', '')}"
                for i, ex in enumerate(exchanges)
            )
            prompt = (
                f"以下是两个 Agent 在交接时的质疑/回复：\n\n{qa_text[:3000]}\n\n"
                f"请提取其中达成一致的、**可检查**的执行约束（如必含元素、文件名、数量、边界）。"
                f"每行一条，不超过 {_MAX_CONSTRAINTS_PER_DIALOGUE} 条，不要编号，不要解释。"
                f"若无明确约束输出 NONE。"
            )
            text = (await agent._llm_call(
                prompt,
                system_prompt="你是协作记录员，只输出约束条目本身，每行一条。",
            )).strip()
            if text and text.upper() != "NONE":
                items = [
                    _strip_markdown(_BULLET_RE.match(ln).group(1) if _BULLET_RE.match(ln) else ln)
                    for ln in text.splitlines() if ln.strip()
                ]
                items = [i[:200] for i in items if len(i) >= _MIN_CONSTRAINT_LEN]
                if items:
                    return items[:_MAX_CONSTRAINTS_PER_DIALOGUE]
        except Exception as e:
            _logger.debug("LLM 约束提取失败，使用兜底: %s", e)

    merged: List[str] = []
    for ex in exchanges:
        merged.extend(extract_constraints_heuristic(str(ex.get("answer", ""))))
    seen: set[str] = set()
    out: List[str] = []
    for item in merged:
        key = item[:60]
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out[:_MAX_CONSTRAINTS_PER_DIALOGUE]


def add_constraints(
    ctx: RunContext,
    constraints: List[str],
    *,
    phase_id: str,
    from_agent: str,
    to_agent: str,
    source: str = "shared_thread",
) -> List[NegotiationEntry]:
    """约束写入台账（去重），并广播 SSE negotiation 事件"""
    existing = {e.text[:60] for e in ctx.negotiation_ledger if e.kind == "constraint"}
    added: List[NegotiationEntry] = []
    for text in constraints:
        if text[:60] in existing:
            continue
        entry = ctx.add_negotiation_entry(NegotiationEntry(
            kind="constraint",
            phase_id=phase_id,
            from_agent=from_agent,
            to_agent=to_agent,
            text=text,
            status="confirmed",
            source=source,  # type: ignore[arg-type]
        ))
        added.append(entry)
        existing.add(text[:60])
    if added:
        _emit_ledger_event(ctx, added)
    return added


def add_open_question(
    ctx: RunContext,
    question: str,
    *,
    phase_id: str,
    from_agent: str,
    to_agent: str = "",
    source: str = "ask_peer",
) -> NegotiationEntry:
    entry = ctx.add_negotiation_entry(NegotiationEntry(
        kind="open_question",
        phase_id=phase_id,
        from_agent=from_agent,
        to_agent=to_agent,
        text=question[:300],
        status="open",
        source=source,  # type: ignore[arg-type]
    ))
    _emit_ledger_event(ctx, [entry])
    return entry


def resolve_entry(ctx: RunContext, entry_id: str, status: str = "resolved") -> bool:
    for e in ctx.negotiation_ledger:
        if e.id == entry_id:
            e.status = status  # type: ignore[assignment]
            ctx.touch()
            _emit_ledger_event(ctx, [e])
            return True
    return False


def _emit_ledger_event(ctx: RunContext, entries: List[NegotiationEntry]) -> None:
    if not ctx.project_id:
        return
    try:
        from core.events import event_bus
        event_bus.broadcast(
            event="negotiation",
            data={"entries": [e.model_dump() for e in entries]},
            project_id=ctx.project_id,
        )
    except Exception as e:
        _logger.warning("协商事件 SSE 广播失败: {}", e)


# ============================================================
# Prompt 注入
# ============================================================

def format_for_prompt(ctx: RunContext, agent_id: str, phase_id: str = "") -> str:
    """构建注入 Agent prompt 的协商上下文（约束 + 未决项 + 人类插话）"""
    blocks: List[str] = []

    constraints = ctx.confirmed_constraints()
    if constraints:
        lines = ["## 已确认约束（必须满足，质量门禁将校验）"]
        for i, e in enumerate(constraints[-10:], 1):
            scope = f"[{e.phase_id}] " if e.phase_id and e.phase_id != phase_id else ""
            lines.append(f"{i}. {scope}{e.text}")
        blocks.append("\n".join(lines))

    open_items = ctx.open_negotiations()
    if open_items:
        lines = ["## 未决问题 ❓（如与你相关请在产出中说明如何处理）"]
        for e in open_items[-5:]:
            tag = "❓" if e.status == "open" else "⚠️"
            lines.append(f"- {tag} ({e.from_agent} → {e.to_agent or '全体'}) {e.text}")
        blocks.append("\n".join(lines))

    interjections = ctx.pending_interjections(agent_id)
    if interjections:
        lines = ["## 用户插话（最高优先级，立即遵守）"]
        for ij in interjections:
            lines.append(f"- {ij.content}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def consume_interjections(ctx: RunContext, agent_id: str) -> int:
    """标记该 Agent 的插话为已消费，返回消费数量"""
    count = 0
    for ij in ctx.pending_interjections(agent_id):
        ij.consumed_by.append(agent_id)
        count += 1
    if count:
        ctx.touch()
    return count


# ============================================================
# 质量门禁：约束覆盖校验
# ============================================================

_TOKEN_RE = re.compile(r"[A-Za-z][\w.-]{2,}|[\u4e00-\u9fff]{2,}|\d+(?:\.\d+)?|#[0-9a-fA-F]{3,8}")
_STOPWORDS = {
    "必须", "需要", "请", "使用", "保持", "确保", "所有", "以及", "或者", "如果",
    "the", "and", "for", "with", "must", "should",
}


def _key_tokens(text: str) -> List[str]:
    tokens = [t for t in _TOKEN_RE.findall(text) if t not in _STOPWORDS]
    # 优先文件名 / hex / 数字等强特征
    strong = [t for t in tokens if "." in t or t.startswith("#") or t.isdigit()]
    return (strong + tokens)[:8]


def check_constraint_coverage(constraint: str, output_text: str) -> bool:
    """启发式：约束关键 token 在产出中的命中率 >= 40% 视为已覆盖"""
    tokens = _key_tokens(constraint)
    if not tokens:
        return True
    lowered = output_text.lower()
    hits = sum(1 for t in tokens if t.lower() in lowered)
    return hits / len(tokens) >= 0.4


async def check_constraint_coverage_semantic(
    constraint: str,
    output_text: str,
    llm_client=None,
) -> tuple:
    """语义级约束覆盖校验 — LLM 判断产出是否真正满足了约束。

    优先走 LLM 语义判断（理解"3个"="三个"、"主角"="女主角"等语义等价），
    LLM 不可用时回退到 token 级启发式检查。

    Returns:
        (satisfied: bool, reason: str)
    """
    if not constraint.strip() or not output_text.strip():
        return True, "空约束或空产出，默认通过"

    # 快路径：token 级已明确通过 → 不浪费 LLM 调用
    if check_constraint_coverage(constraint, output_text):
        return True, "token 级命中率 ≥ 40%"

    # token 级未通过，用 LLM 做语义判断
    if llm_client is None:
        return False, "token 级未通过且无 LLM 客户端"

    try:
        prompt = (
            f"判断以下产出是否满足了约束。\n\n"
            f"约束: {constraint[:300]}\n\n"
            f"产出（摘要）: {output_text[:800]}\n\n"
            f'返回 JSON: {{"satisfied": true/false, "reason": "简要说明为什么满足或不满足"}}'
        )
        raw = await llm_client.chat_json(prompt)
        if isinstance(raw, dict):
            satisfied = bool(raw.get("satisfied", False))
            reason = str(raw.get("reason", ""))
            return satisfied, reason
    except Exception as e:
        _logger.warning("约束语义级校验降级为启发式: {}", e)

    return False, "token 级未通过且 LLM 语义判断失败"


async def evaluate_constraints_async(
    ctx: RunContext,
    phase_id: str,
    agent_id: str,
    output_text: str,
) -> List[dict]:
    """质量门禁调用（异步版）：语义级校验约束覆盖，回写台账状态。

    与 evaluate_constraints 的区别：使用 LLM 语义判断替代纯 token 匹配。
    """
    violations: List[dict] = []
    targets = [
        e for e in ctx.negotiation_ledger
        if e.kind == "constraint"
        and e.status in ("confirmed", "satisfied", "violated")
        and (e.phase_id == phase_id or e.to_agent == agent_id or e.from_agent == agent_id)
    ]
    if not targets:
        return violations

    # 懒加载 LLM 客户端（仅在需要时）
    llm_client = None
    try:
        from tools.llm_client import LLMClient
        from config import DEFAULT_LLM_MODEL
        llm_client = LLMClient({"model": DEFAULT_LLM_MODEL, "temperature": 0.1, "max_tokens": 256})
    except Exception as e:
        _logger.warning("协商 LLM 客户端懒加载失败，语义校验禁用: {}", e)

    changed: List[NegotiationEntry] = []
    for e in targets:
        if llm_client:
            covered, reason = await check_constraint_coverage_semantic(
                e.text, output_text, llm_client
            )
        else:
            covered = check_constraint_coverage(e.text, output_text)
            reason = "token 级检查" if covered else "token 级未通过"

        new_status = "satisfied" if covered else "violated"
        if e.status != new_status:
            e.status = new_status  # type: ignore[assignment]
            changed.append(e)
        if not covered:
            violations.append({
                "rule_id": "constraints_coverage",
                "severity": "warning",
                "message": (
                    f"约束未满足: 「{e.text[:80]}」→ {reason}"
                ),
            })

    if changed:
        # P1-7: 台账状态变更无需单独落库 —— RunContext.negotiation_ledger 随
        # checkpoint（StateManager.save_run_context / _save_checkpoint）全量持久化，
        # 是唯一真相。原 `ctx.save_negotiation_ledger()` 方法在 RunContext 上不存在
        # （AttributeError 被 except:pass 吞掉），属假调用，已移除。
        _logger.debug("协商台账状态更新 {} 条（随 checkpoint 持久化）", len(changed))

    return violations


def evaluate_constraints(
    ctx: RunContext,
    phase_id: str,
    agent_id: str,
    output_text: str,
) -> List[dict]:
    """质量门禁调用：校验产出覆盖约束，回写台账状态，返回违规列表"""
    violations: List[dict] = []
    targets = [
        e for e in ctx.negotiation_ledger
        if e.kind == "constraint"
        and e.status in ("confirmed", "satisfied", "violated")
        and (e.phase_id == phase_id or e.to_agent == agent_id or e.from_agent == agent_id)
    ]
    changed: List[NegotiationEntry] = []
    for e in targets:
        covered = check_constraint_coverage(e.text, output_text)
        new_status = "satisfied" if covered else "violated"
        if e.status != new_status:
            e.status = new_status  # type: ignore[assignment]
            changed.append(e)
        if not covered:
            violations.append({
                "rule_id": "constraints_coverage",
                "severity": "warning",
                "message": f"产出未覆盖已确认约束: {e.text[:80]}",
                "constraint_id": e.id,
            })
    if changed:
        ctx.touch()
        _emit_ledger_event(ctx, changed)
    return violations
