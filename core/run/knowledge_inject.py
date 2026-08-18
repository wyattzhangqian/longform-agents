"""Knowledge 只读注入 (D4) — Phase 入口解析，不写入 RunContext 可变区"""

from __future__ import annotations
from typing import Optional

from core.run.phase_spec import KnowledgeProfile, PhaseSpec
from core.run.run_context import RunContext
from core.logging import get_logger

_logger = get_logger("run.knowledge_inject")


async def resolve_knowledge_text(
    phase: PhaseSpec,
    ctx: RunContext,
    agent_id: str,
) -> str:
    """根据 Phase.knowledge_profile 解析只读知识文本，供 ContextBuilder extra 使用

    P1-5: 主路径改用 SQL 结构化知识（KnowledgeInjector：general 层继承 /
    always+relevant 注入模式 / inject_to 过滤 / 6000 字预算裁剪）。
    修复前这里走 TF-IDF 向量检索（VectorKnowledgeBase），结构化 knowledge_entries
    入库却不参与 Agent 执行；SQL 无命中时回退原 TF-IDF 检索作为保底。
    """
    profile: KnowledgeProfile = phase.knowledge_profile
    # 默认 pack（2026-08-09）：run 未显式配置 packs 时，用项目 domain —— 领域知识
    # 应天然生效。之前 packs 为空 → 领域知识从不注入 writer（工作台能看到但执行不生效）。
    packs = list(profile.packs or [])
    if not packs:
        _dom = (ctx.config_snapshot or {}).get("domain_id") or ""
        if _dom:
            packs = [_dom if str(_dom).startswith("platform:") else f"platform:{_dom}"]
    if not packs:
        return ""

    query = ctx.task or ""
    if not query:
        return ""

    parts: list[str] = []
    run_phase_id = getattr(phase, "id", "") or ""

    # ── 主路径：SQL 结构化知识 ──
    try:
        from core.knowledge.injector import KnowledgeInjector

        injector = KnowledgeInjector()
        for pack in packs:
            if pack == "project.attachments":
                continue
            # knowledge_entries.domain_id 存储带 platform: 前缀
            domain_id = pack if pack.startswith("platform:") else f"platform:{pack}"
            # run phase 与领域 phase 不匹配（unit_N/agent_id/phase_2_writing vs draft）
            # → 用领域 phase 集合判断；run phase 在领域里则用它，否则空串（不过滤，
            #   注入领域全部写作规范，MAX_INJECT_CHARS 裁剪兜底）。
            _dphase_ids = await _domain_phase_ids(domain_id)
            inject_phase_id = run_phase_id if run_phase_id in _dphase_ids else ""
            block = await injector.build_knowledge_context(
                domain_id=domain_id,
                phase_id=inject_phase_id,
                task=ctx.task,
                agent_role=agent_id,
            )
            if block:
                parts.append(f"### 领域知识 [{pack}]\n{block}")
    except Exception as e:
        _logger.warning("SQL 结构化知识注入失败，回退向量检索: {}", e)

    # ── 回退：SQL 无命中时，保留原 TF-IDF 向量检索作为保底 ──
    if not parts:
        try:
            from core.knowledge import get_knowledge_manager

            km = get_knowledge_manager()
            for pack in profile.packs:
                if pack == "project.attachments":
                    continue
                ak = km.get_agent_knowledge(agent_id, pack)
                block = await ak.build_context(query, domain_id=pack, top_k=profile.top_k)
                if block:
                    parts.append(f"### 领域知识 [{pack}]\n{block}")
        except Exception as e:
            _logger.warning("向量知识检索失败，知识块缺失: {}", e)

    return "\n\n".join(parts).strip()


def attach_knowledge_to_config(config: dict, knowledge_text: str) -> dict:
    """将知识文本挂到 agent config 快照（只读，execute 内消费）"""
    if not knowledge_text:
        return config
    out = dict(config)
    out["_phase_knowledge"] = knowledge_text
    return out


_domain_phase_cache: dict = {}


async def _domain_phase_ids(domain_id: str) -> set:
    """领域 phase_definitions 的 id 集合（进程内缓存，避免每次 DB 查询）。"""
    if domain_id not in _domain_phase_cache:
        ids: set = set()
        try:
            from core.gateway.domain_registry import DomainRegistry
            domain = await DomainRegistry.get_domain(domain_id)
            if domain:
                pds = domain.get("phase_definitions", []) or []
                ids = {pd.get("id") for pd in pds if pd.get("id")}
        except Exception:
            pass
        _domain_phase_cache[domain_id] = ids
    return _domain_phase_cache[domain_id]
