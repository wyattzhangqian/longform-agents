"""Phase 3 测试 — 氛围追踪器"""
import pytest
from core.memory.atmosphere_tracker import AtmosphereTracker


def test_update_changes_character_state():
    """声明情绪变化后角色状态应更新"""
    tracker = AtmosphereTracker()
    tracker.update_from_declaration(3, {
        "atmosphere_changes": {
            "张三": {"from": "平静", "to": "愤怒", "trigger": "被背叛", "intensity": 0.8}
        }
    })
    assert "张三" in tracker.characters
    assert tracker.characters["张三"]["primary_state"] == "愤怒"
    assert tracker.characters["张三"]["cause"] == "被背叛"
    assert tracker.characters["张三"]["caused_at"] == 3


def test_tension_trend_detection():
    """张力上升/下降/稳定应正确判断"""
    tracker = AtmosphereTracker()
    # 上升
    tracker.update_from_declaration(1, {"tension_at_end": 0.8})
    assert tracker.scene["tension_trend"] == "rising"
    # 下降
    tracker.update_from_declaration(2, {"tension_at_end": 0.3})
    assert tracker.scene["tension_trend"] == "falling"
    # 稳定
    tracker.update_from_declaration(3, {"tension_at_end": 0.35})
    assert tracker.scene["tension_trend"] == "stable"


def test_format_only_appearing_characters():
    """格式化时只输出本单元出场角色"""
    tracker = AtmosphereTracker()
    tracker.update_from_declaration(1, {
        "atmosphere_changes": {
            "张三": {"to": "愤怒"},
            "李四": {"to": "悲伤"},
        }
    })
    text = tracker.format_for_context(appearing_characters=["张三"])
    assert "张三" in text
    assert "李四" not in text


def test_serialization_roundtrip():
    """to_state / 构造 互为逆操作"""
    tracker = AtmosphereTracker()
    tracker.update_from_declaration(1, {
        "atmosphere_changes": {"张三": {"to": "开心"}},
        "atmosphere_end": "轻松",
        "tension_at_end": 0.3,
    })
    state = tracker.to_state()
    tracker2 = AtmosphereTracker(state=state)
    assert tracker2.characters["张三"]["primary_state"] == "开心"
    assert tracker2.scene["current_tone"] == "轻松"
