"""协商台账测试 — 约束提取 / 台账 / 覆盖校验 / 人类插话（D6 协作闭环核心路径）"""

import pytest

from core.run.run_context import RunContext, NegotiationEntry, HumanInterjection
from core.run import negotiation


@pytest.fixture
def ctx():
    return RunContext(project_id="", task="测试任务")


class TestHeuristicExtraction:
    def test_extracts_bullets(self):
        answer = (
            "- 全文统一使用 16:9 画幅\n"
            "- 主角服装保持红色风衣\n"
            "普通叙述行不是约束"
        )
        items = negotiation.extract_constraints_heuristic(answer)
        assert any("16:9" in i for i in items)
        assert any("红色风衣" in i for i in items)

    def test_skips_markdown_noise(self):
        answer = (
            "**[上游 · 回复]**\n"
            "# 标题行\n"
            "> 引用行\n"
            "- 结尾是冒号的标题：\n"
            "- 有效约束：分辨率固定 1920x1080 输出"
        )
        items = negotiation.extract_constraints_heuristic(answer)
        assert all(not i.startswith(("#", "[", "*", ">")) for i in items)
        assert any("1920x1080" in i for i in items)

    def test_empty_answer(self):
        assert negotiation.extract_constraints_heuristic("") == []


class TestLedger:
    def test_add_constraints_dedup(self, ctx):
        added1 = negotiation.add_constraints(
            ctx, ["约束 A", "约束 B"],
            phase_id="p1", from_agent="a1", to_agent="a2",
        )
        added2 = negotiation.add_constraints(
            ctx, ["约束 A", "约束 C"],
            phase_id="p1", from_agent="a1", to_agent="a2",
        )
        assert len(added1) == 2
        assert len(added2) == 1  # 约束 A 去重
        assert len(ctx.negotiation_ledger) == 3

    def test_confirmed_constraints_filter(self, ctx):
        negotiation.add_constraints(
            ctx, ["约束 A"], phase_id="p1", from_agent="a", to_agent="b",
        )
        negotiation.add_open_question(ctx, "未决问题", phase_id="p1", from_agent="a")
        confirmed = ctx.confirmed_constraints()
        assert len(confirmed) == 1
        assert confirmed[0].kind == "constraint"
        assert len(ctx.open_negotiations()) == 1

    def test_resolve_entry(self, ctx):
        entry = negotiation.add_open_question(ctx, "问题", phase_id="p1", from_agent="a")
        assert negotiation.resolve_entry(ctx, entry.id, status="resolved") is True
        assert ctx.open_negotiations() == []

    def test_resolve_missing_entry(self, ctx):
        assert negotiation.resolve_entry(ctx, "neg_nonexistent") is False


class TestPromptInjection:
    def test_format_includes_constraints_and_interjections(self, ctx):
        negotiation.add_constraints(
            ctx, ["画幅 16:9"], phase_id="p1", from_agent="a", to_agent="b",
        )
        ctx.human_interjections.append(HumanInterjection(
            id="ij_1", content="改成水墨风", target_agent="",
        ))
        text = negotiation.format_for_prompt(ctx, "agent_x", "p1")
        assert "已确认约束" in text
        assert "16:9" in text
        assert "用户插话" in text
        assert "水墨风" in text

    def test_empty_context_returns_empty(self, ctx):
        assert negotiation.format_for_prompt(ctx, "agent_x") == ""


class TestInterjections:
    def test_consume_marks_agent(self, ctx):
        ctx.human_interjections.append(HumanInterjection(id="ij_1", content="注意安全边界"))
        assert len(ctx.pending_interjections("a1")) == 1
        consumed = negotiation.consume_interjections(ctx, "a1")
        assert consumed == 1
        assert ctx.pending_interjections("a1") == []

    def test_targeted_interjection(self, ctx):
        ctx.human_interjections.append(HumanInterjection(
            id="ij_2", content="只给 a2 的指令", target_agent="a2",
        ))
        assert ctx.pending_interjections("a1") == []
        assert len(ctx.pending_interjections("a2")) == 1


class TestConstraintCoverage:
    def test_coverage_hit(self):
        assert negotiation.check_constraint_coverage(
            "输出分辨率固定 1920x1080，格式 PNG",
            "本次产出为 1920x1080 的 PNG 文件",
        ) is True

    def test_coverage_miss(self):
        assert negotiation.check_constraint_coverage(
            "主角服装必须为红色风衣 trench-coat 样式",
            "输出了一段完全无关的文本",
        ) is False

    def test_evaluate_constraints_updates_ledger(self, ctx):
        negotiation.add_constraints(
            ctx, ["分辨率固定 1920x1080"],
            phase_id="p1", from_agent="a", to_agent="b",
        )
        violations = negotiation.evaluate_constraints(ctx, "p1", "b", "完全无关的产出")
        assert len(violations) == 1
        assert violations[0]["rule_id"] == "constraints_coverage"
        assert ctx.negotiation_ledger[0].status == "violated"

        # 修复后再评估 → satisfied 且无违规
        violations2 = negotiation.evaluate_constraints(
            ctx, "p1", "b", "已按 1920x1080 分辨率输出",
        )
        assert violations2 == []
        assert ctx.negotiation_ledger[0].status == "satisfied"


class TestRunContextRoundtrip:
    def test_negotiation_state_survives_serialization(self, ctx):
        """协商台账 + 插话经 model_dump/model_validate 往返不丢失（checkpoint 路径）"""
        negotiation.add_constraints(
            ctx, ["约束 X"], phase_id="p1", from_agent="a", to_agent="b",
        )
        ctx.human_interjections.append(HumanInterjection(id="ij_1", content="插话内容"))

        data = ctx.model_dump()
        restored = RunContext.model_validate(data)
        assert len(restored.negotiation_ledger) == 1
        assert restored.negotiation_ledger[0].text == "约束 X"
        assert len(restored.human_interjections) == 1
        assert restored.human_interjections[0].content == "插话内容"
