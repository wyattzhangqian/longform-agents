"""角色行为模式库 — 结构化的角色 Profile 管理"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BehaviorPattern(BaseModel):
    situation: str                          # 触发情境
    typical_response: str                   # 典型反应
    never_do: List[str] = Field(default_factory=list)  # 硬规则
    examples: List[str] = Field(default_factory=list)  # 逐渐积累的实例


class SpeechStyle(BaseModel):
    characteristics: List[str] = Field(default_factory=list)
    avoids: List[str] = Field(default_factory=list)
    emotion_expression: str = ""


class RelationshipBehavior(BaseModel):
    target_character: str
    dynamic: str = ""
    typical_interaction: str = ""
    boundaries: List[str] = Field(default_factory=list)


class GrowthStage(BaseModel):
    current: str = ""
    next_milestone: str = ""
    growth_trigger: str = ""


class CharacterBehaviorProfile(BaseModel):
    character_id: str
    core_identity: str = ""
    behavioral_patterns: List[BehaviorPattern] = Field(default_factory=list)
    speech_style: SpeechStyle = Field(default_factory=SpeechStyle)
    relationships: List[RelationshipBehavior] = Field(default_factory=list)
    growth_stage: GrowthStage = Field(default_factory=GrowthStage)


class CharacterProfileStore:
    """角色行为模式库的管理"""

    def __init__(self, profiles: Optional[List[Dict[str, Any]]] = None):
        self.profiles: Dict[str, CharacterBehaviorProfile] = {}
        if profiles:
            for p in profiles:
                profile = CharacterBehaviorProfile.model_validate(p)
                self.profiles[profile.character_id] = profile

    def to_state(self) -> List[Dict[str, Any]]:
        return [p.model_dump() for p in self.profiles.values()]

    def get(self, character_id: str) -> Optional[CharacterBehaviorProfile]:
        return self.profiles.get(character_id)

    def append_example(self, character_id: str, situation: str, example: str):
        """为某角色某情境追加行为实例"""
        profile = self.profiles.get(character_id)
        if not profile:
            return
        for pattern in profile.behavioral_patterns:
            if pattern.situation == situation:
                pattern.examples.append(example)
                # 限制 examples 数量
                if len(pattern.examples) > 5:
                    pattern.examples = pattern.examples[-5:]
                break

    def format_for_context(
        self,
        appearing_characters: List[str],
        all_appearing: List[str] = None,
    ) -> str:
        """
        按需加载角色 Profile 并格式化。
        appearing_characters: 本单元有对手戏的角色（加载完整 Profile）
        all_appearing: 本单元所有出场角色（只加载 core_identity）
        """
        lines = ["## 角色行为指南\n"]
        loaded_full = set()

        # 完整 Profile（有对手戏的角色）
        for char_id in appearing_characters:
            profile = self.profiles.get(char_id)
            if not profile:
                continue
            loaded_full.add(char_id)
            lines.append(f"### {char_id}")
            lines.append(f"**核心：** {profile.core_identity}\n")

            # 行为模式（限制数量，最多 3 个）
            for pattern in profile.behavioral_patterns[:3]:
                lines.append(f"当 {pattern.situation} 时：")
                lines.append(f"  会：{pattern.typical_response}")
                if pattern.never_do:
                    lines.append(f"  绝不会：{'、'.join(pattern.never_do)}")
                if pattern.examples:
                    lines.append(f"  前例：{pattern.examples[-1]}")
                lines.append("")

            # 语言风格
            if profile.speech_style.avoids:
                lines.append(f"不会说：{'、'.join(profile.speech_style.avoids)}")
            if profile.speech_style.emotion_expression:
                lines.append(f"情绪表达：{profile.speech_style.emotion_expression}")

            # 本单元相关关系
            if all_appearing:
                for rel in profile.relationships:
                    if rel.target_character in all_appearing:
                        lines.append(f"\n与 {rel.target_character}：{rel.typical_interaction}")

            lines.append(f"\n当前阶段：{profile.growth_stage.current}\n")

        # 简略信息（出场但无对手戏的角色）
        if all_appearing:
            for char_id in all_appearing:
                if char_id in loaded_full:
                    continue
                profile = self.profiles.get(char_id)
                if profile:
                    lines.append(f"- {char_id}：{profile.core_identity}")

        return "\n".join(lines) if len(lines) > 1 else ""
