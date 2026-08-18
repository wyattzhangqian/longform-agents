"""知识引用检测 — 检测 Agent 输出是否引用了注入的知识

用于知识效果反馈：记录哪些知识被 Agent 实际采纳。
"""
from __future__ import annotations
from typing import Dict, List
from core.knowledge import KnowledgeItem


class KnowledgeReferenceDetector:
    """检测 Agent 输出是否引用了注入的知识条目

    策略（任一命中即标记为引用）：
    1. 标题匹配（≥4 字）
    2. 内容前 50 字关键词匹配（≥8 字）
    3. tag 匹配（≥3 字）
    """

    @staticmethod
    def detect(injected: List[KnowledgeItem], output: str) -> Dict[str, bool]:
        """返回 {item_id: referenced} 映射"""
        if not output or not injected:
            return {}
        out = output.lower()
        result: Dict[str, bool] = {}
        for item in injected:
            hit = False
            # 标题
            title = getattr(item, "title", "") or ""
            if title and len(title) >= 4 and title.lower() in out:
                hit = True
            # 内容前 50 字
            if not hit and item.content:
                snippet = item.content[:50].lower()
                if len(snippet) >= 8 and snippet in out:
                    hit = True
            # tags
            if not hit and item.tags:
                for tag in item.tags:
                    if len(tag) >= 3 and tag.lower() in out:
                        hit = True
                        break
            result[item.id] = hit
        return result
