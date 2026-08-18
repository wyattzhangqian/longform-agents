"""Ingestor — 多模式输入标准化"""
from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ExistingUnit(BaseModel):
    """用户提供的已有创作单元"""
    unit_number: int
    content: str
    title: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ReferenceWork(BaseModel):
    """用户提供的参考作品"""
    title: str
    content: str  # 全文或片段
    aspects: List[str] = Field(default_factory=list)
    # aspects: ["style", "structure", "pacing", "character"]


class IngestorInput(BaseModel):
    """Ingestor 的统一输入"""
    mode: str = "create"  # create | continue | reference | rewrite | expand
    intent: str = ""
    content_type: str = "novel"
    total_units: int = 0

    # continue 模式
    existing_units: List[ExistingUnit] = Field(default_factory=list)
    continue_from: int = 0

    # reference 模式
    reference_works: List[ReferenceWork] = Field(default_factory=list)

    # rewrite 模式
    rewrite_units: List[int] = Field(default_factory=list)
    rewrite_feedback: str = ""

    # expand 模式
    existing_outline: str = ""


class IngestorOutput(BaseModel):
    """Ingestor 的标准化输出 — 后续流程消费这个"""
    skeleton_override: Optional[Dict[str, Any]] = None  # 如果有，跳过 SkeletonPlanner
    memory_preset: Optional[Dict[str, Any]] = None      # 预填的 layered_memory
    continuity_preset: Optional[List[Dict]] = None      # 预填的连续性线索
    character_profiles_preset: Optional[List[Dict]] = None
    atmosphere_preset: Optional[Dict[str, Any]] = None
    knowledge_entries_to_inject: List[Dict] = Field(default_factory=list)
    start_unit: int = 1
    rag_content_to_index: List[Dict] = Field(default_factory=list)
    # 续写时需要重建 SkeletonPlanner 的参数
    skeleton_planner_params: Dict[str, Any] = Field(default_factory=dict)


class Ingestor:
    """将各种用户输入标准化为平台初始状态"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    async def ingest(self, input: IngestorInput) -> IngestorOutput:
        # Python 3.9 不支持 match-case，用 if-elif-else
        if input.mode == "create":
            return IngestorOutput(start_unit=1)
        elif input.mode == "continue":
            return await self._handle_continue(input)
        elif input.mode == "reference":
            return await self._handle_reference(input)
        elif input.mode == "rewrite":
            return self._handle_rewrite(input)
        elif input.mode == "expand":
            return await self._handle_expand(input)
        else:
            return IngestorOutput(start_unit=1)

    async def _handle_continue(self, input: IngestorInput) -> IngestorOutput:
        """续写模式"""
        from core.ingestor.content_analyzer import ContentAnalyzer
        analyzer = ContentAnalyzer(llm_client=self.llm)
        analysis = await analyzer.analyze(input.existing_units, input.content_type)

        # 预填记忆
        from core.memory.layered_memory import LayeredMemory
        mem = LayeredMemory()
        mem.L4 = analysis.get("global_settings", "")
        # L2 用最后 5 个单元的摘要
        for item in analysis.get("unit_summaries", [])[-5:]:
            mem.push_to_L2(item["unit_number"], item["summary"])

        # RAG 索引
        rag_items = []
        for unit in input.existing_units:
            rag_items.append({
                "content": unit.content,
                "metadata": {"unit_number": unit.unit_number, "title": unit.title},
            })

        return IngestorOutput(
            memory_preset=mem.to_dict(),
            continuity_preset=analysis.get("continuity_threads"),
            character_profiles_preset=analysis.get("character_profiles"),
            atmosphere_preset=analysis.get("current_atmosphere"),
            start_unit=input.continue_from or (len(input.existing_units) + 1),
            rag_content_to_index=rag_items,
            skeleton_planner_params={
                "mode": "continue",
                "completed_units": len(input.existing_units),
                "intent_continuation": input.intent,
                "analysis_summary": analysis.get("summary", ""),
            },
        )

    async def _handle_reference(self, input: IngestorInput) -> IngestorOutput:
        """仿写模式"""
        from core.ingestor.reference_extractor import ReferenceExtractor
        extractor = ReferenceExtractor(llm_client=self.llm)

        knowledge_entries = []
        for work in input.reference_works:
            rules = await extractor.extract(work)
            for rule in rules:
                knowledge_entries.append({
                    "category": rule.get("category", "style"),
                    "title": rule.get("title", ""),
                    "content": rule.get("content", ""),
                    "inject_mode": "always",  # 风格规则始终注入
                })

        return IngestorOutput(
            start_unit=1,
            knowledge_entries_to_inject=knowledge_entries,
        )

    def _handle_rewrite(self, input: IngestorInput) -> IngestorOutput:
        """改写模式 — 标记需要重写的单元，后续由 replay_from 处理"""
        return IngestorOutput(
            start_unit=min(input.rewrite_units) if input.rewrite_units else 1,
            skeleton_planner_params={
                "mode": "rewrite",
                "rewrite_units": input.rewrite_units,
                "rewrite_feedback": input.rewrite_feedback,
            },
        )

    async def _handle_expand(self, input: IngestorInput) -> IngestorOutput:
        """扩写模式 — 用户已有大纲，跳过 Pass 1-2，直接做 Pass 3"""
        return IngestorOutput(
            start_unit=1,
            skeleton_planner_params={
                "mode": "expand",
                "existing_outline": input.existing_outline,
            },
        )
