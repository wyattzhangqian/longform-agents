"""连续性线索追踪器 — 管理跨单元的伏笔/回调/弧线"""
from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContinuityTracker:
    """
    追踪所有连续性线索的生命周期。
    数据持久化在 RunContext.continuity_state 字段中。
    """

    def __init__(self, threads: Optional[List[Dict[str, Any]]] = None):
        # threads 是 ContinuityThread.model_dump() 的列表
        self.threads: List[Dict[str, Any]] = threads or []

    @classmethod
    def from_skeleton(cls, skeleton_dict: dict) -> "ContinuityTracker":
        """从骨架的 continuity_threads 初始化"""
        threads = skeleton_dict.get("continuity_threads", [])
        return cls(threads=threads)

    @classmethod
    def from_state(cls, state: Optional[List[Dict[str, Any]]]) -> "ContinuityTracker":
        """从 RunContext 持久化状态恢复"""
        return cls(threads=state or [])

    def to_state(self) -> List[Dict[str, Any]]:
        """导出为可序列化的状态"""
        return self.threads

    def get_obligations(self, unit_number: int) -> Dict[str, List[Dict]]:
        """获取某单元的连续性义务"""
        result = {"introduce": [], "reference": [], "resolve": [], "overdue": []}
        for t in self.threads:
            if t.get("introduce_at") == unit_number and t.get("status") == "planned":
                result["introduce"].append(t)
            if unit_number in t.get("reference_at", []):
                result["reference"].append(t)
            if t.get("resolve_at") == unit_number and t.get("status") != "resolved":
                result["resolve"].append(t)
            if t.get("status") == "overdue":
                result["overdue"].append(t)
        return result

    def update_from_declaration(self, unit_number: int, declaration: Dict[str, List[str]]):
        """
        根据 Agent 产出中的 continuity_declaration 更新状态。
        declaration 格式: {"introduced": ["id1"], "referenced": ["id2"], "resolved": ["id3"]}
        """
        introduced_ids = set(declaration.get("introduced", []))
        referenced_ids = set(declaration.get("referenced", []))
        resolved_ids = set(declaration.get("resolved", []))

        for t in self.threads:
            tid = t.get("id", "")
            if tid in resolved_ids:
                t["status"] = "resolved"
                t["resolved_at_actual"] = unit_number
            elif tid in introduced_ids:
                if t.get("status") == "planned":
                    t["status"] = "introduced"
            elif tid in referenced_ids:
                if t.get("status") in ("planned", "introduced"):
                    t["status"] = "referenced"

    def check_overdue(self, current_unit: int):
        """检查并标记逾期线索"""
        for t in self.threads:
            resolve_at = t.get("resolve_at", 99999)
            status = t.get("status", "planned")
            if current_unit > resolve_at and status not in ("resolved", "overdue"):
                t["status"] = "overdue"
                logger.warning(
                    f"Continuity thread [{t.get('id')}] overdue: "
                    f"should resolve at unit {resolve_at}, now at {current_unit}"
                )

    def format_for_context(self, unit_number: int) -> str:
        """格式化为可注入 Agent context 的提醒文本"""
        obligations = self.get_obligations(unit_number)
        if not any(obligations.values()):
            return ""

        lines = ["## 连续性线索提醒\n"]

        if obligations["introduce"]:
            lines.append("### 本单元需引入")
            for t in obligations["introduce"]:
                lines.append(f"- [{t['id']}] {t.get('content', '')}（{t.get('significance', '')}）")

        if obligations["reference"]:
            lines.append("\n### 本单元需呼应")
            for t in obligations["reference"]:
                lines.append(f"- [{t['id']}] {t.get('content', '')}")

        if obligations["resolve"]:
            lines.append("\n### 本单元需解决")
            for t in obligations["resolve"]:
                lines.append(
                    f"- [{t['id']}] {t.get('content', '')}"
                    f"（引入于第 {t.get('introduce_at', '?')} 单元）"
                )

        if obligations["overdue"]:
            lines.append("\n### ⚠️ 逾期未解决（请尽快处理）")
            for t in obligations["overdue"]:
                lines.append(
                    f"- [{t['id']}] {t.get('content', '')}"
                    f"（应在第 {t.get('resolve_at', '?')} 单元解决）"
                )

        return "\n".join(lines)

    def get_stats(self) -> Dict[str, int]:
        """统计各状态的线索数量"""
        stats = {"planned": 0, "introduced": 0, "referenced": 0, "resolved": 0, "overdue": 0}
        for t in self.threads:
            status = t.get("status", "planned")
            stats[status] = stats.get(status, 0) + 1
        return stats
