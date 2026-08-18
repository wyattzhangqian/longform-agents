"""内置检查器函数集合 — 纯文本/结构化/LLM辅助

检查器函数签名: (rule_config: dict, check_input: CheckInput) -> Optional[dict]
返回 None = 通过, 返回 dict = 违规

通过 @register_checker 装饰器注册到 CHECKER_REGISTRY，
由 execute_check() 统一分发。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

from core.gateway.rules import CheckInput

# 检查器注册表
CHECKER_REGISTRY: Dict[str, Callable] = {}


def register_checker(check_type: str):
    """装饰器：注册检查器到 CHECKER_REGISTRY"""
    def decorator(fn):
        CHECKER_REGISTRY[check_type] = fn
        return fn
    return decorator


@register_checker("not_empty")
def check_not_empty(config: dict, data: CheckInput) -> Optional[dict]:
    """检查产出内容非空"""
    if not data.result or (isinstance(data.result, str) and not data.result.strip()):
        return {"message": "产出内容为空"}
    return None


@register_checker("min_length")
def check_min_length(config: dict, data: CheckInput) -> Optional[dict]:
    """检查文本最小长度"""
    min_chars = config.get("min_chars", 50)
    text = data.text_content or str(data.result or "")
    if len(text) < min_chars:
        return {"message": f"内容仅 {len(text)} 字，低于最低要求 {min_chars} 字"}
    return None


@register_checker("max_length")
def check_max_length(config: dict, data: CheckInput) -> Optional[dict]:
    """检查文本最大长度"""
    max_chars = config.get("max_chars", 50000)
    text = data.text_content or str(data.result or "")
    if len(text) > max_chars:
        return {"message": f"内容 {len(text)} 字，超出上限 {max_chars} 字"}
    return None


@register_checker("keyword_absent")
def check_keyword_absent(config: dict, data: CheckInput) -> Optional[dict]:
    """检查文本中不包含指定关键词"""
    keywords = config.get("keywords", [])
    text = data.text_content or str(data.result or "")
    found = [kw for kw in keywords if kw in text]
    if found:
        return {"message": f"包含不允许的内容: {', '.join(found[:3])}"}
    return None


@register_checker("keyword_present")
def check_keyword_present(config: dict, data: CheckInput) -> Optional[dict]:
    """检查文本中必须包含至少一个指定关键词"""
    keywords = config.get("keywords", [])
    require_all = config.get("require_all", False)
    text = data.text_content or str(data.result or "")

    if require_all:
        missing = [kw for kw in keywords if kw not in text]
        if missing:
            return {"message": f"缺少必要内容: {', '.join(missing[:3])}"}
    else:
        if not any(kw in text for kw in keywords):
            return {"message": f"应包含以下之一: {', '.join(keywords[:5])}"}
    return None


@register_checker("regex_match")
def check_regex_match(config: dict, data: CheckInput) -> Optional[dict]:
    """正则表达式匹配检查"""
    pattern = config.get("pattern", "")
    expect_match = config.get("expect_match", True)
    text = data.text_content or str(data.result or "")

    matched = bool(re.search(pattern, text))
    if expect_match and not matched:
        return {"message": config.get("fail_message", f"未匹配预期格式: {pattern}")}
    if not expect_match and matched:
        return {"message": config.get("fail_message", f"包含不允许的格式: {pattern}")}
    return None


@register_checker("markdown_structure")
def check_markdown_structure(config: dict, data: CheckInput) -> Optional[dict]:
    """Markdown 结构检查"""
    text = data.text_content or str(data.result or "")

    require_h1 = config.get("require_h1", False)
    require_h2 = config.get("require_h2", False)
    min_sections = config.get("min_sections", 0)

    h1_count = len(re.findall(r'^# ', text, re.MULTILINE))
    h2_count = len(re.findall(r'^## ', text, re.MULTILINE))

    if require_h1 and h1_count == 0:
        return {"message": "缺少一级标题 (# )"}
    if require_h2 and h2_count == 0:
        return {"message": "缺少二级标题 (## )"}
    if min_sections > 0 and (h1_count + h2_count) < min_sections:
        return {"message": f"章节数 {h1_count + h2_count} 不足，至少需要 {min_sections} 个"}
    return None


@register_checker("count_elements")
def check_count_elements(config: dict, data: CheckInput) -> Optional[dict]:
    """计数元素检查（用正则匹配计数）"""
    pattern = config.get("pattern", "")
    min_count = config.get("min", 0)
    max_count = config.get("max", 999)
    element_name = config.get("element_name", "元素")
    text = data.text_content or str(data.result or "")

    count = len(re.findall(pattern, text, re.MULTILINE))
    if count < min_count:
        return {"message": f"{element_name}数量 {count}，少于要求的 {min_count}"}
    if count > max_count:
        return {"message": f"{element_name}数量 {count}，超过上限 {max_count}"}
    return None


@register_checker("ratio_check")
def check_ratio(config: dict, data: CheckInput) -> Optional[dict]:
    """数值比例检查"""
    pattern_a = config.get("pattern_a", "")
    pattern_b = config.get("pattern_b", ".")
    min_ratio = config.get("min_ratio", 0)
    max_ratio = config.get("max_ratio", 1.0)
    label = config.get("label", "比例")
    text = data.text_content or str(data.result or "")

    count_a = len(re.findall(pattern_a, text))
    count_b = len(re.findall(pattern_b, text)) or 1
    ratio = count_a / count_b

    if ratio < min_ratio:
        return {"message": f"{label} {ratio:.1%} 低于要求 {min_ratio:.1%}"}
    if ratio > max_ratio:
        return {"message": f"{label} {ratio:.1%} 超过上限 {max_ratio:.1%}"}
    return None


@register_checker("semantic_check")
def check_semantic(config: dict, data: CheckInput) -> Optional[dict]:
    """基础语义检查 — 检测明显的低质量信号

    检查项：
    1. 重复段落检测 — 同一句子出现 >= 3 次
    2. Hallucination 声明检测 — "作为一个AI"、 "我无法" 等
    3. 截断标志检测 — 产出以省略号结尾

    config 参数：
      - min_sentence_len: int = 10 (最短句子长度)
      - max_repeat_count: int = 3 (重复阈值)
      - check_hallucination: bool = True
      - check_truncation: bool = True
    """
    from collections import Counter

    violations = []
    text = data.text_content or str(data.result or "")
    if not text or len(text) < 20:
        return None

    min_sentence_len = config.get("min_sentence_len", 10)
    max_repeat = config.get("max_repeat_count", 3)

    # 1. 检测重复段落（> N 次相同句子）
    # 按句号/换行分句
    sentences = []
    for delim in ("。", "\n\n", "\n"):
        for s in text.split(delim):
            s = s.strip()
            if len(s) >= min_sentence_len:
                sentences.append(s)
        if len(sentences) >= 5:
            break

    if len(sentences) >= 5:
        counts = Counter(sentences)
        repeated = [(sent, cnt) for sent, cnt in counts.items() if cnt >= max_repeat]
        if repeated:
            worst_sent, worst_cnt = max(repeated, key=lambda x: x[1])
            violations.append(
                f"重复内容出现 {worst_cnt} 次: '{worst_sent[:40]}...'"
            )

    # 2. 检测 hallucination/身份暴露声明
    if config.get("check_hallucination", True):
        hallucination_markers = [
            "作为一个AI", "作为AI", "我无法", "I cannot", "As an AI",
            "作为一个语言模型", "作为语言模型", "作为人工智能",
            "我的知识截止", "my knowledge cutoff",
        ]
        for marker in hallucination_markers:
            if marker in text:
                violations.append(
                    f"检测到 AI 身份声明: '{marker}'。"
                    "产出应以专业角色口吻输出，避免暴露 AI 身份。"
                )
                break  # 一个标记就够了，不需要对每个标记都报

    # 3. 截断检测
    if config.get("check_truncation", True):
        truncation_markers = [
            "（未完待续）", "（待续）", "...（以下省略）", "[未完]",
            "（内容过长已截断）",
        ]
        for marker in truncation_markers:
            if marker in text:
                violations.append(f"产出似乎被截断: 包含 '{marker}'")
                break

    if violations:
        return {"message": "; ".join(violations)}

    return None


@register_checker("llm_check")
def check_llm(config: dict, data: CheckInput) -> Optional[dict]:
    """LLM 驱动的自定义检查器 — 用户用自然语言定义检查逻辑

    config 字段:
      - instruction: str  — 检查指令（如"检查产出中是否包含有效的 JSON 代码块"）
      - pass_keyword: str — LLM 回复中含此词视为通过（默认 "PASS"）
      - fail_keyword: str — LLM 回复中含此词视为违规（默认 "FAIL"）

    注意：此检查器是同步版（返回 violation dict），
    但内部使用 asyncio.run_coroutine_threadsafe 或在外层 await。
    实际执行由 db_quality_gateway 的异步上下文驱动。
    """
    instruction = config.get("instruction", "")
    if not instruction:
        return None  # 无指令则跳过

    text = data.text_content or str(data.result or "")
    if not text.strip():
        return None  # 空内容由 not_empty 检查器处理

    # 标记需要异步执行（由 execute_check_async 处理）
    return {
        "_async_llm_check": True,
        "instruction": instruction,
        "text": text[:3000],  # 限制发送长度
        "pass_keyword": config.get("pass_keyword", "PASS"),
        "fail_keyword": config.get("fail_keyword", "FAIL"),
    }


def execute_check(check_type: str, config: dict, data: CheckInput) -> Optional[dict]:
    """执行单条检查

    Args:
        check_type: 检查器类型（对应 CHECKER_REGISTRY 中的 key）
        config: 检查参数
        data: CheckInput 输入

    Returns:
        None = 通过, dict = 违规（含 message 字段）
    """
    checker = CHECKER_REGISTRY.get(check_type)
    if not checker:
        return None  # 未知类型静默通过
    try:
        return checker(config, data)
    except Exception as e:
        return {"message": f"检查器执行异常 [{check_type}]: {e}"}


async def execute_check_async(check_type: str, config: dict, data: CheckInput) -> Optional[dict]:
    """异步版检查执行 — 支持 LLM 检查器"""
    # 先尝试同步执行
    result = execute_check(check_type, config, data)

    # 如果返回了 LLM 检查标记，执行异步 LLM 调用
    if isinstance(result, dict) and result.get("_async_llm_check"):
        return await _run_llm_check(result)

    return result


async def _run_llm_check(check_spec: dict) -> Optional[dict]:
    """执行 LLM 驱动的自定义检查"""
    try:
        from tools.llm_client import LLMClient
        from config import DEFAULT_LLM_MODEL

        client = LLMClient({"model": DEFAULT_LLM_MODEL})
        instruction = check_spec["instruction"]
        text = check_spec["text"]
        pass_kw = check_spec["pass_keyword"]
        fail_kw = check_spec["fail_keyword"]

        prompt = (
            f"你是质量检查员。根据以下规则检查内容，只回复 {pass_kw} 或 {fail_kw} + 一句话原因。\n\n"
            f"## 检查规则\n{instruction}\n\n"
            f"## 待检查内容\n{text}\n\n"
            f"回复格式: {pass_kw} 或 {fail_kw}: <原因>"
        )

        response = await client.chat(prompt, temperature=0.1, max_tokens=200)
        response_upper = response.strip().upper()

        if fail_kw.upper() in response_upper:
            reason = response.strip()
            if ":" in reason:
                reason = reason.split(":", 1)[1].strip()
            return {"message": f"[LLM检查] {reason}", "severity": "warning"}

        return None  # 通过

    except Exception as e:
        import logging
        # P1-4 修复：llm_check 异常不再静默放行（返回 None=通过），改为 warning 违规
        # （不阻塞 overall_passed，但留下记录，避免质量门在 LLM 故障时整体静默失效）
        logging.getLogger("checkers").warning("llm_check 执行失败（计为 warning 违规，不再静默放行）: %s", e)
        return {"message": f"[LLM检查] 检查执行失败: {e}", "severity": "warning"}


@register_checker("continuity_compliance")
def check_continuity_compliance(config: dict, data: CheckInput) -> Optional[dict]:
    """
    检查 Agent 是否完成了本单元的连续性义务。
    data.metadata 中应有 continuity_state 和 current_unit 信息。
    """
    metadata = data.metadata or {}
    continuity_state = metadata.get("continuity_state")
    unit_number = metadata.get("current_unit")
    declaration = metadata.get("continuity_declaration", {})

    if not continuity_state or not unit_number:
        return None  # 不适用，pass

    from core.memory.continuity_tracker import ContinuityTracker
    tracker = ContinuityTracker.from_state(continuity_state)
    obligations = tracker.get_obligations(unit_number)

    violations = []

    for thread in obligations["resolve"]:
        tid = thread.get("id", "")
        if tid not in declaration.get("resolved", []):
            violations.append(f"线索 [{tid}] 应在本单元解决但未在产出中声明")

    for thread in obligations["introduce"]:
        tid = thread.get("id", "")
        if tid not in declaration.get("introduced", []):
            violations.append(f"线索 [{tid}] 应在本单元引入但未在产出中声明")

    if violations:
        return {"message": "连续性义务未完成：" + "；".join(violations)}

    return None  # pass
