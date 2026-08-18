"""领域知识库数据模型

KnowledgeEntry: 单条知识条目（规范/范例/反面案例/术语/风格指南）
KnowledgeCategory: 5 种知识类别
InjectMode: 3 种注入模式
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class KnowledgeCategory(str, Enum):
    """知识类别"""
    NORM = "norm"               # 📐 规范文档
    EXAMPLE = "example"         # 📝 优秀范例
    ANTIPATTERN = "antipattern" # 🚫 反面案例
    GLOSSARY = "glossary"       # 📖 术语表
    STYLE = "style"             # 🎯 风格指南


class InjectMode(str, Enum):
    """注入模式"""
    ALWAYS = "always"           # 无条件注入
    RELEVANT = "relevant"       # 按任务相关性检索后注入
    ON_DEMAND = "on_demand"     # Agent 主动 recall 时才提供


CATEGORY_EMOJI = {
    "norm": "📐",
    "example": "📝",
    "antipattern": "🚫",
    "glossary": "📖",
    "style": "🎯",
}

CATEGORY_LABEL = {
    "norm": "规范文档",
    "example": "优秀范例",
    "antipattern": "反面案例",
    "glossary": "术语表",
    "style": "风格指南",
}


class KnowledgeEntry(BaseModel):
    """单条知识条目"""
    id: str = ""
    domain_id: str
    phase_id: str = ""
    category: KnowledgeCategory = KnowledgeCategory.NORM
    title: str
    content: str
    tags: List[str] = Field(default_factory=list)
    priority: int = 50
    inject_mode: InjectMode = InjectMode.ALWAYS
    inject_to: str = ""
    source: str = "platform"
    owner_id: str = ""
    char_count: int = 0
    sort_order: int = 0
    is_archived: bool = False


class KnowledgeImportResult(BaseModel):
    """导入结果"""
    entries: List[KnowledgeEntry]
    total_parsed: int
    needs_confirmation: bool = True


class KnowledgeInjectionPlan(BaseModel):
    """注入计划（调试用）"""
    domain_id: str
    phase_id: str
    total_entries: int
    injected_entries: int
    total_chars: int
    truncated: bool
    entries_used: List[str]
