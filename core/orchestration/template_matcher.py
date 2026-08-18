"""TemplateMatcher — 三层匹配 + 三层兜底

用于 auto_plan 流程中，将 LLM 拆解出的子任务匹配到最合适的 Agent 模板。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.agent.template import AgentTemplate, get_template_registry

# ── 数据类 ────────────────────────────────────────────────


@dataclass
class MatchResult:
    """单个子任务的匹配结果"""
    subtask_title: str
    required_capabilities: List[str] = field(default_factory=list)
    matched_template_id: Optional[str] = None
    matched_template_name: Optional[str] = None
    score: float = 0.0
    level: str = "none"  # strong | acceptable | weak | none
    reason: str = ""
    fallback_used: bool = False
    fallback_type: str = ""  # "" | "llm_generated" | "domain_general" | "skipped"
    alternative_ids: List[str] = field(default_factory=list)
    suggestion: Optional[dict] = None  # 创建建议 {type, domain, role, reason}

    def to_dict(self) -> dict:
        return {
            "subtask_title": self.subtask_title,
            "required_capabilities": self.required_capabilities,
            "matched_template_id": self.matched_template_id,
            "matched_template_name": self.matched_template_name,
            "score": self.score,
            "level": self.level,
            "reason": self.reason,
            "fallback_used": self.fallback_used,
            "fallback_type": self.fallback_type,
            "alternative_ids": self.alternative_ids,
        }


# ── 匹配引擎 ──────────────────────────────────────────────


class TemplateMatcher:
    """三层匹配 + 三层兜底

    Layer 1: capability 标签精确匹配 (权重 40%)
    Layer 2: embedding 语义向量相似度 (权重 35%)
    Layer 3: LLM 直接看模板列表选择 (权重 25%)

    兜底 1: LLM 即时生成临时 Agent
    兜底 2: 领域通用 Agent
    兜底 3: 跳过 + 告警
    """

    # 匹配权重
    CAPABILITY_WEIGHT = 0.40
    SEMANTIC_WEIGHT = 0.35
    DOMAIN_WEIGHT = 0.25

    # 匹配阈值
    STRONG_THRESHOLD = 80.0
    ACCEPTABLE_THRESHOLD = 60.0
    WEAK_THRESHOLD = 40.0
    MATCH_MIN_THRESHOLD = 40.0  # 低于此值触发兜底

    # P2-13 数据化：领域通用模板不再硬编码映射，改从 agent_templates 表按
    # domain + capability_tags 含 general_* 标记查询（见 _find_domain_general_template）。
    # 新增领域只需提供一个带 general_* 标记的通用助手模板，无需改代码。

    # 跨领域衰减系数：非本领域模板分数乘以该系数
    # 避免"确定歌词主题"误匹配到小说创作助手这类跨领域错配
    CROSS_DOMAIN_PENALTY = 0.4

    # 本领域通用模板：匹配时额外加分（在衰减之后仍可能因本领域优势胜出）
    # 注意：跨领域的 general_assistant 仍会被衰减，它们只在 _fallback 兜底阶段使用
    @classmethod
    def _is_same_domain_template(cls, tmpl_domain: str, target_domain: str) -> bool:
        """判断模板是否属于目标领域"""
        return tmpl_domain == target_domain

    # 同义词映射 (capability 标签的中英文/变体)
    _SYNONYM_MAP: Dict[str, List[str]] = {
        "world_building": ["世界观构建", "世界观设定", "worldbuilding", "setting_design"],
        "character_design": ["人物设计", "角色设计", "人设", "character_creation", "角色创建"],
        "dialogue_writing": ["对话写作", "对白", "dialogue_design", "对话设计"],
        "plot_structure": ["情节结构", "情节设计", "narrative_structure", "故事结构"],
        "research": ["研究", "调研", "investigation", "analysis", "调查"],
        "writing": ["写作", "撰稿", "content_creation", "文案", "编写"],
        "editing": ["编辑", "审校", "校对", "proofreading", "润色"],
        "music": ["音乐", "作曲", "编曲", "songwriting", "旋律"],
        "storyboard": ["分镜", "分镜师", "storyboarding", "分格"],
        "coloring": ["上色", "着色", "color", "配色"],
        "postproduction": ["后期", "后期制作", "post", "post_production"],
        "mixing": ["混音", "混音师", "mix", "音频处理"],
        "review": ["审阅", "审核", "审查", "reviewing", "质量检查"],
        "lyric_writing": ["作词", "歌词", "填词", "lyrics"],
        "proofreading": ["校对", "审校", "copy_editing", "校稿"],
    }

    def __init__(self, llm_client=None):
        self._registry = get_template_registry()
        self._llm_client = llm_client
        self._embedding_model = None
        self._embedding_cache: Dict[str, List[float]] = {}

    # ═══════════════════════════════════════════════════════
    # 公共接口
    # ═══════════════════════════════════════════════════════

    async def match(
        self,
        subtasks: list,
        domain: str,
        llm_client=None,
    ) -> List[MatchResult]:
        """对一组子任务执行三层匹配

        Args:
            subtasks: SubTask 对象列表，每个有 title/description/required_capabilities
            domain: 目标领域 (web_novel/comic_static/...)
            llm_client: LLMClient 实例 (用于 Layer 3 和兜底 1)

        Returns:
            MatchResult 列表，与 subtasks 一一对应
        """
        client = llm_client or self._llm_client
        all_templates = await self._registry.list()
        results: List[MatchResult] = []

        for subtask in subtasks:
            result = await self._match_one(subtask, domain, all_templates, client)
            results.append(result)

        return results

    # ═══════════════════════════════════════════════════════
    # 单子任务匹配
    # ═══════════════════════════════════════════════════════

    async def _match_one(
        self,
        subtask,
        domain: str,
        all_templates: List[AgentTemplate],
        llm_client=None,
    ) -> MatchResult:
        caps = list(getattr(subtask, "required_capabilities", []) or [])
        title = getattr(subtask, "title", "") or ""
        description = getattr(subtask, "description", "") or ""

        result = MatchResult(
            subtask_title=title,
            required_capabilities=caps,
        )

        # ── Layer 1: capability 标签精确匹配 ──────────
        tag_result = self._capability_match(caps, domain, all_templates)
        if tag_result:
            best_tmpl, best_score, alternatives = tag_result
            result.matched_template_id = best_tmpl.id
            result.matched_template_name = best_tmpl.name
            result.score = best_score
            result.alternative_ids = alternatives[:3]
            result.reason = f"标签精确命中: {self._hit_tags(caps, best_tmpl)}"

            if best_score >= self.STRONG_THRESHOLD:
                result.level = "strong"
                return result

        # ── Layer 2: embedding 语义匹配 ───────────────
        semantic_results = await self._semantic_match(
            title, description, caps, domain, all_templates
        )
        if semantic_results:
            best_tmpl, sem_score = semantic_results[0]
            # 合并 Layer 1 和 Layer 2 的结果
            if result.score > 0:
                combined = max(
                    result.score * (1 - self.SEMANTIC_WEIGHT) + sem_score * self.SEMANTIC_WEIGHT,
                    sem_score,
                )
            else:
                combined = sem_score

            if combined > result.score:
                result.matched_template_id = best_tmpl.id
                result.matched_template_name = best_tmpl.name
                result.score = combined
                result.reason = f"语义相似度匹配: {sem_score:.0f}分"

            result.alternative_ids = list(set(
                result.alternative_ids + [
                    m[0].id for m in semantic_results[1:4]
                    if m[0].id != result.matched_template_id
                ]
            ))[:3]

            if result.score >= self.STRONG_THRESHOLD:
                result.level = "strong"
                return result

        # 判断当前匹配等级
        if result.score >= self.STRONG_THRESHOLD:
            result.level = "strong"
        elif result.score >= self.ACCEPTABLE_THRESHOLD:
            result.level = "acceptable"
            if not result.reason:
                result.reason = "综合匹配得分可接受"
        elif result.score >= self.WEAK_THRESHOLD:
            result.level = "weak"
            if not result.reason:
                result.reason = "匹配度偏低，建议手动确认"
        else:
            result.level = "none"

        # ── Layer 3: LLM 直接选择 ─────────────────────
        # 在标签匹配 + 语义匹配后仍未达 acceptable 时，让 LLM 介入选择
        if result.score < self.ACCEPTABLE_THRESHOLD and llm_client:
            llm_result = await self._llm_select(
                title, description, caps, domain, all_templates, llm_client
            )
            if llm_result:
                result.matched_template_id = llm_result.id
                result.matched_template_name = llm_result.name
                result.score = max(result.score, 50.0)
                result.level = "acceptable"
                result.reason = "LLM 从模板库中直接选择"

        # ── 如果仍不达标，走兜底 ──────────────────────
        if result.score < self.MATCH_MIN_THRESHOLD or result.matched_template_id is None:
            result = await self._fallback(subtask, domain, result, llm_client)

        return result

    # ═══════════════════════════════════════════════════════
    # Layer 1: capability 标签匹配
    # ═══════════════════════════════════════════════════════

    def _capability_match(
        self,
        caps: List[str],
        domain: str,
        all_templates: List[AgentTemplate],
    ) -> Optional[Tuple[AgentTemplate, float, List[str]]]:
        """按 capability 标签精确/同义/关联匹配"""
        scored: List[Tuple[AgentTemplate, float]] = []

        for tmpl in all_templates:
            score = 0.0
            for cap in caps:
                if cap in tmpl.capability_tags:
                    score += 30  # 精确命中
                else:
                    for tcap in tmpl.capability_tags:
                        if self._is_synonym(cap, tcap):
                            score += 20
                            break
                        elif self._is_related(cap, tcap):
                            score += 10
                            break

            # 领域加分
            if tmpl.domain == domain:
                score += 25

            max_possible = len(caps) * 30 + 25
            normalized = min((score / max_possible) * 100, 100) if max_possible > 0 else 0

            # 跨领域惩罚：非本领域模板大幅衰减（通用模板也衰减，它们只在 _fallback 兜底阶段使用）
            if tmpl.domain and tmpl.domain != domain:
                normalized *= self.CROSS_DOMAIN_PENALTY

            if normalized > 20:
                scored.append((tmpl, normalized))

        scored.sort(key=lambda x: x[1], reverse=True)
        if not scored:
            return None

        # 专业优先：如果前两名分数差 < 10 分，专业 agent 额外加分
        if len(scored) >= 2:
            top_score = scored[0][1]
            for i, (tmpl, score) in enumerate(scored[:5]):
                if top_score - score < 10 and self._is_specialized_template(tmpl):
                    scored[i] = (tmpl, score + 12)
            # 重新排序
            scored.sort(key=lambda x: x[1], reverse=True)

        return (
            scored[0][0],
            scored[0][1],
            [t.id for t, _ in scored[1:6]],
        )

    def _is_specialized_template(self, tmpl: AgentTemplate) -> bool:
        """判断是否为专业 agent（非通用 general_assistant 系列）"""
        tmpl_id = (tmpl.id or "").lower()
        return "general_assistant" not in tmpl_id

    def _is_synonym(self, cap: str, tcap: str) -> bool:
        """检查两个 capability 标签是否为同义词"""
        cap_l = cap.lower().replace("_", " ")
        tcap_l = tcap.lower().replace("_", " ")
        for variants in self._SYNONYM_MAP.values():
            variants_low = [v.lower().replace("_", " ") for v in variants]
            if cap_l in variants_low and tcap_l in variants_low:
                return True
        # 直接相等也视为同义
        return cap_l == tcap_l

    def _is_related(self, cap: str, tcap: str) -> bool:
        """检查两个 capability 标签是否语义关联（共享词根）"""
        cap_parts = set(cap.lower().replace("_", " ").split())
        tcap_parts = set(tcap.lower().replace("_", " ").split())
        common = cap_parts & tcap_parts
        return len(common) > 0

    def _hit_tags(self, caps: List[str], tmpl: AgentTemplate) -> str:
        """返回在模板中命中的标签列表"""
        hits = [c for c in caps if c in tmpl.capability_tags]
        if not hits:
            hits = [c for c in caps if any(self._is_synonym(c, tc) for tc in tmpl.capability_tags)]
        return ", ".join(hits[:3]) if hits else "语义匹配"

    # ═══════════════════════════════════════════════════════
    # Layer 2: embedding 语义匹配
    # ═══════════════════════════════════════════════════════

    async def _semantic_match(
        self,
        title: str,
        description: str,
        caps: List[str],
        domain: str,
        all_templates: List[AgentTemplate],
        top_k: int = 10,
    ) -> List[Tuple[AgentTemplate, float]]:
        """使用 embedding 做语义相似度匹配"""
        try:
            model = await self._get_embedding_model()
        except Exception:
            return []

        # 构建查询文本
        parts = [title]
        if description:
            parts.append(description)
        if caps:
            parts.append(f"需要能力: {', '.join(caps)}")
        query_text = ". ".join(parts)

        try:
            query_vec = await self._get_embedding(model, query_text)
        except Exception:
            return []

        scored: List[Tuple[AgentTemplate, float]] = []
        for tmpl in all_templates:
            tmpl_text = f"{tmpl.name}. {tmpl.role}. 能力: {', '.join(tmpl.capability_tags[:8])}"
            try:
                tmpl_vec = await self._get_embedding(model, tmpl_text)
            except Exception:
                continue

            similarity = self._cosine_similarity(query_vec, tmpl_vec)
            score = self._similarity_to_score(similarity)

            if tmpl.domain == domain:
                score = min(score + 10, 100)
            else:
                # 跨领域惩罚：非本领域模板大幅衰减（通用模板也衰减，它们只在 _fallback 兜底阶段使用）
                score *= self.CROSS_DOMAIN_PENALTY

            scored.append((tmpl, score))

        scored.sort(key=lambda x: x[1], reverse=True)

        # 专业优先：前两名分数差 < 10 时，专业 agent 额外加分
        if len(scored) >= 2:
            top_score = scored[0][1]
            for i, (tmpl, score) in enumerate(scored[:5]):
                if top_score - score < 10 and self._is_specialized_template(tmpl):
                    scored[i] = (tmpl, score + 12)
            scored.sort(key=lambda x: x[1], reverse=True)

        return scored[:top_k]

    async def _get_embedding_model(self):
        """懒加载 embedding 模型"""
        if self._embedding_model is not None:
            return self._embedding_model
        try:
            from sentence_transformers import SentenceTransformer
            self._embedding_model = SentenceTransformer(
                "paraphrase-multilingual-MiniLM-L12-v2"
            )
            return self._embedding_model
        except ImportError:
            raise RuntimeError(
                "sentence-transformers 未安装。安装: pip install sentence-transformers"
            )

    async def _get_embedding(self, model, text: str) -> List[float]:
        """获取文本的 embedding 向量，带缓存"""
        cache_key = text[:200]  # 用前 200 字符做 key
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(None, lambda: model.encode(text).tolist())
        self._embedding_cache[cache_key] = vec
        return vec

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """余弦相似度"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x ** 2 for x in a) ** 0.5
        norm_b = sum(x ** 2 for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _similarity_to_score(sim: float) -> float:
        """将 0-1 的余弦相似度映射到 0-100 的匹配分"""
        if sim >= 0.85:
            return 100.0
        elif sim >= 0.70:
            return 60 + (sim - 0.70) / 0.15 * 40
        elif sim >= 0.50:
            return 20 + (sim - 0.50) / 0.20 * 40
        else:
            return sim / 0.50 * 20

    # ═══════════════════════════════════════════════════════
    # Layer 3: LLM 直接选择
    # ═══════════════════════════════════════════════════════

    async def _llm_select(
        self,
        title: str,
        description: str,
        caps: List[str],
        domain: str,
        all_templates: List[AgentTemplate],
        llm_client,
    ) -> Optional[AgentTemplate]:
        """让 LLM 看到模板列表，直接选择最合适的"""
        # 过滤到目标领域的模板
        domain_templates = [t for t in all_templates if t.domain == domain]
        if not domain_templates:
            domain_templates = all_templates

        if len(domain_templates) == 0:
            return None

        template_list = "\n".join(
            f"- {t.id}: {t.emoji} {t.name}\n  角色: {t.role}\n  能力: {', '.join(t.capability_tags[:6])}"
            for t in domain_templates[:20]  # 最多发 20 个给 LLM
        )

        prompt = f"""你需要为一个子任务选择最合适的 Agent 模板。

**子任务**: {title}
**描述**: {description or '无'}
**需要的技能**: {', '.join(caps) if caps else '未指定'}

**可用模板** (仅列出领域 "{domain}" 的模板):
{template_list}

请选择最合适的模板 ID，回复格式: 只回复模板 ID (如 "tpl_novel_world_builder")，如果没有合适的回复 "NONE"。"""

        try:
            response = await llm_client.chat(prompt)
            selected_id = response.strip().strip('"').strip("'")
        except Exception:
            return None

        if not selected_id or selected_id.upper() == "NONE":
            return None

        tmpl = await self._registry.get(selected_id)
        return tmpl

    # ═══════════════════════════════════════════════════════
    # 三层兜底
    # ═══════════════════════════════════════════════════════

    async def _find_domain_general_template(self, domain: str) -> Optional[AgentTemplate]:
        """P2-13 数据化：按 domain + capability_tags 含 general_* 标记查找领域通用助手模板。

        替代原硬编码 DOMAIN_GENERAL_MAP —— 新增领域只需提供一个带 general_* 标记的
        通用助手模板（fixtures/data/agent_templates.json），无需改代码。
        """
        try:
            templates = await self._registry.list()
            for t in templates:
                tags = t.capability_tags or []
                if t.domain == domain and any(
                    str(tag).startswith("general_") for tag in tags
                ):
                    return t
        except Exception:
            pass
        return None

    async def _fallback(
        self,
        subtask,
        domain: str,
        partial_result: MatchResult,
        llm_client=None,
    ) -> MatchResult:
        """执行三层兜底策略"""

        title = getattr(subtask, "title", "") or ""
        description = getattr(subtask, "description", "") or ""
        caps = list(getattr(subtask, "required_capabilities", []) or [])

        # 兜底 1: LLM 即时生成 Agent
        if llm_client:
            try:
                temp_agent = await self._generate_temp_agent(
                    title, description, caps, domain, llm_client
                )
                if temp_agent:
                    temp_id = await self._registry.save_temporary_agent({
                        **temp_agent,
                        "project_id": "",
                        "subtask_title": title,
                        "match_score": partial_result.score,
                    })
                    partial_result.matched_template_id = temp_id
                    partial_result.matched_template_name = temp_agent.get("name", title)
                    partial_result.score = 40.0
                    partial_result.level = "weak"
                    partial_result.reason = f"AI 临时生成了 Agent「{temp_agent.get('name', title)}」"
                    partial_result.fallback_used = True
                    partial_result.fallback_type = "llm_generated"
                    return partial_result
            except Exception:
                pass

        # 兜底 2: 领域通用 Agent（P2-13 数据化：从 agent_templates 查 general_* 标记模板）
        general_tmpl = await self._find_domain_general_template(domain)
        if general_tmpl:
                partial_result.matched_template_id = general_tmpl.id
                partial_result.matched_template_name = general_tmpl.name
                partial_result.score = 30.0
                partial_result.level = "weak"
                partial_result.reason = f"使用领域通用 Agent「{general_tmpl.name}」兜底"
                partial_result.fallback_used = True
                partial_result.fallback_type = "domain_general"
                # 附带建议：提示用户可以创建更专业的 agent
                partial_result.suggestion = {
                    "type": "create_specialized_agent",
                    "domain": domain,
                    "role": f"{title}专家",
                    "reason": f"当前{domain}领域无'{title}'专业角色，建议创建以获得更好体验",
                }
                return partial_result

        # 兜底 3: 跳过 + 告警
        partial_result.matched_template_id = None
        partial_result.matched_template_name = None
        partial_result.level = "none"
        partial_result.reason = "未能匹配到合适 Agent，请手动指派"
        partial_result.fallback_used = True
        partial_result.fallback_type = "skipped"
        partial_result.suggestion = {
            "type": "create_domain_agent",
            "domain": domain,
            "role": f"{title}专家",
            "reason": f"平台暂无此领域Agent，请先创建专业Agent后再运行",
        }
        return partial_result

    async def _generate_temp_agent(
        self,
        title: str,
        description: str,
        caps: List[str],
        domain: str,
        llm_client,
    ) -> Optional[dict]:
        """LLM 即时生成临时 Agent 的 system_prompt + 元信息"""
        prompt = f"""你是一个 Agent 模板生成器。为以下子任务生成专业的 Agent 定义。

**子任务**: {title}
**描述**: {description or '无'}
**所属领域**: {domain}
**需要的能力**: {', '.join(caps) if caps else '未指定'}

返回 JSON 格式，包含:
- name: Agent 中文名称 (10字以内)
- emoji: 一个相关 emoji
- role: 角色描述 (一句话，30字以内)
- system_prompt: 完整的 system_prompt，包含角色定位、工作流程(3-5步)、输出格式、质量自检清单(3-5条)。200字以上。
- capability_tags: 能力标签列表 (5-8个)
- recommended_model: "deepseek-v4-flash"  # 平台 LLM 唯一支持的模型（deepseek-v4-flash / deepseek-v4-pro）
- temperature: 0.7

只返回 JSON，不要有其他内容。"""

        try:
            response = await llm_client.chat(prompt)
            # 提取 JSON
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(response[json_start:json_end])
                return {
                    "name": data.get("name", title),
                    "emoji": data.get("emoji", "🤖"),
                    "role": data.get("role", ""),
                    "system_prompt": data.get("system_prompt", ""),
                    "capability_tags": data.get("capability_tags", caps),
                    "recommended_model": data.get("recommended_model", "deepseek-v4-flash"),
                    "temperature": data.get("temperature", 0.7),
                    # 2026-08-12：4096 会被 reasoning 思维链吃光 → 长文截断/空产出。
                    # temp agent 是兜底（处理任意任务），至少 16384。
                    "max_tokens": 16384,
                }
        except Exception:
            pass

        return None


# ── 单例 ──────────────────────────────────────────────────

_matcher: Optional[TemplateMatcher] = None


def get_template_matcher(llm_client=None) -> TemplateMatcher:
    """获取 TemplateMatcher 单例"""
    global _matcher
    if _matcher is None or (llm_client is not None and _matcher._llm_client is None):
        _matcher = TemplateMatcher(llm_client=llm_client)
    return _matcher
