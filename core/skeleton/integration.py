"""骨架规划与现有 Runner/Planner 的集成逻辑"""
from __future__ import annotations
import asyncio
import logging
from typing import List, Optional

from core.skeleton.models import Skeleton, UnitSpec
from core.skeleton.planner import SkeletonPlanner
from core.run.diagnostics import Severity
from core.vault.project_artifact_service import ProjectArtifactService

logger = logging.getLogger(__name__)

# 骨架规划总超时：避免 LLM 流式响应/推理链过长时长时间占用事件循环，
# 超时后降级为 fallback 骨架，保证工作流能继续执行（不卡死整个后端）。
_SKELETON_PLAN_TIMEOUT_SECONDS = 120


async def plan_skeleton_if_needed(
    project_id: str,
    task: str,
    content_type: str = "novel",
    total_units: int = 0,
    llm_client=None,
    diagnostics=None,
) -> Optional[Skeleton]:
    """
    判断是否需要骨架规划，如需要则执行并持久化。

    规则：total_units > 1 时触发骨架规划。
    单篇内容（total_units=0 or 1）不需要骨架。

    超时/失败保护：骨架规划超过 _SKELETON_PLAN_TIMEOUT_SECONDS 秒未完成时，
    降级为均匀 fallback 骨架，避免阻塞主事件循环导致整个后端假死。

    diagnostics: 可选 RunDiagnostics 累加器（2026-08-09 P1）——超时/失败/持久化失败
    都记录降级，不再私下静默。
    """
    if total_units <= 1:
        return None

    planner = SkeletonPlanner(llm_client=llm_client)
    degraded = False
    try:
        # 执行期用 plan_outline（Pass1+2）而非完整 plan（Pass1+2+3）：
        # Phase 扩展只需 arcs + units.goal，Pass3 详细 brief 非必需；
        # 且减少 LLM 调用次数（N 单元从 1+arc+N/5 次降到 1+arc 次），降低卡死风险。
        skeleton = await asyncio.wait_for(
            planner.plan_outline(
                intent=task,
                content_type=content_type,
                total_units=total_units,
            ),
            timeout=_SKELETON_PLAN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        degraded = True
        logger.warning(
            "骨架规划超时(>%ds)，降级为 fallback 骨架: project=%s content_type=%s units=%d",
            _SKELETON_PLAN_TIMEOUT_SECONDS, project_id, content_type, total_units,
        )
        skeleton = planner._fallback_skeleton(task, content_type, total_units)
    except Exception as e:
        degraded = True
        logger.warning("骨架规划失败，降级为 fallback 骨架: %s", e)
        skeleton = planner._fallback_skeleton(task, content_type, total_units)

    if degraded and diagnostics is not None:
        diagnostics.record(
            Severity.CRITICAL, "skeleton_planner",
            f"骨架规划超时/失败，降级为 fallback 骨架 (units={total_units})",
            "继续执行，但章节人设/伏笔一致性由 fallback 骨架保证，可能弱于完整规划",
        )

    skeleton.project_id = project_id

    # 持久化为 Artifact
    try:
        await save_skeleton_artifact(project_id, skeleton)
    except Exception as e:
        logger.warning("骨架 artifact 持久化失败: %s", e)
        if diagnostics is not None:
            diagnostics.record(
                Severity.WARNING, "skeleton_planner",
                f"骨架 artifact 持久化失败: {e}",
                "骨架不落盘，resume/跨进程恢复会丢失",
            )

    return skeleton


async def save_skeleton_artifact(project_id: str, skeleton: Skeleton):
    """将骨架保存为项目 Artifact"""
    content = skeleton.model_dump_json(indent=2)
    await ProjectArtifactService.write_text(
        project_id=project_id,
        relative_path="skeleton.json",
        content=content,
        agent_id="system:skeleton_planner",
        artifact_type="skeleton",
        metadata={"version": skeleton.version, "total_units": skeleton.total_units},
    )


async def load_skeleton_artifact(
    project_id: str, diagnostics=None,
) -> Optional[Skeleton]:
    """从 Artifact 加载骨架"""
    try:
        from core.vault.versioning import ArtifactVersioning
        content = await ArtifactVersioning.get_current_content(project_id, "skeleton.json")
        if content:
            return Skeleton.model_validate_json(content)
    except Exception as e:
        # 2026-08-09 P1：骨架加载失败静默 → CRITICAL 降级（resume 丢失骨架）
        _logger.warning("骨架 artifact 加载失败: %s", e)
        if diagnostics is not None:
            diagnostics.record(
                Severity.CRITICAL, "skeleton_planner",
                f"骨架 artifact 加载失败: {e}",
                "骨架不持久化，resume/恢复会丢失",
            )
    return None


def expand_phases_with_skeleton(specs, skeleton: Skeleton):
    """骨架驱动的 Phase 静态扩展 — 把"写作"phase 替换为 N 个章节 phase。

    策略：
    - 识别"写作"phase（label 含 写作/写第/撰写/创作/正文 等关键词），用 N 个章节 phase 替换
    - 保留其他 phase（骨架设计/审校/连续性追踪等职责型 phase）
    - 每个章节 phase 通过 metadata.dependencies + metadata.preconditions 串联
    - 章节 phase 的 agent_id 复用原"写作"phase 的 agent_id（保持人设一致）
    - 章节 phase 的 label 从骨架 UnitSpec.goal 取

    这是"不改 GraphRuntime 主循环"前提下的长内容执行方案：
    扩展发生在 compile 之前，GraphRuntime 拿到的就是一个普通的 N+ 节点的 WorkflowGraph。

    Args:
        specs: 原 PhaseSpec 列表（规划阶段 LLM 分解出的"骨架设计/写作/审校"等）
        skeleton: 已生成的骨架（含 N 个 UnitSpec）

    Returns:
        新的 PhaseSpec 列表（长度 = len(specs) - 1 + skeleton.total_units）
    """
    from core.run.phase_spec import PhaseSpec

    if not skeleton or not skeleton.units:
        return specs

    WRITER_KEYWORDS = ("写作", "写第", "撰写", "创作", "起草", "正文", "write", "draft", "compos", "writ")

    # 识别第一个"写作"phase
    # 注意：CollaborationGraph.from_request 会把 label 回填成 agent 中文名
    # （builtin_writer → "撰稿人"），"撰稿人" 不含上述关键词 → 光靠 label 匹配会
    # 漏判。因此同时匹配 agent_id（builtin_writer 含 "writ"），保证骨架扩展不静默失效。
    writer_idx = -1
    for i, spec in enumerate(specs):
        label_lower = (spec.label or "").lower()
        agent_lower = (spec.agent_id or "").lower()
        if any(kw in label_lower for kw in WRITER_KEYWORDS) or any(
            kw in agent_lower for kw in WRITER_KEYWORDS
        ):
            writer_idx = i
            break

    # 没识别到写作 phase → 不扩展（保守回退）
    if writer_idx < 0:
        logger.info("expand_phases_with_skeleton: 未识别到写作 phase，保持原 %d phases", len(specs))
        return specs

    writer_spec = specs[writer_idx]
    writer_agent_id = writer_spec.agent_id or "builtin_general"
    writer_phase_id = writer_spec.id
    writer_deps = list(writer_spec.metadata.get("dependencies", []))

    sorted_units = sorted(skeleton.units, key=lambda u: u.unit_number)
    unit_name = skeleton.unit_definition.name if skeleton.unit_definition else "章"

    # 构建章节 phases
    unit_specs: List[PhaseSpec] = []
    prev_unit_id = ""

    for unit in sorted_units:
        unit_id = f"unit_{unit.unit_number}"

        # 第一章继承写作 phase 的依赖（通常是"骨架设计"phase）
        # 后续章依赖上一章（严格顺序）
        if unit.unit_number == 1:
            deps = writer_deps.copy()
        else:
            deps = [prev_unit_id]

        unit_spec = PhaseSpec(
            id=unit_id,
            agent_id=writer_agent_id,
            label=f"第{unit.unit_number}{unit_name}：{(unit.goal or '')[:40]}",
            collaboration=writer_spec.collaboration,
            dialogue_policy=writer_spec.dialogue_policy,
            expected_outputs=[f"unit_{unit.unit_number:03d}.md"],
            metadata={
                "unit_number": unit.unit_number,
                "critical": unit.critical,
                "dependencies": deps,
                "preconditions": (
                    [] if unit.unit_number == 1
                    else [{"type": "unit_completed", "unit_number": unit.unit_number - 1}]
                ),
            },
        )
        # 继承写作 phase 的质量/知识配置
        unit_spec.quality_profile = writer_spec.quality_profile.model_copy(deep=True)
        unit_spec.knowledge_profile = writer_spec.knowledge_profile.model_copy(deep=True)

        unit_specs.append(unit_spec)
        prev_unit_id = unit_id

    last_unit_id = prev_unit_id

    # 组装结果：写作 phase 之前的 + N 个章节 phase + 之后的（依赖重写）
    result: List[PhaseSpec] = []
    result.extend(specs[:writer_idx])
    result.extend(unit_specs)

    for spec in specs[writer_idx + 1:]:
        deps = list(spec.metadata.get("dependencies", []))
        new_deps = []
        for d in deps:
            if d == writer_phase_id:
                new_deps.append(last_unit_id)
            else:
                new_deps.append(d)
        # 原依赖含写作 phase 且最后一章不在新 deps 里 → 加上
        if writer_phase_id in deps and last_unit_id not in new_deps:
            new_deps.append(last_unit_id)
        spec.metadata["dependencies"] = new_deps
        result.append(spec)

    logger.info(
        "expand_phases_with_skeleton: %d phases → %d phases（写作 phase '%s' 扩展为 %d 章）",
        len(specs), len(result), writer_spec.label, len(unit_specs),
    )
    return result


def format_unit_spec_for_context(unit_spec: UnitSpec, skeleton: Skeleton) -> str:
    """将 UnitSpec 格式化为可注入 Agent context 的文本"""
    unit_def = skeleton.unit_definition
    lines = [
        f"## 当前创作单元：第 {unit_spec.unit_number} {unit_def.name}",
        f"",
        f"**目标：** {unit_spec.goal}",
        f"**节奏定位：** {unit_spec.pacing}",
    ]

    if unit_spec.atmosphere_target:
        lines.append(f"**氛围目标：** {unit_spec.atmosphere_target}")

    if unit_spec.type_specific:
        for k, v in unit_spec.type_specific.items():
            lines.append(f"**{k}：** {v}")

    # 连续性提醒
    threads = skeleton.get_threads_for_unit(unit_spec.unit_number)
    if threads["introduce"]:
        lines.append(f"\n### 本单元需引入的线索")
        for t in threads["introduce"]:
            lines.append(f"- [{t.id}] {t.content}（{t.significance}）")
    if threads["reference"]:
        lines.append(f"\n### 本单元需呼应的线索")
        for t in threads["reference"]:
            lines.append(f"- [{t.id}] {t.content}")
    if threads["resolve"]:
        lines.append(f"\n### 本单元需解决的线索")
        for t in threads["resolve"]:
            lines.append(f"- [{t.id}] {t.content}（引入于第{t.introduce_at}{unit_def.name}）")
    if threads["overdue"]:
        lines.append(f"\n### ⚠️ 逾期未解决")
        for t in threads["overdue"]:
            lines.append(f"- [{t.id}] {t.content}（应在第{t.resolve_at}{unit_def.name}解决）")

    return "\n".join(lines)
