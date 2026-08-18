"""氛围/情绪追踪器 — 追踪角色情绪状态和场景氛围"""
from __future__ import annotations
from typing import Any, Dict, List, Optional


class AtmosphereTracker:
    """
    追踪角色情绪状态和场景氛围。
    数据来源：Agent 产出 metadata 中的 atmosphere_changes 声明。
    存储在 RunContext.atmosphere_state 字段。
    """

    def __init__(self, state: Optional[Dict[str, Any]] = None):
        data = state or {}
        self.characters: Dict[str, Dict[str, Any]] = data.get("characters", {})
        self.scene: Dict[str, Any] = data.get("scene", {
            "current_tone": "",
            "tension_level": 0.5,
            "tension_trend": "stable",
        })
        self.rhythm_history: List[str] = data.get("rhythm_history", [])

    def to_state(self) -> Dict[str, Any]:
        return {
            "characters": self.characters,
            "scene": self.scene,
            "rhythm_history": self.rhythm_history,
        }

    def update_from_declaration(self, unit_number: int, metadata: Dict[str, Any]):
        """从 Agent 产出的 metadata 更新状态"""
        # 更新角色情绪
        changes = metadata.get("atmosphere_changes", {})
        for char_id, change in changes.items():
            if char_id not in self.characters:
                self.characters[char_id] = {}
            char = self.characters[char_id]
            char["primary_state"] = change.get("to", change.get("primary_state", ""))
            char["intensity"] = change.get("intensity", 0.5)
            char["cause"] = change.get("trigger", change.get("cause", ""))
            char["caused_at"] = unit_number
            # 简化的恢复进度：每个单元推进 0.2
            prev_progress = char.get("recovery_progress", 0)
            if change.get("to") == char.get("previous_state"):
                char["recovery_progress"] = min(1.0, prev_progress + 0.2)
            else:
                char["recovery_progress"] = 0  # 新状态，重置
            char["previous_state"] = change.get("from", "")

        # 更新场景氛围
        if "atmosphere_end" in metadata:
            self.scene["current_tone"] = metadata["atmosphere_end"]
        if "tension_at_end" in metadata:
            prev = self.scene.get("tension_level", 0.5)
            new = metadata["tension_at_end"]
            self.scene["tension_level"] = new
            if new > prev + 0.1:
                self.scene["tension_trend"] = "rising"
            elif new < prev - 0.1:
                self.scene["tension_trend"] = "falling"
            else:
                self.scene["tension_trend"] = "stable"

        # 更新节奏历史
        pacing = metadata.get("pacing_actual", "")
        if pacing:
            self.rhythm_history.append(pacing)
            if len(self.rhythm_history) > 10:
                self.rhythm_history = self.rhythm_history[-10:]

    def format_for_context(self, appearing_characters: List[str] = None) -> str:
        """格式化为可注入 Agent context 的氛围指导"""
        lines = []

        # 角色情绪
        chars_to_show = appearing_characters or list(self.characters.keys())
        relevant_chars = {k: v for k, v in self.characters.items() if k in chars_to_show}

        if relevant_chars:
            lines.append("## 角色当前状态\n")
            for char_id, state in relevant_chars.items():
                primary = state.get("primary_state", "未知")
                cause = state.get("cause", "")
                caused_at = state.get("caused_at", "?")
                recovery = state.get("recovery_progress", 0)
                lines.append(f"### {char_id}")
                lines.append(f"- 状态：{primary}")
                if cause:
                    lines.append(f"- 原因：{cause}（第 {caused_at} 单元）")
                if recovery > 0:
                    lines.append(f"- 恢复进度：{int(recovery * 100)}%")
                lines.append(f"- ⚠️ 无重大事件触发不应突变为相反状态\n")

        # 场景氛围
        if self.scene.get("current_tone"):
            lines.append("## 场景氛围")
            lines.append(f"- 当前：{self.scene['current_tone']}")
            lines.append(f"- 张力：{self.scene.get('tension_level', 0.5)}（{self.scene.get('tension_trend', 'stable')}）")

        return "\n".join(lines) if lines else ""
