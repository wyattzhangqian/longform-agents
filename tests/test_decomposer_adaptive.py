"""PR-4 自适应分解测试 — TaskDecomposer 策略表 + 阶段验收契约。

策略表是纯函数（task_type → 骨架），可单测；acceptance criteria / expected_outputs
经 _parse_decompose_response 解析到 SubTask（阶段契约字段无损）。
"""
import json
import pytest

from core.orchestration.decomposer import TaskDecomposer


# ═══════════════════════════════════════════════════════════
# 策略表
# ═══════════════════════════════════════════════════════════

def test_skeleton_for_known_task_types():
    assert TaskDecomposer.skeleton_for_task_type("code") == ["需求", "方案", "实现", "测试", "修复", "验证"]
    assert TaskDecomposer.skeleton_for_task_type("research") == ["问题定义", "检索", "交叉验证", "综合", "引用审查"]
    assert TaskDecomposer.skeleton_for_task_type("long_content") == ["总纲", "单元计划", "生产", "连贯性", "汇编"]


def test_skeleton_for_unknown_task_type_falls_back_default():
    default = TaskDecomposer._DEFAULT_SKELETON
    assert TaskDecomposer.skeleton_for_task_type("") == default
    assert TaskDecomposer.skeleton_for_task_type("bogus") == default


# ═══════════════════════════════════════════════════════════
# 阶段验收契约解析
# ═══════════════════════════════════════════════════════════

def test_parse_decompose_response_extracts_contract_fields():
    """LLM 返回的 expected_outputs/acceptance_criteria 解析到 SubTask。"""
    d = TaskDecomposer()
    response = json.dumps([
        {
            "title": "资料调研",
            "description": "检索并交叉验证",
            "required_capabilities": ["research"],
            "dependencies": [],
            "expected_outputs": ["research_notes.md"],
            "acceptance_criteria": ["每个结论至少两个来源", "标注置信度"],
        },
        {
            "title": "撰写报告",
            "description": "综合成报告",
            "required_capabilities": ["report_writing"],
            "dependencies": ["资料调研"],
            "expected_outputs": ["report.md"],
            "acceptance_criteria": ["结构清晰", "有引用"],
        },
    ])
    sub_tasks = d._parse_decompose_response("任务", response, ["research", "report_writing"], 10)
    assert len(sub_tasks) == 2
    assert sub_tasks[0].expected_outputs == ["research_notes.md"]
    assert sub_tasks[0].expected_artifacts == ["research_notes.md"]
    assert sub_tasks[0].acceptance_criteria == ["每个结论至少两个来源", "标注置信度"]
    # dependencies 仍正确解析（title → id）
    assert sub_tasks[1].dependencies == [sub_tasks[0].id]


def test_parse_decompose_response_missing_contract_fields():
    """LLM 未返回 contract 字段 → 默认空（不崩）。"""
    d = TaskDecomposer()
    response = json.dumps([{"title": "执行", "description": "d"}])
    sub_tasks = d._parse_decompose_response("t", response, [], 5)
    assert sub_tasks[0].expected_outputs == []
    assert sub_tasks[0].acceptance_criteria == []


# ═══════════════════════════════════════════════════════════
# prompt 构建
# ═══════════════════════════════════════════════════════════

def test_build_prompt_uses_task_type_skeleton():
    d = TaskDecomposer()
    prompt = d._build_decompose_prompt("写代码", ["coding"], 5, task_type="code")
    assert "需求 → 方案 → 实现 → 测试 → 修复 → 验证" in prompt
    assert "acceptance_criteria" in prompt
    assert "expected_outputs" in prompt


def test_build_prompt_injects_domain_prior():
    d = TaskDecomposer()
    prior_text = "领域「网文小说」的阶段骨架：\n1. 大纲：故事骨架"
    prompt = d._build_decompose_prompt("写小说", ["writing"], 5, domain_prior_text=prior_text)
    assert "领域「网文小说」" in prompt
    assert "大纲" in prompt


def test_build_prompt_without_task_type_uses_default_skeleton():
    d = TaskDecomposer()
    prompt = d._build_decompose_prompt("任务", ["general"], 5)
    assert "调研/分析 → 构思/设计 → 初稿创作 → 润色完善 → 检查输出" in prompt
