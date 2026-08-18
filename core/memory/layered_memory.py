"""分层记忆管理 — 为长内容创作提供结构化上下文"""
from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LayeredMemory:
    """
    五层记忆结构：
      L4: 全局设定（始终注入，不受 token budget 裁剪）
      L3: Arc 级摘要（当前 Arc 的进展总结）
      L2: 滑动窗口（最近 N 个单元的精炼摘要）
      L1: 当前单元工作记忆（UnitSpec + 连续性提醒 + 氛围指导）
      L0: 按需检索（RAG，Agent 通过工具调用触发）
    """

    WINDOW_SIZE = 5  # L2 保留最近几个单元

    def __init__(self):
        self.L4: str = ""                    # 全局设定文本
        self.L3: str = ""                    # 当前 Arc 摘要
        self.L2: List[Dict[str, Any]] = []   # 滑动窗口 [{unit_number, summary}]
        self.L1: str = ""                    # 当前单元工作记忆

    @classmethod
    def from_run_context(cls, ctx_dict: dict) -> "LayeredMemory":
        """从 RunContext 的 layered_memory 字段恢复"""
        mem = cls()
        data = ctx_dict or {}
        mem.L4 = data.get("L4", "")
        mem.L3 = data.get("L3", "")
        mem.L2 = data.get("L2", [])
        mem.L1 = data.get("L1", "")
        return mem

    def to_dict(self) -> dict:
        """序列化为可存入 RunContext 的 dict"""
        return {
            "L4": self.L4,
            "L3": self.L3,
            "L2": self.L2,
            "L1": self.L1,
        }

    def build_L4(self, skeleton_dict: dict):
        """
        从骨架构建 L4 全局设定。
        包含：premise + theme + 角色列表摘要 + 核心世界观规则。
        """
        lines = ["## 全局设定（始终有效）\n"]
        lines.append(f"**前提：** {skeleton_dict.get('premise', '')}")
        lines.append(f"**主题：** {skeleton_dict.get('theme', '')}")
        lines.append(f"**内容类型：** {skeleton_dict.get('content_type', '')}")
        lines.append(f"**总单元数：** {skeleton_dict.get('total_units', 0)}")

        # 提取角色信息（从 Arc 的 character_arcs 汇总）
        characters = set()
        for arc in skeleton_dict.get("arcs", []):
            for ca in arc.get("character_arcs", []):
                characters.add(ca.get("character", ""))
        if characters:
            lines.append(f"\n**主要角色：** {', '.join(characters)}")

        self.L4 = "\n".join(lines)

    def build_L3(self, arc_dict: Optional[dict], completed_summary: str = ""):
        """从当前 Arc 信息构建 L3"""
        if not arc_dict:
            self.L3 = ""
            return
        lines = [f"## 当前阶段：{arc_dict.get('title', '')}"]
        lines.append(f"核心冲突：{arc_dict.get('core_conflict', '')}")
        if completed_summary:
            lines.append(f"\n已完成进展：\n{completed_summary}")
        self.L3 = "\n".join(lines)

    def push_to_L2(self, unit_number: int, summary: str):
        """推入一条新的单元摘要到滑动窗口"""
        self.L2.append({"unit_number": unit_number, "summary": summary})
        # 超出窗口大小则移除最早的
        if len(self.L2) > self.WINDOW_SIZE:
            self.L2 = self.L2[-self.WINDOW_SIZE:]

    def format_L2(self) -> str:
        """格式化滑动窗口为可注入文本"""
        if not self.L2:
            return ""
        lines = ["## 近期进展\n"]
        for item in self.L2:
            lines.append(f"### 第 {item['unit_number']} 单元")
            lines.append(item["summary"])
            lines.append("")
        return "\n".join(lines)

    def build_context_blocks(self) -> Dict[str, str]:
        """返回各层的文本块，供 ContextBuilder 使用"""
        return {
            "L4": self.L4,
            "L3": self.L3,
            "L2": self.format_L2(),
            "L1": self.L1,
        }

    def total_tokens_estimate(self) -> int:
        """粗略估算各层 token 总量"""
        total = len(self.L4) + len(self.L3) + len(self.format_L2()) + len(self.L1)
        return int(total * 1.5)  # 1 char ≈ 1.5 tokens（跟现有 ContextBuilder 一致）
