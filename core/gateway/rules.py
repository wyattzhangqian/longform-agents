"""Quality Rules — 通用规则定义与规则引擎

纯通用层：只提供规则框架和执行引擎。
具体检查逻辑（如剧本字数、图片质量等）由质量领域体系提供（quality_rules 表）。
"""

from __future__ import annotations
import logging
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


class CheckInput(BaseModel):
    """质量检查标准化输入模型

    替代原来 data: Any 的宽泛类型，提供结构化输入。
    """
    agent_id: str = ""
    result: Any = None                                 # Agent 产出结果
    text_content: str = ""                              # 文本内容（便于文本类检查器直接使用）
    structure_keys: List[str] = Field(default_factory=list)  # 产出结构的键列表
    quality_score: float = 0.0                          # 质量评分（如有）
    metadata: Dict[str, Any] = Field(default_factory=dict)


class QualityRule(BaseModel):
    """质量检查规则

    所有规则由 DomainAdapter 在应用层注册，core 层不预设任何业务规则类型。
    check_type 为自由字符串，由应用层定义其语义。
    """
    rule_id: str
    name: str
    description: str = ""
    check_type: str = "generic"                     # 应用层自定义类型
    severity: str = "warning"                       # info | warning | error
    domain_id: str = ""                             # 所属领域插件
    config: Dict[str, Any] = Field(default_factory=dict)
    # 可选的检查函数引用（应用层注入，不参与序列化）
    check_fn: Optional[object] = Field(default=None, exclude=True)


class RuleEngine:
    """规则引擎 — 执行质量检查规则

    内置两个通用检查器：
    - structure: 结构完整性（必需字段检查）
    - consistency: 一致性检查（占位，需应用层提供具体逻辑）

    业务相关的检查器（script/image/video/...）由应用层通过 _check_fn 注入。
    """

    @staticmethod
    def check(rules: List[QualityRule], data: Any) -> List[dict]:
        """对数据执行一组规则，返回违规列表

        Args:
            rules: 规则列表
            data: 可以是 CheckInput、dict 或其他类型（兼容旧调用方式）
        """
        # 自动适配：如果传入 dict 但不是 CheckInput 实例，包装为 CheckInput
        check_input: CheckInput
        if isinstance(data, CheckInput):
            check_input = data
        elif isinstance(data, dict):
            check_input = CheckInput(
                agent_id=data.get("agent_id", ""),
                result=data.get("result", data),
                text_content=str(data.get("result", "") or data.get("content", "")),
                structure_keys=list(data.keys()) if data else [],
                quality_score=float(data.get("quality_score", 0) or 0),
                metadata=data.get("metadata", {}),
            )
        else:
            check_input = CheckInput(result=data, text_content=str(data))

        violations = []
        for rule in rules:
            result = RuleEngine._check_rule(rule, check_input)
            if result:
                violations.append(result)
        return violations

    @staticmethod
    def _check_rule(rule: QualityRule, data: CheckInput) -> Optional[dict]:
        """执行单条规则：优先使用应用层注入的 _check_fn，回退到内置检查器"""
        # 1. 优先使用应用层注入的检查函数
        if rule.check_fn and callable(rule.check_fn):
            try:
                return rule.check_fn(rule, data)
            except Exception as e:
                _logger.warning("检查函数异常 (%s): %s", rule.rule_id, e)
                # fall through to built-in checker — 不静默放行

        # 2. 回退到内置通用检查器
        handler = getattr(RuleEngine, f"_check_{rule.check_type}", None)
        if handler:
            return handler(rule, data)
        return None

    # ==================== 内置通用检查器 ====================

    @staticmethod
    def _check_structure(rule: QualityRule, data: CheckInput) -> Optional[dict]:
        """通用结构完整性检查"""
        result = data.result
        if not isinstance(result, dict):
            return None
        required_fields = rule.config.get("required_fields", [])
        missing = [f for f in required_fields if f not in result or not result[f]]
        if missing:
            return {
                "rule_id": rule.rule_id,
                "severity": rule.severity,
                "message": f"缺少必需字段: {', '.join(missing)}",
            }
        return None

    @staticmethod
    def _check_consistency(rule: QualityRule, data: CheckInput) -> Optional[dict]:
        """基础一致性检查 — 验证必要章节/最小长度

        config 支持:
        - required_sections: list[str] — 产出中必须包含的章节标题关键词
        - min_length: int — 最小长度
        - forbidden_patterns: list[str] — 产出中不应出现的模式
        """
        result_text = ""
        if isinstance(data.result, str):
            result_text = data.result
        elif isinstance(data.result, dict):
            result_text = str(data.result.get("content") or data.result.get("text") or data.result)
        elif data.text_content:
            result_text = data.text_content
        else:
            result_text = str(data.result or "")

        violations: list[str] = []

        # 检查必要章节
        sections = rule.config.get("required_sections", [])
        for section in sections:
            if section.lower() not in result_text.lower():
                violations.append(f"缺少必要章节/内容: {section}")

        # 检查最小长度
        min_len = rule.config.get("min_length", 0)
        if min_len and len(result_text) < min_len:
            violations.append(f"内容长度 {len(result_text)} < 最低要求 {min_len}")

        # 检查禁忌模式
        forbidden = rule.config.get("forbidden_patterns", [])
        for pattern in forbidden:
            if pattern.lower() in result_text.lower():
                violations.append(f"包含不应出现的内容: {pattern[:30]}")

        if violations:
            return {
                "rule_id": rule.rule_id,
                "severity": rule.severity,
                "message": "; ".join(violations[:3]),
            }
        return None

    @staticmethod
    def _check_generic(rule: QualityRule, data: CheckInput) -> Optional[dict]:
        """通用检查器：检查数据非空"""
        if not data.result or (isinstance(data.result, dict) and not data.result):
            return {
                "rule_id": rule.rule_id,
                "severity": rule.severity,
                "message": f"产出数据为空（{rule.name}）",
            }
        return None

    @staticmethod
    def _check_artifact_file(rule: QualityRule, data: CheckInput) -> Optional[dict]:
        """检查期望产物文件是否已落盘（Quality Gate 可选校验）"""
        from core.vault.project_artifact_service import ProjectArtifactService

        project_id = str(data.metadata.get("project_id") or rule.config.get("project_id") or "")
        expected = rule.config.get("expected_files") or []
        if not project_id or not expected:
            return None
        for fname in expected:
            try:
                if ProjectArtifactService.resolve_project_path(project_id, str(fname)).is_file():
                    return None
            except ValueError:
                continue
        return {
            "rule_id": rule.rule_id,
            "severity": rule.severity,
            "message": f"缺少期望产物: {', '.join(str(f) for f in expected)}",
        }

    @staticmethod
    def passes(violations: List[dict]) -> bool:
        """检查是否所有规则都通过（无 error 级别违规）"""
        return not any(v["severity"] == "error" for v in violations)
