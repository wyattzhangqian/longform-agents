"""QualityGateway + RuleEngine 测试 — 覆盖规则评估/通过判定/重试/历史"""

import pytest
from core.gateway import QualityGateway, QualityRule, RuleEngine


class TestQualityRule:
    """QualityRule 模型测试"""

    def test_create_rule(self):
        rule = QualityRule(
            rule_id="test_1",
            name="测试规则",
            check_type="generic",
            severity="warning",
        )
        assert rule.rule_id == "test_1"
        assert rule.severity == "warning"
        assert rule.config == {}

    def test_rule_with_config(self):
        rule = QualityRule(
            rule_id="struct",
            name="结构检查",
            check_type="structure",
            severity="error",
            config={"required_fields": ["title", "content"]},
        )
        assert len(rule.config["required_fields"]) == 2

    def test_rule_exclude_check_fn(self):
        """check_fn 应被排除在序列化之外"""
        rule = QualityRule(
            rule_id="custom",
            name="自定义",
            check_type="custom",
            check_fn=lambda r, d: None,
        )
        d = rule.model_dump()
        assert "check_fn" not in d


class TestRuleEngineCheck:
    """规则引擎核心检查逻辑"""

    def test_check_empty_rules(self):
        violations = RuleEngine.check([], {"result": "anything"})
        assert violations == []

    def test_check_generic_passes_non_empty(self):
        rules = [QualityRule(rule_id="g1", name="非空", check_type="generic", severity="error")]
        violations = RuleEngine.check(rules, "some content")
        assert len(violations) == 0  # 非空字符串应通过

    def test_check_generic_fails_empty_string(self):
        rules = [QualityRule(rule_id="g1", name="非空", check_type="generic", severity="error")]
        violations = RuleEngine.check(rules, "")
        assert len(violations) == 1
        assert violations[0]["severity"] == "error"

    def test_check_generic_fails_none(self):
        rules = [QualityRule(rule_id="g1", name="非空", check_type="generic", severity="error")]
        violations = RuleEngine.check(rules, None)
        assert len(violations) == 1

    def test_check_generic_fails_empty_dict(self):
        rules = [QualityRule(rule_id="g1", name="非空", check_type="generic", severity="error")]
        violations = RuleEngine.check(rules, {})
        assert len(violations) == 1

    def test_check_structure_passes(self):
        rules = [
            QualityRule(
                rule_id="req_fields",
                name="必需字段",
                check_type="structure",
                severity="error",
                config={"required_fields": ["title", "content"]},
            )
        ]
        result = {"title": "测试", "content": "内容", "extra": "ok"}
        violations = RuleEngine.check(rules, result)
        assert len(violations) == 0

    def test_check_structure_fails_missing_field(self):
        rules = [
            QualityRule(
                rule_id="req_fields",
                name="必需字段",
                check_type="structure",
                severity="error",
                config={"required_fields": ["title"]},
            )
        ]
        result = {"content": "没有 title"}
        violations = RuleEngine.check(rules, result)
        assert len(violations) == 1
        assert "title" in violations[0]["message"]

    def test_check_structure_falsy_field_counts_missing(self):
        """空字符串/None/0 值的字段视为缺失"""
        rules = [
            QualityRule(
                rule_id="req",
                name="必需字段",
                check_type="structure",
                severity="error",
                config={"required_fields": ["title"]},
            )
        ]
        violations = RuleEngine.check(rules, {"title": ""})
        assert len(violations) == 1

    def test_check_structure_non_dict_skipped(self):
        """非 dict 输入不执行 structure 检查"""
        rules = [
            QualityRule(rule_id="s1", name="结构", check_type="structure", severity="error")
        ]
        violations = RuleEngine.check(rules, "不是 dict")
        assert len(violations) == 0  # 返回 None，不计为 violation

    def test_check_consistency_detects_issues(self):
        """一致性检查在缺必要章节时返回 violation"""
        rules = [
            QualityRule(
                rule_id="c1", name="一致性", check_type="consistency", severity="warning",
                config={"required_sections": ["必须有的章节"]},
            )
        ]

        # 不包含必要章节 → 有 violation
        violations = RuleEngine.check(rules, "缺少内容的纯文本")
        assert len(violations) >= 1
        assert "必须有的章节" in violations[0]["message"]

        # 包含必要章节 → 无 violation
        violations_ok = RuleEngine.check(rules, "这是必须有的章节内容")
        assert len(violations_ok) == 0

    def test_check_custom_fn_called(self):
        """自定义检查函数应被调用"""
        called = []

        def custom_fn(rule, data):
            called.append(True)
            return {"rule_id": rule.rule_id, "severity": "error", "message": "custom fail"}

        rules = [
            QualityRule(
                rule_id="custom1",
                name="自定义",
                check_type="my_type",
                severity="error",
                check_fn=custom_fn,
            )
        ]
        violations = RuleEngine.check(rules, "any data")
        assert len(violations) == 1
        assert len(called) == 1
        assert violations[0]["message"] == "custom fail"

    def test_check_custom_fn_exception_fallback(self):
        """自定义函数抛异常时静默回退"""
        def broken_fn(rule, data):
            raise RuntimeError("broken!")

        rules = [
            QualityRule(
                rule_id="broken",
                name="坏掉的",
                check_type="broken_type",
                severity="error",
                check_fn=broken_fn,
            )
        ]
        # 不应有内置 handler 处理 broken_type，所以返回空
        violations = RuleEngine.check(rules, "data")
        # broken_fn 异常被捕获，无 fallback handler → 无 violation
        assert len(violations) == 0

    def test_check_input_dict_auto_wrap(self):
        """传入 dict 自动包装为 CheckInput"""
        rules = [
            QualityRule(rule_id="g1", name="通用", check_type="generic", severity="error")
        ]
        # dict 但非 CheckInput 实例 → 自动包装
        violations = RuleEngine.check(rules, {"result": "hello"})
        assert len(violations) == 0  # 有内容，通过

    def test_multiple_rules_all_violate(self):
        rules = [
            QualityRule(rule_id="r1", name="R1", check_type="generic", severity="error"),
            QualityRule(rule_id="r2", name="R2", check_type="generic", severity="warning"),
        ]
        violations = RuleEngine.check(rules, None)
        assert len(violations) == 2  # 两条都违反（空数据）


class TestRuleEnginePasses:
    """passes() 判定方法"""

    def test_no_violations_passes(self):
        assert RuleEngine.passes([]) is True

    def test_only_warnings_passes(self):
        violations = [
            {"rule_id": "w1", "severity": "warning", "message": "warn"},
            {"rule_id": "i1", "severity": "info", "message": "info"},
        ]
        assert RuleEngine.passes(violations) is True

    def test_error_does_not_pass(self):
        violations = [{"rule_id": "e1", "severity": "error", "message": "err"}]
        assert RuleEngine.passes(violations) is False

    def test_mixed_severities_error_dominates(self):
        violations = [
            {"rule_id": "w1", "severity": "warning", "message": "w"},
            {"rule_id": "e1", "severity": "error", "message": "e"},
        ]
        assert RuleEngine.passes(violations) is False


class TestQualityGatewayEvaluate:
    """QualityGateway evaluate 集成测试"""

    def test_evaluate_empty_gateway(self, empty_gateway):
        violations = empty_gateway.evaluate("agent_1", "result")
        assert violations == []

    def test_evaluate_all_rules(self, sample_gateway, sample_agent_result):
        violations = sample_gateway.evaluate("writer", sample_agent_result)
        # sample_agent_result 有 title 和足够长的 content → 应通过所有检查
        error_vs = [v for v in violations if v["severity"] == "error"]
        assert len(error_vs) == 0

    def test_evaluate_bad_result(self, sample_gateway, bad_agent_result):
        violations = sample_gateway.evaluate("writer", bad_agent_result)
        # bad_agent_result 缺 title → error; content 太短 → warning
        assert len(violations) >= 1

    def test_evaluate_none_result_triggers_generic(self, sample_gateway):
        violations = sample_gateway.evaluate("agent_x", None)
        generic_v = [v for v in violations if v["rule_id"] == "min_content" or "空" in v.get("message", "")]
        assert len(violations) > 0

    def test_evaluate_custom_fn_gateway(self, custom_fn_gateway):
        short_text = "太短了"
        violations = custom_fn_gateway.evaluate("a", short_text)
        assert any(v["rule_id"] == "custom_len" for v in violations)

    def test_evaluate_custom_fn_gateway_pass(self, custom_fn_gateway):
        # custom_len 规则要求至少 50 字符
        long_text = "这是一段足够长的文本用于测试自定义长度检查函数是否正常工作，需要超过五十个字符才能通过该自定义规则的最小长度校验条件。"
        violations = custom_fn_gateway.evaluate("a", long_text)
        assert len(violations) == 0


class TestQualityGatewayHistory:
    """检查历史记录"""

    def test_history_recorded(self, sample_gateway, sample_agent_result):
        sample_gateway.evaluate("a1", sample_agent_result)
        sample_gateway.evaluate("a2", {"title": "test"})
        history = sample_gateway.get_history()
        assert len(history) == 2
        assert history[0]["agent_id"] == "a1"
        assert "violations_count" in history[0]

    def test_clear_history(self, sample_gateway, sample_agent_result):
        sample_gateway.evaluate("a1", sample_agent_result)
        sample_gateway.clear_history()
        assert len(sample_gateway.get_history()) == 0


class TestQualityGatewayShouldRetry:
    """should_retry 重试判断"""

    def test_no_errors_no_retry(self, sample_gateway, sample_agent_result):
        sample_gateway.evaluate("a1", sample_agent_result)
        assert sample_gateway.should_retry([]) is False

    def test_retry_under_max(self, custom_fn_gateway):
        """连续失败但未达上限时应建议重试"""
        for _ in range(2):
            violations = custom_fn_gateway.evaluate("a", "短")
        should = custom_fn_should_retry_helper(custom_fn_gateway)
        assert should is True

    def test_retry_exceeds_max(self, custom_fn_gateway):
        """超过 max_retries 后不应再重试"""
        for _ in range(4):
            custom_fn_gateway.evaluate("a", "短")
        should = custom_fn_should_retry_helper(custom_fn_gateway, max_retries=2)
        assert should is False

    def test_only_warning_no_retry(self, sample_gateway, bad_agent_result):
        """只有 warning 没有 error 时不应自动重试"""
        violations = sample_gateway.evaluate("a", bad_agent_result)
        errors_only = [v for v in violations if v["severity"] == "error"]
        if not errors_only:
            assert sample_gateway.should_retry(violations) is False


def custom_fn_should_retry_helper(gateway: QualityGateway, max_retries: int = 3) -> bool:
    """helper: 获取 latest violations 并调用 should_retry"""
    history = gateway.get_history()
    if not history:
        return False
    latest = history[-1]
    return gateway.should_retry(latest["violations"], max_retries=max_retries)


class TestQualityGatewayPipeline:
    """Pipeline 分类型检查"""

    def test_set_pipeline(self, sample_gateway):
        sample_gateway.set_pipeline(["structure", "generic"])
        assert sample_gateway._pipeline == ["structure", "generic"]

    def test_evaluate_specific_types(self, sample_gateway):
        violations = sample_gateway.evaluate(
            "a", {"title": "ok"}, check_types=["structure"]
        )
        # 只执行了 structure 检查，generic 未执行
        for v in violations:
            assert "generic" not in v.get("rule_id", "")

    def test_add_rules_dedup(self, empty_gateway):
        rule = QualityRule(rule_id="dup", name="D", check_type="generic")
        empty_gateway.add_rules([rule])
        empty_gateway.add_rules([rule])  # 重复添加
        assert len(empty_gateway._rules) == 1
