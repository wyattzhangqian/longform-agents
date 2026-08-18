"""Phase 3 测试 — 角色行为 Profile"""
import pytest
from core.agent.character_profile import (
    CharacterProfileStore, CharacterBehaviorProfile,
    BehaviorPattern, SpeechStyle, GrowthStage,
)


def _make_profile(char_id="张三"):
    return CharacterBehaviorProfile(
        character_id=char_id,
        core_identity="沉默寡言的剑客",
        behavioral_patterns=[
            BehaviorPattern(situation="被挑衅", typical_response="冷眼相对", never_do=["大声咆哮"]),
        ],
        speech_style=SpeechStyle(characteristics=["简短"], avoids=["废话"], emotion_expression="沉默"),
        growth_stage=GrowthStage(current="自我封闭", next_milestone="学会信任"),
    )


def test_format_full_profile():
    """有对手戏的角色应输出完整 Profile"""
    store = CharacterProfileStore([_make_profile("张三").model_dump()])
    text = store.format_for_context(appearing_characters=["张三"], all_appearing=["张三"])
    assert "张三" in text
    assert "沉默寡言的剑客" in text
    assert "被挑衅" in text
    assert "大声咆哮" in text  # never_do
    assert "自我封闭" in text  # growth_stage


def test_format_minimal_profile():
    """无对手戏的角色只输出 core_identity"""
    store = CharacterProfileStore([_make_profile("张三").model_dump()])
    text = store.format_for_context(appearing_characters=[], all_appearing=["张三"])
    assert "张三" in text
    assert "沉默寡言的剑客" in text
    # 不应有完整行为模式
    assert "被挑衅" not in text


def test_append_example_caps_at_5():
    """examples 数量不超过 5"""
    profile = _make_profile()
    store = CharacterProfileStore([profile.model_dump()])
    for i in range(10):
        store.append_example("张三", "被挑衅", f"实例{i}")
    p = store.get("张三")
    assert len(p.behavioral_patterns[0].examples) == 5
    # 保留最后 5 个
    assert "实例5" in p.behavioral_patterns[0].examples
    assert "实例9" in p.behavioral_patterns[0].examples
    assert "实例0" not in p.behavioral_patterns[0].examples


def test_serialization_roundtrip():
    """to_state / 构造 互为逆操作"""
    store = CharacterProfileStore([_make_profile("张三").model_dump()])
    state = store.to_state()
    store2 = CharacterProfileStore(state)
    p = store2.get("张三")
    assert p is not None
    assert p.core_identity == "沉默寡言的剑客"
