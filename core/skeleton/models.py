"""骨架数据模型 — 内容形态无关"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RhythmPoint(BaseModel):
    """节奏曲线上的一个标记点"""
    unit_number: int
    pacing: str  # exposition | rising | climax | falling | resolution
    tension: float = 0.5  # 0-1


class ContinuityThread(BaseModel):
    """连续性线索（伏笔/视觉回调/角色弧线等）"""
    id: str
    type: str = "plot_foreshadow"  # plot_foreshadow | visual_callback | motif | character_arc | item
    content: str
    significance: str = ""
    introduce_at: int  # 引入单元号
    reference_at: List[int] = Field(default_factory=list)
    resolve_at: int  # 解决单元号
    scope: str = "global"  # global | arc
    status: str = "planned"  # planned | introduced | referenced | resolved | overdue


class UnitDefinition(BaseModel):
    """定义'一个创作单元'是什么"""
    name: str = "章"  # 章/话/集/关卡
    deliverables: List[str] = Field(default_factory=lambda: ["text"])
    metrics: List[Dict[str, str]] = Field(default_factory=list)
    # 例: [{"name": "word_count", "unit": "字", "target": "3000-4000"}]


class UnitSpec(BaseModel):
    """单个创作单元的规格说明"""
    unit_number: int
    goal: str
    pacing: str = "exposition"
    critical: bool = False

    # 连续性管理
    continuity_introduce: List[str] = Field(default_factory=list)
    continuity_reference: List[str] = Field(default_factory=list)
    continuity_resolve: List[str] = Field(default_factory=list)

    # 氛围
    atmosphere_target: str = ""

    # 内容形态特有字段（不同 content_type 放不同 key）
    type_specific: Dict[str, Any] = Field(default_factory=dict)
    # 小说: {"pov": "张三", "word_count_target": [3000, 4000]}
    # 漫画: {"page_count": 20, "key_panels": [...]}
    # 剧集: {"duration_minutes": 45, "locations": [...]}

    # 依赖（哪些单元必须先完成）
    depends_on: List[int] = Field(default_factory=list)


class Arc(BaseModel):
    """一个叙事弧（卷/季/幕）"""
    id: str
    title: str
    unit_range: List[int]  # [start, end]，如 [1, 25]
    core_conflict: str
    resolution: str = ""
    character_arcs: List[Dict[str, str]] = Field(default_factory=list)
    # [{"character": "张三", "from": "封闭自我", "to": "学会信任"}]


class Skeleton(BaseModel):
    """全局骨架 — 长内容项目的结构蓝图"""
    project_id: str = ""
    content_type: str = "novel"  # novel | manga | series | game | custom
    premise: str = ""
    theme: str = ""
    total_units: int = 0

    unit_definition: UnitDefinition = Field(default_factory=UnitDefinition)
    arcs: List[Arc] = Field(default_factory=list)
    units: List[UnitSpec] = Field(default_factory=list)
    continuity_threads: List[ContinuityThread] = Field(default_factory=list)
    rhythm_curve: List[RhythmPoint] = Field(default_factory=list)

    # 版本管理
    version: int = 1
    created_at: str = ""
    updated_at: str = ""

    def get_unit(self, unit_number: int) -> Optional[UnitSpec]:
        """获取指定单元的规格"""
        for u in self.units:
            if u.unit_number == unit_number:
                return u
        return None

    def get_arc_for_unit(self, unit_number: int) -> Optional[Arc]:
        """获取单元所属的 Arc"""
        for arc in self.arcs:
            if arc.unit_range[0] <= unit_number <= arc.unit_range[1]:
                return arc
        return None

    def get_threads_for_unit(self, unit_number: int) -> Dict[str, List[ContinuityThread]]:
        """获取跟某单元相关的所有连续性线索"""
        result = {"introduce": [], "reference": [], "resolve": [], "overdue": []}
        for t in self.continuity_threads:
            if t.introduce_at == unit_number:
                result["introduce"].append(t)
            if unit_number in t.reference_at:
                result["reference"].append(t)
            if t.resolve_at == unit_number:
                result["resolve"].append(t)
            if t.status == "overdue":
                result["overdue"].append(t)
        return result
