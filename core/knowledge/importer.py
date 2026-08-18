"""KnowledgeImporter — 知识导入器

Markdown 拆分 + LLM 自动分类 + 自然语言解析
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

from core.knowledge.models_kb import InjectMode, KnowledgeCategory, KnowledgeEntry
from core.logging import get_logger

_logger = get_logger("knowledge.importer")


class KnowledgeImporter:
    """知识导入器：Markdown 拆分 + LLM 自动分类 + 自然语言解析"""

    async def import_markdown(
        self,
        markdown_text: str,
        domain_id: str,
        owner_id: str,
        llm_client=None,
    ) -> List[KnowledgeEntry]:
        """从 Markdown 文本导入知识条目

        拆分策略：
        1. 按 ## 标题拆分为独立条目
        2. 无标题的按空行+长度拆分
        3. LLM 自动分类（可选，无 LLM 时默认 norm）
        """
        sections = self._split_by_heading(markdown_text)

        entries = []
        for title, content in sections:
            if not content.strip():
                continue

            category = KnowledgeCategory.NORM
            inject_mode = InjectMode.ALWAYS
            priority = 50

            if llm_client:
                classification = await self._classify_entry(title, content, llm_client)
                category = classification.get("category", KnowledgeCategory.NORM)
                inject_mode = classification.get("inject_mode", InjectMode.ALWAYS)
                priority = classification.get("priority", 50)
            else:
                category, inject_mode, priority = self._heuristic_classify(title, content)

            entries.append(
                KnowledgeEntry(
                    domain_id=domain_id,
                    category=category,
                    title=title or content[:30].strip(),
                    content=content.strip(),
                    tags=self._extract_tags(title + " " + content),
                    priority=priority,
                    inject_mode=inject_mode,
                    source="user",
                    owner_id=owner_id,
                    char_count=len(content),
                )
            )

        _logger.info("Markdown 导入: %d 个条目从 %d 字文本", len(entries), len(markdown_text))
        return entries

    async def parse_natural_language(
        self,
        user_input: str,
        domain_id: str,
        phase_id: str,
        owner_id: str,
        llm_client,
    ) -> List[KnowledgeEntry]:
        """从用户自然语言描述中解析出知识条目

        用户输入如：
        "我们团队做漫剧有几个规矩：
         1. 第一话第一格必须用远景交代世界观
         2. 每话结尾必须是悬念格"
        → 解析为多条独立的知识条目
        """
        prompt = f"""将以下用户描述的领域知识拆分为结构化条目。

用户输入：
---
{user_input}
---

请返回 JSON 数组，每个元素：
{{
  "title": "简短标题（10字以内）",
  "content": "完整的知识描述（保留原文细节，可适当扩充为完整句子）",
  "category": "norm 或 example 或 antipattern 或 glossary 或 style",
  "phase_id": "适用的阶段ID（不确定则留空）",
  "priority": 50-90之间的整数（越重要越高）,
  "inject_mode": "always 或 relevant",
  "tags": ["关键词1", "关键词2"]
}}

分类规则：
- norm: 必须遵守的规范/规则
- example: 正面示例/模板/范本
- antipattern: 需要避免的错误做法
- glossary: 术语定义/概念解释
- style: 风格偏好/语气/格式要求

返回纯 JSON 数组，无 markdown 包裹。"""

        response = await llm_client.chat(prompt, temperature=0.2)

        parsed = None
        try:
            json_str = response.strip()
            # 去除可能的 markdown 包裹
            if "```" in json_str:
                match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
                if match:
                    json_str = match.group(1).strip()
            parsed = json.loads(json_str)
        except (json.JSONDecodeError, AttributeError):
            _logger.warning("自然语言解析失败，原始响应: %s", response[:200])
            return [
                KnowledgeEntry(
                    domain_id=domain_id,
                    phase_id=phase_id,
                    category=KnowledgeCategory.NORM,
                    title=user_input[:30],
                    content=user_input,
                    tags=[],
                    priority=60,
                    inject_mode=InjectMode.ALWAYS,
                    source="user",
                    owner_id=owner_id,
                    char_count=len(user_input),
                )
            ]

        entries = []
        for item in parsed:
            cat_str = item.get("category", "norm")
            try:
                category = KnowledgeCategory(cat_str)
            except ValueError:
                category = KnowledgeCategory.NORM

            mode_str = item.get("inject_mode", "always")
            try:
                mode = InjectMode(mode_str)
            except ValueError:
                mode = InjectMode.ALWAYS

            entries.append(
                KnowledgeEntry(
                    domain_id=domain_id,
                    phase_id=item.get("phase_id", phase_id),
                    category=category,
                    title=item.get("title", ""),
                    content=item.get("content", ""),
                    tags=item.get("tags", []),
                    priority=item.get("priority", 60),
                    inject_mode=mode,
                    source="user",
                    owner_id=owner_id,
                    char_count=len(item.get("content", "")),
                )
            )

        return entries

    def _split_by_heading(self, text: str) -> List[Tuple[str, str]]:
        """按 ## 标题拆分 markdown"""
        sections = []
        current_title = ""
        current_content = []

        for line in text.split("\n"):
            heading_match = re.match(r"^#{1,3}\s+(.+)", line)
            if heading_match:
                if current_title or current_content:
                    sections.append((current_title, "\n".join(current_content)))
                current_title = heading_match.group(1).strip()
                current_content = []
            else:
                current_content.append(line)

        if current_title or current_content:
            sections.append((current_title, "\n".join(current_content)))

        # 如果只有一个 section 且内容很长，尝试按列表项拆分
        if len(sections) == 1 and len(sections[0][1]) > 500:
            sub_sections = self._split_by_list_items(sections[0][1])
            if len(sub_sections) > 1:
                return sub_sections

        return sections

    def _split_by_list_items(self, text: str) -> List[Tuple[str, str]]:
        """按编号列表项拆分"""
        items = re.split(r"\n(?=\d+[.、)]\s)", text)
        results = []
        for item in items:
            item = item.strip()
            if not item:
                continue
            first_line = item.split("\n")[0][:50]
            results.append((first_line, item))
        return results if len(results) > 1 else [(text[:30], text)]

    def _heuristic_classify(
        self, title: str, content: str
    ) -> Tuple[KnowledgeCategory, InjectMode, int]:
        """启发式分类（无 LLM 时）"""
        text = (title + " " + content).lower()

        if any(kw in text for kw in ["忌", "不要", "避免", "禁止", "错误", "反面"]):
            return KnowledgeCategory.ANTIPATTERN, InjectMode.ALWAYS, 60
        if any(kw in text for kw in ["示例", "范例", "模板", "参考", "example"]):
            return KnowledgeCategory.EXAMPLE, InjectMode.RELEVANT, 50
        if any(kw in text for kw in ["术语", "定义", "概念", "glossary", "名词"]):
            return KnowledgeCategory.GLOSSARY, InjectMode.ALWAYS, 40
        if any(kw in text for kw in ["风格", "语气", "格式", "调性", "style"]):
            return KnowledgeCategory.STYLE, InjectMode.ALWAYS, 70

        return KnowledgeCategory.NORM, InjectMode.ALWAYS, 50

    def _extract_tags(self, text: str) -> List[str]:
        """从文本中提取关键词标签"""
        tags = set()
        for match in re.finditer(r'[「""](.{2,10})[」""]', text):
            tags.add(match.group(1))
        for match in re.finditer(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)*\b", text):
            tags.add(match.group(0).lower())
        return list(tags)[:10]

    async def _classify_entry(self, title: str, content: str, llm_client) -> dict:
        """LLM 辅助分类"""
        prompt = f"""对以下知识条目进行分类。

标题: {title}
内容前200字: {content[:200]}

返回 JSON:
{{"category": "norm|example|antipattern|glossary|style", "inject_mode": "always|relevant", "priority": 30-90}}

规则：
- norm(规范): 必须遵守的规则 → always, priority 60-80
- example(范例): 参考模板 → relevant, priority 40-60
- antipattern(反面): 要避免的 → always, priority 50-70
- glossary(术语): 名词解释 → always, priority 30-50
- style(风格): 偏好指南 → always, priority 70-90"""

        response = await llm_client.chat(prompt, temperature=0.1)
        try:
            json_str = response.strip()
            if "```" in json_str:
                match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
                if match:
                    json_str = match.group(1).strip()
            result = json.loads(json_str)
            result["category"] = KnowledgeCategory(result.get("category", "norm"))
            result["inject_mode"] = InjectMode(result.get("inject_mode", "always"))
            return result
        except (json.JSONDecodeError, ValueError):
            return {
                "category": KnowledgeCategory.NORM,
                "inject_mode": InjectMode.ALWAYS,
                "priority": 50,
            }
