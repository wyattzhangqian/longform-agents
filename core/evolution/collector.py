"""Evolution 数据收集 — 成功案例 + 决策日志"""

from __future__ import annotations
from typing import Optional, Dict, Any, List
from datetime import datetime
import json
from pydantic import BaseModel, Field


class SuccessCase(BaseModel):
    """成功案例记录"""
    case_id: str
    agent_id: str
    project_id: str = ""
    domain_id: str = ""
    result_summary: Dict[str, Any] = Field(default_factory=dict)  # 结果摘要
    quality_score: float = 0.0                                     # 质量评分
    iterations: int = 1                                            # 用了多少轮
    duration_seconds: float = 0                                    # 执行耗时
    prompt_used: str = ""                                          # 使用的 prompt
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class DecisionLog(BaseModel):
    """决策日志记录"""
    log_id: str
    agent_id: str
    project_id: str = ""
    decision_type: str = ""                        # tool_choice | strategy | creative_direction
    context: Dict[str, Any] = Field(default_factory=dict)  # 决策上下文
    outcome: str = ""                              # 决策结果描述
    was_successful: bool = True                    # 是否成功
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
