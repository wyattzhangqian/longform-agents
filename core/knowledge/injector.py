"""KnowledgeInjector — 知识注入引擎

将领域知识注入到 Agent 的执行上下文中。

注入策略：
1. always 条目：无条件注入（规范/术语/风格指南）
2. relevant 条目：基于任务关键词匹配 top-K（范例/反面案例）
3. on_demand 条目：不注入，Agent 需要时通过工具检索

预算控制：
- 总注入字符不超过 MAX_INJECT_CHARS（默认 6000 字 ≈ 4000 tokens）
- 超出时按 priority 降序裁剪
"""

from __future__ import annotations

import json
from typing import Dict, List

from core.knowledge.models_kb import (
    CATEGORY_EMOJI,
    CATEGORY_LABEL,
    KnowledgeInjectionPlan,
)
from core.knowledge.store import KnowledgeStore
from core.logging import get_logger

_logger = get_logger("knowledge.injector")


class KnowledgeInjector:
    """将领域知识注入到 Agent 的执行上下文中"""

    MAX_INJECT_CHARS = 6000
    RELEVANT_TOP_K = 5

    async def build_knowledge_context(
        self,
        domain_id: str,
        phase_id: str,
        task: str,
        agent_role: str = "",
    ) -> str:
        """构建知识注入文本 — 含 general 层继承"""

        # [新增] 1. 加载 platform:general 的 always 条目（通用层知识对所有领域生效）
        general_always = []
        if domain_id != "platform:general":
            general_always = await KnowledgeStore.list_entries(
                domain_id="platform:general",
                phase_id=None,  # general 不限定 phase
                inject_mode="always",
            )

        # 2. 加载目标领域的 always 条目（原有逻辑）
        domain_always = await KnowledgeStore.list_entries(
            domain_id=domain_id,
            phase_id=phase_id,
            inject_mode="always",
        )

        # 3. 加载目标领域的 relevant 条目并排序（原有逻辑）
        relevant_entries = await KnowledgeStore.list_entries(
            domain_id=domain_id,
            phase_id=phase_id,
            inject_mode="relevant",
        )
        ranked_relevant = self._rank_by_relevance(relevant_entries, task)[:self.RELEVANT_TOP_K]

        # [新增] 4. 合并：general always + domain always + domain relevant
        # 去重：以 title 为 key，domain 层覆盖 general 层（同 title 取 domain 的）
        seen_titles = set()
        merged: list = []

        # domain always 优先（覆盖 general 同名条目）
        for entry in domain_always:
            title = entry.get("title", "")
            seen_titles.add(title)
            merged.append(entry)

        # general always 补充（仅 domain 未覆盖的）
        for entry in general_always:
            title = entry.get("title", "")
            if title not in seen_titles:
                seen_titles.add(title)
                merged.append(entry)

        # relevant 追加
        for entry in ranked_relevant:
            title = entry.get("title", "")
            if title not in seen_titles:
                merged.append(entry)

        # 5. 按 inject_to 过滤（如果指定了 agent_role）
        all_entries = merged
        if agent_role:
            all_entries = [
                e for e in all_entries
                if not e.get("inject_to") or e["inject_to"] == agent_role
            ]

        # 6. 按 priority 降序排列
        all_entries.sort(key=lambda e: -(e.get("priority", 50)))

        # 7. 格式化 + 预算裁剪（原有逻辑）
        text = self._format_entries(all_entries)

        _logger.debug(
            "知识注入: domain=%s phase=%s entries=%d chars=%d (含general继承)",
            domain_id, phase_id, len(all_entries), len(text),
        )

        return text

    def get_injection_plan(
        self,
        entries: List[Dict],
        final_text: str,
    ) -> KnowledgeInjectionPlan:
        """获取注入计划（调试/前端展示用）"""
        return KnowledgeInjectionPlan(
            domain_id=entries[0]["domain_id"] if entries else "",
            phase_id=entries[0].get("phase_id", "") if entries else "",
            total_entries=len(entries),
            injected_entries=len([e for e in entries if e.get("_injected")]),
            total_chars=len(final_text),
            truncated=len(final_text) >= self.MAX_INJECT_CHARS,
            entries_used=[e["id"] for e in entries if e.get("_injected")],
        )

    def _rank_by_relevance(self, entries: List[Dict], task: str) -> List[Dict]:
        """按与任务的相关性排序（关键词匹配）"""
        task_lower = task.lower()

        scored = []
        for entry in entries:
            tags = entry.get("tags", [])
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []

            score = 0.0
            for tag in tags:
                if tag in task_lower or tag in task:
                    score += 2.0

            title = entry.get("title", "")
            if title and title in task:
                score += 3.0

            priority = entry.get("priority", 50) / 100.0
            score *= (0.5 + priority)

            scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])
        return [e for s, e in scored if s > 0]

    def _deduplicate_by_title(self, entries: List[Dict]) -> List[Dict]:
        """同 title 去重，保留 priority 最高的"""
        seen_titles: Dict[str, Dict] = {}
        for entry in entries:
            title = entry.get("title", "")
            if title in seen_titles:
                if entry.get("priority", 0) > seen_titles[title].get("priority", 0):
                    seen_titles[title] = entry
            else:
                seen_titles[title] = entry
        return list(seen_titles.values())

    def _format_entries(self, entries: List[Dict]) -> str:
        """格式化为分类的 markdown 文本（含预算裁剪）"""
        groups: Dict[str, List[Dict]] = {}
        for entry in entries:
            cat = entry.get("category", "norm")
            groups.setdefault(cat, []).append(entry)

        category_order = ["style", "norm", "glossary", "example", "antipattern"]

        parts = []
        total_chars = 0

        for cat in category_order:
            items = groups.get(cat, [])
            if not items:
                continue

            emoji = CATEGORY_EMOJI.get(cat, "📄")
            label = CATEGORY_LABEL.get(cat, cat)
            section_header = f"\n### {emoji} {label}\n"

            if total_chars + len(section_header) > self.MAX_INJECT_CHARS:
                break

            parts.append(section_header)
            total_chars += len(section_header)

            for item in items:
                title = item.get("title", "")
                content = item.get("content", "")

                entry_text = f"\n**{title}**\n{content}\n"

                if total_chars + len(entry_text) > self.MAX_INJECT_CHARS:
                    remaining = self.MAX_INJECT_CHARS - total_chars - len(f"\n**{title}**\n") - 50
                    if remaining > 100:
                        entry_text = f"\n**{title}**\n{content[:remaining]}…\n"
                        parts.append(entry_text)
                        total_chars += len(entry_text)
                    parts.append("\n[更多知识已省略，可通过 recall 工具获取]")
                    return "".join(parts)

                parts.append(entry_text)
                total_chars += len(entry_text)
                item["_injected"] = True

        return "".join(parts)
