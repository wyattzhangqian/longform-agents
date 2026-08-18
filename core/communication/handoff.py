"""Handoff — Agent 间结构化工作交接"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from core.communication.content_format import (
    contains_agent_process_preamble,
    is_ephemeral_agent_output,
)


class HandoffPayload(BaseModel):
    """结构化 handoff 载荷"""
    from_agent: str
    to_agent: str
    from_name: str = ""
    to_name: str = ""
    phase: str = ""
    summary: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)
    instructions: str = ""
    artifact_keys: List[str] = Field(default_factory=list)
    message_id: Optional[int] = None

    def to_metadata(self) -> dict:
        return {
            "handoff_version": "1",
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "from_name": self.from_name,
            "to_name": self.to_name,
            "phase": self.phase,
            "summary": self.summary,
            "result": self.result,
            "instructions": self.instructions,
            "artifact_keys": self.artifact_keys,
        }

    def to_context_text(self) -> str:
        """格式化为可注入 LLM 的上下文（内容优先，文件引用为辅）"""
        lines = [
            f"## 来自 {self.from_name or self.from_agent} 的工作交接",
            f"**摘要**: {self.summary}",
        ]
        if self.instructions:
            lines.append(f"\n**协作要求**: {self.instructions}")
        if self.artifact_keys:
            lines.append(f"\n**完整文件**: {', '.join(self.artifact_keys)}")
            lines.append(
                "（以上摘要已包含核心内容；如需完整细节请 file_read 读取文件）"
            )
        return "\n".join(lines)


def _extract_content_preview(result_dict: dict) -> str:
    """从 Agent 产出中提取可读内容摘要（过滤 LLM 思考杂讯）"""
    # 优先取 result / raw / content 字段
    raw = (
        result_dict.get("result")
        or result_dict.get("raw")
        or result_dict.get("content")
        or ""
    )
    text = str(raw).strip()
    if not text or len(text) < 20:
        return ""
    if is_ephemeral_agent_output(text):
        return ""
    if contains_agent_process_preamble(text, window=600):
        return ""
    # 截断到 400 字，在词边界处断开
    if len(text) <= 400:
        return text
    cut = text[:400]
    # 尝试在最后一个换行或句号处断开
    for sep in ("\n\n", "\n", "。", "；", "，", " "):
        idx = cut.rfind(sep)
        if idx > 200:
            return cut[:idx] + "\n（...更多内容见完整文件）"
    return cut + "..."


def build_handoff_payload(
    from_agent: str,
    to_agent: str,
    result: Any,
    from_name: str = "",
    to_name: str = "",
    phase: str = "",
    instructions: str = "",
    *,
    artifact_keys: Optional[List[str]] = None,
) -> HandoffPayload:
    """从 Agent 执行结果构建 HandoffPayload（包含实质内容摘要，不只是文件名）。"""
    result_dict = result if isinstance(result, dict) else {"content": str(result)}
    keys = [k for k in (artifact_keys or []) if k and k not in {"result", "raw", "content"}]

    # 内容预览（实质性摘要，让下游 Agent 不读文件也能了解核心产出）
    content_preview = _extract_content_preview(result_dict)

    # 摘要：中文名 + 阶段标签（避免「已完成「agent_tpl_xxx」」）
    name = from_name or from_agent
    phase_label = phase
    if not phase_label or phase_label == from_agent or str(phase_label).startswith("agent_"):
        phase_label = name
    summary = f"{name} 已完成「{phase_label or '本阶段'}」。"
    if keys:
        summary += f"\n**产出文件**: {', '.join(keys)}"
    if content_preview:
        summary += f"\n\n**核心内容**:\n{content_preview}"
    elif keys:
        summary += "\n（请在完整文件中查看详细内容）"

    default_instructions = (
        f"请仔细阅读 {name} 的上述产出，基于其中的设定、结论和发现继续你的专业工作。"
        "如有疑问、矛盾或可改进之处，请在你的回复开头明确指出并讨论，"
        "然后再开始你的创作。"
    )

    return HandoffPayload(
        from_agent=from_agent,
        to_agent=to_agent,
        from_name=from_name,
        to_name=to_name,
        phase=phase,
        summary=summary,
        result=result_dict,
        instructions=instructions or default_instructions,
        artifact_keys=keys[:10],
    )


def deliverable_refs_from_files(
    project_id: str,
    agent_id: str,
    phase_id: str,
    filenames: List[str],
) -> List["WorkspaceRef"]:
    """从已落盘的期望交付文件构建 WorkspaceRef。"""
    from core.run.run_context import WorkspaceRef
    from core.vault.project_artifact_service import ProjectArtifactService

    refs: List[WorkspaceRef] = []
    for fname in filenames:
        name = Path(fname).name
        if not name:
            continue
        try:
            full = ProjectArtifactService.resolve_project_path(project_id, name)
        except ValueError:
            continue
        if not full.is_file():
            continue
        refs.append(
            WorkspaceRef(
                artifact_id="",
                path=ProjectArtifactService.file_path_for_db(full),
                summary=name,
                agent_id=agent_id,
                phase_id=phase_id,
            )
        )
    return refs
