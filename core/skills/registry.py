"""Skill 注册表 — DB CRUD + 平台内置种子"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional

from core.skills.models import SkillDefinition, SkillManifest, MCPServerDefinition
from core.storage.database import get_db

PLATFORM_SKILLS: List[dict] = [
    {
        "id": "skill_platform_file_read",
        "name": "文件读取",
        "description": "读取 outputs 目录下的文本文件",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["file_read"],
            "prompt_snippet": "## 技能：文件读取\n可使用 file_read 读取项目产出文件。",
        },
    },
    {
        "id": "skill_platform_file_write",
        "name": "文件写入",
        "description": "将内容写入 outputs 目录",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["file_write"],
            "prompt_snippet": "## 技能：文件写入\n可使用 file_write 保存文本产物。",
        },
    },
    {
        "id": "skill_platform_web_search",
        "name": "网页检索",
        "description": "搜索互联网（DuckDuckGo 免费 / 或配置 Tavily、SerpAPI Key）",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["web_search"],
            "prompt_snippet": "## 技能：网页检索\n可使用 web_search 查询最新信息。",
        },
    },

    # === P3 新增 ===
    {
        "id": "skill_platform_generate_image",
        "name": "AI 生图",
        "description": "使用 AI 模型生成图片",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["generate_image"],
            "prompt_snippet": (
                "## 技能：AI 生图\n"
                "可使用 generate_image 工具生成图片。\n"
                "参数: prompt(图片描述,尽量具体), style(风格,可选), width/height(尺寸,默认1024)。\n"
                "提示：描述应包含构图、光线、色调、风格等要素。"
            ),
        },
    },
    {
        "id": "skill_platform_generate_video",
        "name": "AI 生视频",
        "description": "使用 AI 模型生成视频",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["generate_video"],
            "prompt_snippet": (
                "## 技能：AI 生视频\n"
                "可使用 generate_video 工具生成视频。\n"
                "参数: prompt(视频描述), image_url(参考图URL,图生视频时提供)。"
            ),
        },
    },
    {
        "id": "skill_platform_generate_music",
        "name": "AI 音乐生成",
        "description": "使用 AI 生成音乐/歌曲",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["generate_music"],
            "prompt_snippet": (
                "## 技能：AI 音乐生成\n"
                "可使用 generate_music 工具生成音乐。\n"
                "参数: prompt(音乐描述), lyrics(歌词,可选), "
                "instrumental(纯器乐true/false), style(风格标签), duration(秒数,默认60)。"
            ),
        },
    },
    {
        "id": "skill_platform_shell_exec",
        "name": "命令行执行",
        "description": "在沙箱中执行 shell 命令（需人工审批）",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["shell_exec"],
            "prompt_snippet": (
                "## 技能：命令行执行（高风险）\n"
                "可使用 shell_exec 执行系统命令。需人工审批，超时120秒。\n"
                "仅用于必要的文件处理/数据转换操作。"
            ),
            "default_permission_mode": "ask",
            "default_risk_level": "high",
        },
    },
    {
        "id": "skill_platform_asset_query",
        "name": "资产库查询",
        "description": "查询和读取平台资产库内容",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["asset_query"],
            "prompt_snippet": (
                "## 技能：资产库查询\n"
                "可使用 asset_query 查询项目资产。\n"
                "action 参数: list_refs(列出引用), get_content(读取全文,需asset_id), search(搜索)。"
            ),
        },
    },
    {
        "id": "skill_platform_llm_call",
        "name": "LLM 辅助推理",
        "description": "执行中调用 LLM 进行辅助分析/生成/翻译",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["llm_call"],
            "prompt_snippet": (
                "## 技能：LLM 辅助推理\n"
                "可使用 llm_call 进行额外 AI 分析。适用于：\n"
                "- 生成角色描述/背景故事\n"
                "- 评估情节一致性\n"
                "- 翻译/改写段落\n"
                "- 提取结构化信息\n"
                "参数: prompt(提示词,必填), system_prompt(可选), temperature(可选)。"
            ),
        },
    },
    {
        "id": "skill_platform_knowledge_query",
        "name": "知识库检索",
        "description": "按需检索领域知识库",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["knowledge_query"],
            "prompt_snippet": (
                "## 技能：知识库检索\n"
                "可使用 knowledge_query 按需检索知识。适用于：\n"
                "- 查找角色设定、世界观规则\n"
                "- 回溯前情摘要\n"
                "- 获取风格指南和参考\n"
                "参数: query(检索关键词), tags(标签,逗号分隔,可选), top_k(返回数量,默认5)。"
            ),
        },
    },
    {
        "id": "skill_platform_read_url",
        "name": "网页内容读取",
        "description": "读取指定 URL 的网页正文",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tools": ["read_url"],
            "prompt_snippet": (
                "## 技能：网页内容读取\n"
                "可使用 read_url 读取网页正文。\n"
                "与 web_search 的区别：\n"
                "- web_search: 搜索关键词 → 多条摘要\n"
                "- read_url: 打开特定网址 → 完整正文\n"
                "参数: url(网址,必填), max_chars(最大字符数,默认8000)。"
            ),
        },
    },
    # ═══ 领域1：音乐制作专业工具集（llm_prompt 物化工具） ═══
    {
        "id": "skill_music_lyrics",
        "name": "作词专业工具集",
        "description": "歌词韵脚方案与可唱性校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_rhyme_scheme",
                    "type": "llm_prompt",
                    "description": "检查歌词的韵脚方案和韵律流畅度",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "lyrics": {"type": "string", "description": "歌词文本"},
                            "target_scheme": {"type": "string", "description": "目标韵脚方案（如 AABB / ABAB）"},
                        },
                        "required": ["lyrics"],
                    },
                    "prompt_template": "你是韵律专家。分析以下歌词的韵脚：\n\n{lyrics}\n\n目标方案：{target_scheme}\n\n检查：\n1. 每句尾字的韵母，标注实际韵脚方案\n2. 是否押韵不齐（跳韵/出韵）\n3. 是否存在为押韵牺牲内容的句子（韵>意）\n4. 中文歌的'倒字'问题（字调与旋律走向冲突）\n输出 JSON: {{\"actual_scheme\": \"...\", \"issues\": [...], \"suggestions\": [...]}}",
                    "system_prompt": "你是专业作词人和韵律学专家。用准确的音韵学术语。",
                    "temperature": 0.3,
                },
                {
                    "name": "check_singability",
                    "type": "llm_prompt",
                    "description": "检查歌词的可唱性（咬字/换气/节奏贴合）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "lyrics": {"type": "string"},
                            "section": {"type": "string", "description": "段落类型 verse/chorus/bridge"},
                        },
                        "required": ["lyrics"],
                    },
                    "prompt_template": "评估以下歌词的可唱性：\n\n{lyrics}\n段落：{section}\n\n检查：\n1. 是否有难咬字组合（连续闭口音/绕口）\n2. 换气点是否自然（长句无换气机会）\n3. 字数节奏是否规整（同段各句字数差异）\n4. Chorus 是否够'抓耳'（记忆点强度）\n输出 JSON: {{\"singability_score\": 0-10, \"hard_spots\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 作词专业工具\n- check_rhyme_scheme: 检查韵脚方案和韵律\n- check_singability: 检查可唱性（咬字/换气/倒字）\n写完歌词后主动用这些工具校验，尤其注意中文歌的倒字问题。",
        },
    },
    {
        "id": "skill_music_composition",
        "name": "作曲专业工具集",
        "description": "参考曲目分析/和弦进行推荐/旋律评估",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "analyze_reference_track",
                    "type": "llm_prompt",
                    "description": "分析参考曲目的作曲特征（调性/BPM/曲式/和声/旋律走向）",
                    "parameters": {
                        "type": "object",
                        "properties": {"track_info": {"type": "string", "description": "参考曲目名称或描述"}},
                        "required": ["track_info"],
                    },
                    "prompt_template": "你是音乐分析师。分析曲目「{track_info}」的作曲特征：\n1. 调性和 BPM\n2. 曲式结构（各段小节数）\n3. 和声进行（用罗马数字标记）\n4. 旋律特征（音域/音程/节奏型）\n5. 情绪曲线\n6. 最突出的作曲技巧\n用准确的乐理术语。",
                    "system_prompt": "你是资深音乐理论分析师。",
                    "temperature": 0.3,
                },
                {
                    "name": "suggest_chord_progression",
                    "type": "llm_prompt",
                    "description": "基于风格和情绪推荐和弦进行",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "style": {"type": "string"},
                            "emotion": {"type": "string"},
                            "key": {"type": "string"},
                            "section": {"type": "string", "description": "verse/chorus/bridge"},
                        },
                        "required": ["style", "emotion"],
                    },
                    "prompt_template": "推荐 3 个和弦进行：\n风格：{style}\n情绪：{emotion}\n调性：{key}\n段落：{section}\n每个给出：罗马数字标记、具体和弦、听感描述、经典案例曲目。",
                    "temperature": 0.5,
                },
                {
                    "name": "evaluate_melody",
                    "type": "llm_prompt",
                    "description": "评估旋律与和声的搭配及可唱性",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "melody_description": {"type": "string", "description": "旋律描述（音高走向/节奏型）"},
                            "harmony": {"type": "string", "description": "和声进行"},
                        },
                        "required": ["melody_description"],
                    },
                    "prompt_template": "评估旋律：\n旋律：{melody_description}\n和声：{harmony}\n\n检查：\n1. 旋律与和声是否有碰撞（非和弦音处理）\n2. 音程跳进是否可唱（>8度需谨慎）\n3. Chorus 是否比 Verse 音域更高更强\n4. 是否有清晰的记忆点（hook）\n输出 JSON: {{\"score\": 0-10, \"issues\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 作曲专业工具\n- analyze_reference_track: 分析参考曲目\n- suggest_chord_progression: 推荐和弦进行\n- evaluate_melody: 评估旋律和声搭配\n定调性/和声前先分析参考，写完旋律后用 evaluate_melody 校验。",
        },
    },
    {
        "id": "skill_music_arrangement",
        "name": "编曲专业工具集",
        "description": "编曲分析/频率冲突检查/段落过渡推荐",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "analyze_arrangement",
                    "type": "llm_prompt",
                    "description": "分析参考曲目的编曲手法（配器/密度/过渡）",
                    "parameters": {
                        "type": "object",
                        "properties": {"track_info": {"type": "string"}},
                        "required": ["track_info"],
                    },
                    "prompt_template": "分析「{track_info}」的编曲：\n1. 使用了哪些乐器（逐轨列出）\n2. 各段落配器增减策略\n3. 段落过渡手法\n4. 音色特征（温暖/冷冽/空灵/厚重）\n5. 最突出的编曲技巧",
                    "temperature": 0.3,
                },
                {
                    "name": "check_frequency_conflict",
                    "type": "llm_prompt",
                    "description": "检查配器方案的频率冲突",
                    "parameters": {
                        "type": "object",
                        "properties": {"instruments": {"type": "string", "description": "配器列表及频率范围"}},
                        "required": ["instruments"],
                    },
                    "prompt_template": "你是混音工程师。检查配器频率冲突：\n\n{instruments}\n\n对每对冲突乐器指出：冲突频段、严重程度、解决建议（换音色/调音域/让位）。\n输出 JSON: {{\"conflicts\": [...], \"score\": 0-10}}",
                    "temperature": 0.2,
                },
                {
                    "name": "suggest_transition",
                    "type": "llm_prompt",
                    "description": "推荐段落过渡手法",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "from_section": {"type": "string"},
                            "to_section": {"type": "string"},
                            "style": {"type": "string"},
                        },
                        "required": ["from_section", "to_section"],
                    },
                    "prompt_template": "推荐 3 种过渡手法：\n从 {from_section} 到 {to_section}\n风格：{style}\n每种说明：做法、持续拍数、听感效果。",
                    "temperature": 0.5,
                },
            ],
            "prompt_snippet": "## 编曲专业工具\n- analyze_arrangement: 分析参考编曲\n- check_frequency_conflict: 检查配器频率冲突\n- suggest_transition: 推荐段落过渡\n确定配器后必须用 check_frequency_conflict 验证，避免频率打架。",
        },
    },
    {
        "id": "skill_music_mixing",
        "name": "混音专业工具集",
        "description": "频率分配规划/声场布局规划",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "plan_frequency_allocation",
                    "type": "llm_prompt",
                    "description": "为各轨规划频率分配（避免频率打架）",
                    "parameters": {
                        "type": "object",
                        "properties": {"tracks": {"type": "string", "description": "所有音轨列表"}},
                        "required": ["tracks"],
                    },
                    "prompt_template": "为以下音轨规划 EQ 频率分配：\n\n{tracks}\n\n对每轨给出：主频段、高通频率、需要削减的频段、让位关系。\n确保每个频段主要由一个乐器占据。\n输出 JSON: {{\"allocations\": [{{track, main_freq, high_pass, cuts}}]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "plan_spatial",
                    "type": "llm_prompt",
                    "description": "规划声场布局（声像/深度/宽度）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tracks": {"type": "string"},
                            "style": {"type": "string"},
                        },
                        "required": ["tracks"],
                    },
                    "prompt_template": "规划声场：\n音轨：{tracks}\n风格：{style}\n\n给出：\n1. 声像布局（哪些居中/哪些偏左右）\n2. 深度分层（干声在前/混响在后）\n3. 立体声宽度策略\n原则：人声/kick/bass/snare 居中，其他分布两侧。\n输出 JSON: {{\"center\": [...], \"sides\": {{...}}, \"depth\": {{...}}}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 混音专业工具\n- plan_frequency_allocation: 规划各轨频率分配\n- plan_spatial: 规划声场布局\n混音前先做频率分配和声场规划，确保清晰度。",
        },
    },
    {
        "id": "skill_music_mastering",
        "name": "母带专业工具集",
        "description": "目标平台响度标准查询",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "get_platform_loudness_target",
                    "type": "llm_prompt",
                    "description": "获取目标平台的响度标准",
                    "parameters": {
                        "type": "object",
                        "properties": {"platform": {"type": "string", "description": "目标平台名"}},
                        "required": ["platform"],
                    },
                    "prompt_template": "给出「{platform}」的母带响度标准：\n目标 LUFS、真峰值上限（dBTP）、动态范围建议、常见误区。\n参考：Spotify/Apple Music -14 LUFS，YouTube -14 LUFS，CD -9 LUFS，抖音/短视频约 -14 到 -16 LUFS。",
                    "temperature": 0.2,
                },
            ],
            "prompt_snippet": "## 母带专业工具\n- get_platform_loudness_target: 查询目标平台响度标准\n处理前先确认目标平台的 LUFS 和 dBTP 要求。",
        },
    },
    {
        "id": "skill_music_vocal",
        "name": "声乐指导工具集",
        "description": "歌曲音域分析与调性建议",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "analyze_vocal_range",
                    "type": "llm_prompt",
                    "description": "分析歌曲音域并给出调性建议",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "melody_description": {"type": "string"},
                            "singer_range": {"type": "string", "description": "演唱者舒适音域（可选）"},
                        },
                        "required": ["melody_description"],
                    },
                    "prompt_template": "分析音域：\n旋律：{melody_description}\n演唱者音域：{singer_range}\n\n给出：\n1. 歌曲最高音和最低音\n2. 总音域跨度\n3. 是否超出演唱者舒适区\n4. 是否建议移调（升/降几个半音）\n输出 JSON: {{\"highest\": \"...\", \"lowest\": \"...\", \"range\": \"...\", \"key_change_suggestion\": \"...\"}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 声乐工具\n- analyze_vocal_range: 分析音域+调性建议\n先确认音域是否适合演唱者，必要时建议移调。",
        },
    },
    # ═══ 领域2：漫画（静态）专业工具集 ═══
    {
        "id": "skill_comic_script",
        "name": "漫画编剧工具集",
        "description": "翻页悬念与对话密度校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_page_turn",
                    "type": "llm_prompt",
                    "description": "检查翻页悬念设计（每页右下角是否有钩子）",
                    "parameters": {
                        "type": "object",
                        "properties": {"page_script": {"type": "string", "description": "分页脚本"}},
                        "required": ["page_script"],
                    },
                    "prompt_template": "你是漫画叙事专家。检查翻页悬念：\n\n{page_script}\n\n漫画的'翻页'是关键悬念点——每页最后一格（右下）应制造'想翻下一页'的冲动。检查：\n1. 每页右下格是否有钩子（悬念/冲突/期待）\n2. 是否存在把高潮放在左上（翻页前就泄了底）\n3. 跨页大格（spread）的使用时机是否恰当\n输出 JSON: {{\"pages\": [{{page, has_hook, issue}}], \"suggestions\": [...]}}",
                    "system_prompt": "你是资深漫画编剧，深谙翻页叙事。",
                    "temperature": 0.3,
                },
                {
                    "name": "check_dialogue_density",
                    "type": "llm_prompt",
                    "description": "检查每格对话密度（避免文字挤爆画面）",
                    "parameters": {
                        "type": "object",
                        "properties": {"page_script": {"type": "string"}},
                        "required": ["page_script"],
                    },
                    "prompt_template": "检查对话密度：\n\n{page_script}\n\n规则：单格对话 ≤ 25 字，单页对话 ≤ 80 字。超出会挤占画面、影响阅读。\n列出超标的格子，给出精简建议。\n输出 JSON: {{\"overloaded_panels\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 漫画编剧工具\n- check_page_turn: 检查翻页悬念\n- check_dialogue_density: 检查对话密度\n分页后用 check_page_turn 确保每页有翻页钩子，用 check_dialogue_density 避免文字挤爆。",
        },
    },
    {
        "id": "skill_comic_character",
        "name": "漫画角色设计工具集",
        "description": "剪影辨识度与配色方案校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "silhouette_check",
                    "type": "llm_prompt",
                    "description": "评估角色剪影辨识度",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "silhouette": {"type": "string", "description": "角色轮廓描述（发型/体型/姿态/配件）"},
                            "other_characters": {"type": "string", "description": "同作品其他角色轮廓"},
                        },
                        "required": ["silhouette"],
                    },
                    "prompt_template": "你是角色设计审核。评估剪影辨识度：\n\n本角色轮廓：{silhouette}\n其他角色：{other_characters}\n\n剪影测试标准：缩小到 1cm 高、纯黑影，仍能辨认是谁。检查：\n1. 是否有最强辨识特征（发型通常最强）\n2. 与其他角色轮廓差异是否足够\n3. 黑白/缩小后是否仍可区分\n输出 JSON: {{\"distinctiveness\": 0-10, \"strongest_feature\": \"...\", \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "check_color_scheme",
                    "type": "llm_prompt",
                    "description": "检查配色方案（和谐度/性格匹配/区分度）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "colors": {"type": "string", "description": "配色（主色/辅色/点缀色）"},
                            "character_trait": {"type": "string", "description": "性格关键词"},
                            "other_characters_colors": {"type": "string"},
                        },
                        "required": ["colors"],
                    },
                    "prompt_template": "评估角色配色：\n配色：{colors}\n性格：{character_trait}\n其他角色配色：{other_characters_colors}\n\n检查：\n1. 色彩和谐（是否超过3主色+2点缀）\n2. 颜色心理与性格匹配（热血=红橙/冷静=蓝/神秘=紫黑/温柔=粉黄）\n3. 与其他角色是否撞色\n4. 黑白印刷下灰度是否可区分\n输出 JSON: {{\"harmony\": 0-10, \"issues\": [...], \"alternatives\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 角色设计工具\n- silhouette_check: 评估剪影辨识度（1cm 测试）\n- check_color_scheme: 检查配色和谐度和区分度\n定稿前必须过剪影测试和配色检查。",
        },
    },
    {
        "id": "skill_comic_storyboard",
        "name": "漫画分镜工具集",
        "description": "视线流与镜头多样性校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_eye_flow",
                    "type": "llm_prompt",
                    "description": "检查页面视线流是否顺畅",
                    "parameters": {
                        "type": "object",
                        "properties": {"page_layout": {"type": "string", "description": "页面分格布局描述"}},
                        "required": ["page_layout"],
                    },
                    "prompt_template": "你是分镜专家。检查视线流：\n\n{page_layout}\n\n漫画阅读顺序（中文右到左 / 日漫右上到左下 / 韦氏左到右）。检查：\n1. 格子排列是否引导读者正确的阅读顺序\n2. 是否有视线跳跃/混乱（不知道先看哪格）\n3. 视觉焦点格是否突出\n输出 JSON: {{\"flow_score\": 0-10, \"confusion_points\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "check_camera_variety",
                    "type": "llm_prompt",
                    "description": "检查镜头角度多样性（避免连续同角度）",
                    "parameters": {
                        "type": "object",
                        "properties": {"panels": {"type": "string", "description": "各格镜头描述"}},
                        "required": ["panels"],
                    },
                    "prompt_template": "检查镜头多样性：\n\n{panels}\n\n规则：不应连续 3 格用相同景别/角度（呆板）。对话场景要有正反打变化。检查：\n1. 是否有连续雷同镜头\n2. 景别是否有变化（特写/中景/远景）\n3. 对话场景是否遵守 180 度规则\n输出 JSON: {{\"variety_score\": 0-10, \"monotonous_runs\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 分镜工具\n- check_eye_flow: 检查视线流\n- check_camera_variety: 检查镜头多样性\n分镜后用这两个工具验证阅读顺畅度和镜头节奏。",
        },
    },
    {
        "id": "skill_comic_lineart",
        "name": "线稿工具集",
        "description": "线条层次校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_line_hierarchy",
                    "type": "llm_prompt",
                    "description": "检查线条层次（轮廓/结构/细节三级）",
                    "parameters": {
                        "type": "object",
                        "properties": {"description": {"type": "string", "description": "线稿描述"}},
                        "required": ["description"],
                    },
                    "prompt_template": "检查线条层次：\n{description}\n\n专业线稿三级线宽：轮廓线（最粗）> 结构线（中）> 细节线（最细）。前景重、背景轻营造纵深。检查：\n1. 三级线宽是否分明\n2. 前后景线宽是否体现纵深\n3. 排线方向是否统一\n输出 JSON: {{\"score\": 0-10, \"issues\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 线稿工具\n- check_line_hierarchy: 检查线条层次\n绘制后检查三级线宽是否分明。",
        },
    },
    {
        "id": "skill_comic_coloring",
        "name": "上色工具集",
        "description": "光源一致性校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_lighting_consistency",
                    "type": "llm_prompt",
                    "description": "检查光源一致性和氛围色",
                    "parameters": {
                        "type": "object",
                        "properties": {"coloring_plan": {"type": "string"}},
                        "required": ["coloring_plan"],
                    },
                    "prompt_template": "检查上色方案：\n{coloring_plan}\n\n检查：\n1. 同一场景内光源方向是否一致\n2. 高饱和/高明度区域是否 ≤ 画面 20%（否则视觉焦点混乱）\n3. 氛围色（环境反射光）是否让元素融合\n4. 情绪色是否匹配剧情\n输出 JSON: {{\"score\": 0-10, \"issues\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 上色工具\n- check_lighting_consistency: 检查光源一致性\n上色后验证光源方向和高饱和占比。",
        },
    },
    {
        "id": "skill_comic_layout",
        "name": "排版工具集",
        "description": "对话框布局校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_bubble_placement",
                    "type": "llm_prompt",
                    "description": "检查对话框布局（不遮挡关键画面）",
                    "parameters": {
                        "type": "object",
                        "properties": {"layout_plan": {"type": "string"}},
                        "required": ["layout_plan"],
                    },
                    "prompt_template": "检查对话框布局：\n{layout_plan}\n\n检查：\n1. 气泡是否遮挡人脸/关键动作\n2. 阅读顺序是否自然（气泡排列顺应阅读方向）\n3. 字号在移动端是否可读\n4. 关键信息是否落在出血区（会被裁掉）\n输出 JSON: {{\"score\": 0-10, \"issues\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 排版工具\n- check_bubble_placement: 检查对话框布局\n排版后验证气泡不遮脸、阅读顺序自然。",
        },
    },
    # ═══ 领域3：漫剧（动态）专业工具集 ═══
    {
        "id": "skill_drama_script",
        "name": "漫剧剧本工具集",
        "description": "钩子设计与时长估算",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_episode_hook",
                    "type": "llm_prompt",
                    "description": "检查每集的开场钩子和结尾悬念",
                    "parameters": {
                        "type": "object",
                        "properties": {"episode_script": {"type": "string"}},
                        "required": ["episode_script"],
                    },
                    "prompt_template": "你是短剧编剧专家。检查钩子设计：\n\n{episode_script}\n\n短剧生死在钩子。检查：\n1. 开场 3-5 秒是否有冲突/悬念（否则观众划走）\n2. 结尾是否留悬念（驱动看下一集）\n3. 中段是否有维持注意力的转折\n无效开场：风景空镜/旁白介绍/缓慢铺垫。\n输出 JSON: {{\"opening_hook\": 0-10, \"ending_hook\": 0-10, \"issues\": [...], \"suggestions\": [...]}}",
                    "system_prompt": "你深谙短剧/漫剧的注意力经济。",
                    "temperature": 0.4,
                },
                {
                    "name": "estimate_duration",
                    "type": "llm_prompt",
                    "description": "估算每场戏时长（对话+动作+镜头）",
                    "parameters": {
                        "type": "object",
                        "properties": {"scene_script": {"type": "string"}},
                        "required": ["scene_script"],
                    },
                    "prompt_template": "估算时长：\n\n{scene_script}\n\n估算依据：对白按正常语速（每字约0.3秒）+ 停顿 + 动作/转场时间。给出每场戏时长和本集总时长。漫剧单集通常 3-8 分钟。\n输出 JSON: {{\"scenes\": [{{scene, duration_sec}}], \"total_min\": X, \"over_target\": bool}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 剧本工具\n- check_episode_hook: 检查开场/结尾钩子\n- estimate_duration: 估算时长\n每集写完必须过钩子检查，并估算时长确保在 3-8 分钟。",
        },
    },
    {
        "id": "skill_drama_visual",
        "name": "视觉导演工具集",
        "description": "色彩脚本与镜头规则定义",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "generate_color_script",
                    "type": "llm_prompt",
                    "description": "为全剧生成色彩脚本（情绪→色彩映射）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "story_summary": {"type": "string"},
                            "emotional_arc": {"type": "string", "description": "全剧情绪弧线"},
                        },
                        "required": ["story_summary"],
                    },
                    "prompt_template": "为漫剧生成色彩脚本：\n故事：{story_summary}\n情绪弧线：{emotional_arc}\n\n输出一个情绪→色彩映射表，覆盖全剧主要情绪节点。每个节点给出：主色调、饱和度、对比度、使用场景。\n输出 JSON: {{\"color_script\": [{{emotion, main_color, saturation, contrast, usage}}]}}",
                    "temperature": 0.5,
                },
                {
                    "name": "define_camera_rules",
                    "type": "llm_prompt",
                    "description": "定义全剧镜头语言规则（情绪→运镜）",
                    "parameters": {
                        "type": "object",
                        "properties": {"style_keywords": {"type": "string"}},
                        "required": ["style_keywords"],
                    },
                    "prompt_template": "定义镜头语言规则：\n风格关键词：{style_keywords}\n\n给出情绪→运镜的规则表（如：紧张=手持晃动+快推；平静=固定长镜；回忆=慢速缓推+柔焦）。\n输出 JSON: {{\"camera_rules\": [{{emotion, movement, duration, shot_type}}]}}",
                    "temperature": 0.4,
                },
            ],
            "prompt_snippet": "## 视觉导演工具\n- generate_color_script: 生成色彩脚本\n- define_camera_rules: 定义镜头规则\n用这些工具产出 Style Bible，作为下游所有视觉环节的统一规范。",
        },
    },
    {
        "id": "skill_drama_storyboard",
        "name": "漫剧分镜工具集",
        "description": "镜头节奏与转场推荐",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_shot_rhythm",
                    "type": "llm_prompt",
                    "description": "检查镜头时长节奏（避免平均单调）",
                    "parameters": {
                        "type": "object",
                        "properties": {"shot_list": {"type": "string", "description": "镜头列表含时长"}},
                        "required": ["shot_list"],
                    },
                    "prompt_template": "检查镜头节奏：\n\n{shot_list}\n\n好的节奏有张弛（长短镜头交替）。检查：\n1. 是否所有镜头时长雷同（平均=呆板）\n2. 情绪高点是否有特殊镜头处理（长镜/特写/慢镜）\n3. 对话场景是否有正反打变化\n4. 快慢节奏是否匹配情绪（紧张=快切，抒情=长镜）\n输出 JSON: {{\"rhythm_score\": 0-10, \"issues\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "suggest_transition",
                    "type": "llm_prompt",
                    "description": "推荐镜头间转场方式",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "from_shot": {"type": "string"},
                            "to_shot": {"type": "string"},
                            "emotion": {"type": "string"},
                        },
                        "required": ["from_shot", "to_shot"],
                    },
                    "prompt_template": "推荐转场：\n从 {from_shot} 到 {to_shot}\n情绪：{emotion}\n\n转场类型：硬切（cut，最常用/紧凑）、淡入淡出（fade，时空跳跃）、叠化（dissolve，柔和过渡/回忆）、划变（wipe，明显分隔）、匹配剪辑（match cut，视觉连贯）、声音先入（J-cut，悬念）。推荐 2 种并说明。",
                    "temperature": 0.4,
                },
            ],
            "prompt_snippet": "## 分镜工具\n- check_shot_rhythm: 检查镜头节奏\n- suggest_transition: 推荐转场\n分镜后验证节奏张弛，转场匹配情绪。",
        },
    },
    {
        "id": "skill_drama_animation",
        "name": "动画节奏工具集",
        "description": "缓动曲线推荐",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "suggest_easing",
                    "type": "llm_prompt",
                    "description": "为运动推荐缓动曲线",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "motion": {"type": "string", "description": "运动描述"},
                            "emotion": {"type": "string"},
                        },
                        "required": ["motion"],
                    },
                    "prompt_template": "推荐缓动曲线：\n运动：{motion}\n情绪：{emotion}\n\n缓动类型：ease-in（缓起，蓄力/紧张）、ease-out（缓停，释放/舒缓）、ease-in-out（自然，通用）、linear（匀速，机械/无机感）、custom bezier（特殊）。给出推荐类型+贝塞尔参数+理由。\n输出 JSON: {{\"easing\": \"...\", \"bezier\": \"...\", \"rationale\": \"...\"}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 动画节奏工具\n- suggest_easing: 推荐缓动曲线\n为每个运动选缓动曲线时，让曲线匹配情绪（紧张ease-in/释放ease-out）。",
        },
    },
    {
        "id": "skill_drama_dubbing",
        "name": "配音脚本工具集",
        "description": "台词情绪标注",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "markup_emotion",
                    "type": "llm_prompt",
                    "description": "为台词标注情绪/语速/重音/停顿",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dialogue": {"type": "string"},
                            "character_state": {"type": "string", "description": "角色当前情绪状态"},
                        },
                        "required": ["dialogue"],
                    },
                    "prompt_template": "为配音标注表演指导：\n台词：{dialogue}\n角色状态：{character_state}\n\n对每句标注：情绪类型、强度(1-5)、语速(慢/中/快)、重音词、停顿位置、特殊处理(气声/颤音/哽咽等)。\n输出 JSON: {{\"lines\": [{{text, emotion, intensity, speed, stress_words, pauses, special}}]}}",
                    "temperature": 0.4,
                },
            ],
            "prompt_snippet": "## 配音工具\n- markup_emotion: 标注台词情绪/语速/重音\n为每句台词生成表演标注，指导 TTS 或声优表演。",
        },
    },
    {
        "id": "skill_drama_postproduction",
        "name": "后期制作工具集",
        "description": "音效与配乐进出点规划",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "plan_sound_design",
                    "type": "llm_prompt",
                    "description": "规划音效设计（环境音/动作音/转场音）",
                    "parameters": {
                        "type": "object",
                        "properties": {"scene_description": {"type": "string"}},
                        "required": ["scene_description"],
                    },
                    "prompt_template": "规划音效：\n场景：{scene_description}\n\n分类给出：1)环境音（底噪/氛围）；2)动作音（脚步/开门/打斗）；3)转场音（whoosh/impact）；4)情绪音效（心跳/耳鸣/回响）。每个标注出现时间点。\n输出 JSON: {{\"ambient\": [...], \"action\": [...], \"transition\": [...], \"emotional\": [...]}}",
                    "temperature": 0.4,
                },
                {
                    "name": "plan_music_cue",
                    "type": "llm_prompt",
                    "description": "规划配乐进出点",
                    "parameters": {
                        "type": "object",
                        "properties": {"episode_emotional_arc": {"type": "string"}},
                        "required": ["episode_emotional_arc"],
                    },
                    "prompt_template": "规划配乐进出点：\n本集情绪弧线：{episode_emotional_arc}\n\n给出：每段配乐的进入时间、退出时间、情绪类型、音量层级、留白点（无配乐处，让观众专注对话/制造张力）。\n注意：配乐不能盖过对白，情绪转折处配乐变化。\n输出 JSON: {{\"cues\": [{{in, out, mood, level, note}}], \"silence_points\": [...]}}",
                    "temperature": 0.4,
                },
            ],
            "prompt_snippet": "## 后期工具\n- plan_sound_design: 规划音效\n- plan_music_cue: 规划配乐进出点\n后期合成时用这些工具确保声画协调，配乐不盖对白。",
        },
    },
    # ═══ 领域4：网文小说专业工具集（纯文本分析/校验） ═══
    {
        "id": "skill_novel_worldbuilding",
        "name": "世界观架构工具集",
        "description": "世界观设定自洽校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_setting_consistency",
                    "type": "llm_prompt",
                    "description": "检查世界观设定内部自洽",
                    "parameters": {"type": "object", "properties": {"settings": {"type": "string"}}, "required": ["settings"]},
                    "prompt_template": "检查世界观自洽：\n\n{settings}\n\n检查核心法则之间/设定之间是否有矛盾（如'魔法需消耗生命'但某角色无限放魔法）。找出冲突点。\n输出 JSON: {{\"conflicts\": [...], \"score\": 0-10}}",
                    "temperature": 0.2,
                },
            ],
            "prompt_snippet": "## 世界观工具\n- check_setting_consistency: 检查设定自洽\n定稿前验证核心法则无自相矛盾。",
        },
    },
    {
        "id": "skill_novel_character",
        "name": "人物设定工具集",
        "description": "角色立体度校验（want/need/flaw/arc）",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_character_depth",
                    "type": "llm_prompt",
                    "description": "检查角色立体度（want/need/flaw/arc）",
                    "parameters": {"type": "object", "properties": {"character": {"type": "string"}}, "required": ["character"]},
                    "prompt_template": "检查角色立体度：\n\n{character}\n\n立体角色四要素：Want（表层欲望）、Need（深层需求，常与want冲突）、Flaw（致命缺陷）、Arc（成长弧光）。检查：\n1. 四要素是否齐全\n2. want 和 need 是否有张力\n3. 是否'完美无缺'（=扁平）\n输出 JSON: {{\"depth_score\": 0-10, \"missing\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 人物工具\n- check_character_depth: 检查角色立体度\n设计角色后验证 want/need/flaw/arc 齐全。",
        },
    },
    {
        "id": "skill_novel_plot",
        "name": "情节架构工具集",
        "description": "情节因果链完整性校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_causality",
                    "type": "llm_prompt",
                    "description": "检查情节因果链完整性",
                    "parameters": {"type": "object", "properties": {"plot": {"type": "string"}}, "required": ["plot"]},
                    "prompt_template": "检查因果链：\n\n{plot}\n\n检查：\n1. 每个情节转折是否有前因（不是天上掉下来）\n2. 高潮是否由主角的选择/行动驱动（而非巧合/外力）\n3. 是否有'工具人'情节（角色行为只为推进剧情，不符合其动机）\n输出 JSON: {{\"causality_score\": 0-10, \"gaps\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 情节工具\n- check_causality: 检查因果链\n情节设计后验证因果完整、高潮由主角驱动。",
        },
    },
    {
        "id": "skill_novel_writing",
        "name": "小说写作工具集",
        "description": "节奏/告诉式/感官描写校验（章节写手无核心）",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_pacing",
                    "type": "llm_prompt",
                    "description": "评估段落叙事节奏是否匹配目标",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "target_pacing": {"type": "string", "description": "目标节奏（如'紧张升温'）"},
                        },
                        "required": ["text", "target_pacing"],
                    },
                    "prompt_template": "你是叙事节奏专家。评估节奏：\n目标：{target_pacing}\n\n文本：\n{text}\n\n评估：\n1. 实际节奏感受（快/慢/紧/松）\n2. 是否匹配目标\n3. 哪些句子拖慢/加速了节奏（长句放缓，短句加急；对话加速，描写放缓）\n4. 修改建议\n输出 JSON: {{\"match_score\": 0-10, \"actual\": \"...\", \"issues\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "detect_telling",
                    "type": "llm_prompt",
                    "description": "检测'告诉'而非'展示'的段落（show don't tell）",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                    "prompt_template": "检测'告诉式'写作：\n\n{text}\n\n'告诉'（直接陈述情绪/评价，如'她很伤心'）vs '展示'（用动作/感官/细节让读者感受，如'她把脸埋进枕头，肩膀微微发抖'）。\n找出所有'告诉式'句子，给出'展示式'改写建议。\n输出 JSON: {{\"telling_sentences\": [{{original, rewrite}}], \"telling_ratio\": 0-1}}",
                    "temperature": 0.4,
                },
                {
                    "name": "check_sensory_detail",
                    "type": "llm_prompt",
                    "description": "检查感官描写覆盖度",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                    "prompt_template": "检查感官描写：\n\n{text}\n\n好的场景描写调动多种感官（视/听/嗅/味/触）。检查：\n1. 用了哪些感官\n2. 是否过度依赖视觉（新手通病）\n3. 缺失的感官能否补充\n输出 JSON: {{\"senses_used\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 写作工具\n- check_pacing: 检查节奏匹配\n- detect_telling: 检测'告诉式'写作\n- check_sensory_detail: 检查感官描写\n写完章节后用 detect_telling 排查，用 check_pacing 校验节奏。",
        },
    },
    {
        "id": "skill_novel_dialogue",
        "name": "对话设计工具集",
        "description": "对话辨识度与信息倾倒校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_voice_distinction",
                    "type": "llm_prompt",
                    "description": "检查角色对话是否有辨识度（去掉标签能否分清谁说的）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dialogue": {"type": "string"},
                            "characters": {"type": "string", "description": "各角色语言指纹"},
                        },
                        "required": ["dialogue"],
                    },
                    "prompt_template": "检查对话辨识度：\n对话：\n{dialogue}\n角色语言指纹：{characters}\n\n测试：去掉'XX说'标签，能否仅凭说话内容/风格分辨谁在说？\n检查：\n1. 各角色用词/句式/语气是否有区分\n2. 是否有角色说话'串味'（不符合其性格）\n3. 是否所有人说话一个腔调（作者腔）\n输出 JSON: {{\"distinction_score\": 0-10, \"blurred_lines\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "detect_exposition_dump",
                    "type": "llm_prompt",
                    "description": "检测对话中的信息倾倒（As you know, Bob）",
                    "parameters": {"type": "object", "properties": {"dialogue": {"type": "string"}}, "required": ["dialogue"]},
                    "prompt_template": "检测信息倾倒：\n\n{dialogue}\n\n'信息倾倒'指借角色之口生硬交代背景（如'你知道的，我们的国王三年前就驾崩了'——角色间本该已知的信息硬说给读者听）。找出这类句子，给出自然化建议。\n输出 JSON: {{\"exposition_dumps\": [{{original, issue, fix}}]}}",
                    "temperature": 0.4,
                },
            ],
            "prompt_snippet": "## 对话工具\n- check_voice_distinction: 检查角色辨识度\n- detect_exposition_dump: 检测信息倾倒\n对话写完做辨识度测试，排查生硬的背景交代。",
        },
    },
    {
        "id": "skill_novel_foreshadowing",
        "name": "伏笔管理工具集",
        "description": "时间线一致性与伏笔回收质量校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_timeline_consistency",
                    "type": "llm_prompt",
                    "description": "检查时间线逻辑一致性",
                    "parameters": {"type": "object", "properties": {"events": {"type": "string", "description": "事件时间线"}}, "required": ["events"]},
                    "prompt_template": "检查时间线：\n\n{events}\n\n检查：\n1. 事件先后顺序是否有矛盾\n2. 角色年龄/时间跨度是否自洽\n3. 因果是否成立（结果不能早于原因）\n4. 是否有'穿帮'（某人在两地同时出现）\n输出 JSON: {{\"conflicts\": [...], \"score\": 0-10}}",
                    "temperature": 0.2,
                },
                {
                    "name": "evaluate_payoff",
                    "type": "llm_prompt",
                    "description": "评估伏笔回收质量（太明显/太隐晦/恰好）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "setup": {"type": "string", "description": "伏笔埋设内容"},
                            "payoff": {"type": "string", "description": "伏笔回收内容"},
                        },
                        "required": ["setup", "payoff"],
                    },
                    "prompt_template": "评估伏笔回收：\n埋设：{setup}\n回收：{payoff}\n\n评估：\n1. 回收前是否有足够铺垫（不能突兀）\n2. 是否太明显（读者早猜到=无惊喜）或太隐晦（读者没印象=无效）\n3. 回收是否呼应埋设（不能'埋A收B'）\n输出 JSON: {{\"quality\": \"太明显/太隐晦/恰好\", \"issues\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 伏笔工具\n- check_timeline_consistency: 检查时间线逻辑\n- evaluate_payoff: 评估伏笔回收质量\n管理伏笔时验证时间线自洽，回收时评估质量。",
        },
    },
    {
        "id": "skill_novel_style",
        "name": "文风编辑工具集",
        "description": "文风一致性分析",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "analyze_style_consistency",
                    "type": "llm_prompt",
                    "description": "分析文风一致性（叙事距离/用词/句式）",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "baseline": {"type": "string", "description": "风格基准"}}, "required": ["text"]},
                    "prompt_template": "分析文风一致性：\n基准：{baseline}\n文本：\n{text}\n\n检查：\n1. 叙事距离是否一致（贴近/中等/疏远）\n2. 用词倾向是否统一（雅/俗/冷/暖）\n3. 句式是否有变化（避免连续3句同结构）\n4. 是否有陈词滥调\n输出 JSON: {{\"consistency_score\": 0-10, \"deviations\": [...], \"cliches\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 文风工具\n- analyze_style_consistency: 分析文风一致性\n逐章审阅时对照风格基准检查偏离。",
        },
    },
    {
        "id": "skill_novel_proofread",
        "name": "审校编辑工具集",
        "description": "人名/地名一致性校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_name_consistency",
                    "type": "llm_prompt",
                    "description": "检查人名/地名一致性",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "name_registry": {"type": "string", "description": "已确定的人名地名表"}}, "required": ["text"]},
                    "prompt_template": "检查名称一致性：\n名称表：{name_registry}\n文本：\n{text}\n\n找出：1)人名/地名前后不一致（如'张三'一会写成'张叁'）；2)未登记的新名称（可能是笔误）；3)称谓混乱。\n输出 JSON: {{\"inconsistencies\": [...], \"new_names\": [...]}}",
                    "temperature": 0.2,
                },
            ],
            "prompt_snippet": "## 审校工具\n- check_name_consistency: 检查名称一致性\n校对时对照名称表排查笔误和不一致。",
        },
    },
    # ═══ 领域5：研究报告专业工具集 ═══
    {
        "id": "skill_research_investigate",
        "name": "调研工具集",
        "description": "来源可信度评估与交叉验证",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "assess_source_credibility",
                    "type": "llm_prompt",
                    "description": "评估信息来源可信度",
                    "parameters": {
                        "type": "object",
                        "properties": {"source_info": {"type": "string", "description": "来源信息（出处/作者/时间/内容）"}},
                        "required": ["source_info"],
                    },
                    "prompt_template": "你是信息核查专家。评估来源可信度：\n\n{source_info}\n\n四维评估：\n1. 权威性（作者/机构资质，学术>行业>媒体>自媒体）\n2. 时效性（信息是否过时）\n3. 客观性（是否有利益相关/立场偏见）\n4. 可验证性（能否交叉核实）\n输出 JSON: {{\"credibility\": 0-10, \"dimension_scores\": {{...}}, \"warnings\": [...], \"recommendation\": \"采用/谨慎采用/弃用\"}}",
                    "system_prompt": "你是严谨的事实核查员。",
                    "temperature": 0.2,
                },
                {
                    "name": "cross_verify",
                    "type": "llm_prompt",
                    "description": "对关键信息做交叉验证分析",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string", "description": "待验证的关键信息"},
                            "sources": {"type": "string", "description": "多个来源对此的说法"},
                        },
                        "required": ["claim", "sources"],
                    },
                    "prompt_template": "交叉验证：\n关键信息：{claim}\n各来源说法：{sources}\n\n分析：\n1. 各来源是否一致\n2. 是否独立（还是互相引用同一源头，伪独立）\n3. 分歧点及可能原因\n4. 可信结论\n输出 JSON: {{\"consistent\": bool, \"independent_count\": N, \"conflicts\": [...], \"conclusion\": \"...\", \"confidence\": 0-1}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 调研工具\n- assess_source_credibility: 评估来源可信度\n- cross_verify: 交叉验证关键信息\n关键信息必须 ≥2 个独立来源交叉验证。用 read_url 读取原文而非只看搜索摘要。",
        },
    },
    {
        "id": "skill_research_analysis",
        "name": "数据分析工具集",
        "description": "图表类型推荐与统计有效性校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "suggest_chart_type",
                    "type": "llm_prompt",
                    "description": "根据数据性质推荐图表类型",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "data_nature": {"type": "string", "description": "数据性质（比较/趋势/构成/分布/关系）"},
                            "data_description": {"type": "string"},
                        },
                        "required": ["data_nature"],
                    },
                    "prompt_template": "推荐图表类型：\n数据性质：{data_nature}\n数据描述：{data_description}\n\n映射：比较→柱状图/条形图；趋势→折线图/面积图；构成→饼图/堆叠柱/树图；分布→直方图/箱线图/散点；关系→散点/气泡/热力。给出推荐+理由+注意事项。\n输出 JSON: {{\"recommended\": \"...\", \"rationale\": \"...\", \"cautions\": [...]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "check_statistical_validity",
                    "type": "llm_prompt",
                    "description": "检查统计结论的有效性（相关≠因果等）",
                    "parameters": {"type": "object", "properties": {"analysis": {"type": "string", "description": "分析结论"}}, "required": ["analysis"]},
                    "prompt_template": "检查统计有效性：\n\n{analysis}\n\n检查常见谬误：\n1. 相关当因果（correlation ≠ causation）\n2. 样本量不足却下强结论\n3. 幸存者偏差\n4. 忽略置信区间/p值\n5. 辛普森悖论（分组趋势与整体相反）\n输出 JSON: {{\"issues\": [...], \"severity\": \"...\", \"corrections\": [...]}}",
                    "temperature": 0.2,
                },
            ],
            "prompt_snippet": "## 数据分析工具\n- suggest_chart_type: 推荐图表类型\n- check_statistical_validity: 检查统计有效性\n下结论前用 check_statistical_validity 排查相关≠因果等谬误。",
        },
    },
    {
        "id": "skill_research_writing",
        "name": "报告撰写工具集",
        "description": "MECE 与金字塔结构校验",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "check_mece",
                    "type": "llm_prompt",
                    "description": "检查论点结构是否 MECE（相互独立完全穷尽）",
                    "parameters": {"type": "object", "properties": {"structure": {"type": "string", "description": "报告论点结构/章节大纲"}}, "required": ["structure"]},
                    "prompt_template": "检查 MECE：\n\n{structure}\n\nMECE = Mutually Exclusive（相互独立，无重叠）+ Collectively Exhaustive（完全穷尽，无遗漏）。检查：\n1. 各论点/章节是否有内容重叠\n2. 是否有遗漏的关键维度\n3. 分类逻辑是否统一\n输出 JSON: {{\"mece_score\": 0-10, \"overlaps\": [...], \"gaps\": [...]}}",
                    "temperature": 0.3,
                },
                {
                    "name": "check_pyramid",
                    "type": "llm_prompt",
                    "description": "检查是否遵循金字塔原理（结论先行）",
                    "parameters": {"type": "object", "properties": {"section": {"type": "string"}}, "required": ["section"]},
                    "prompt_template": "检查金字塔结构：\n\n{section}\n\n金字塔原理：结论先行，论据支撑，自上而下。检查：\n1. 是否先给结论再给论据（而非流水账铺垫到最后才说结论）\n2. 每个论据是否支撑上层结论\n3. 执行摘要是否能独立传达核心\n输出 JSON: {{\"score\": 0-10, \"issues\": [...], \"suggestions\": [...]}}",
                    "temperature": 0.3,
                },
            ],
            "prompt_snippet": "## 撰写工具\n- check_mece: 检查论点 MECE\n- check_pyramid: 检查金字塔结构\n搭建报告结构后用 check_mece 验证无重叠遗漏，写作用金字塔原理结论先行。",
        },
    },
    {
        "id": "skill_research_review",
        "name": "报告审阅工具集",
        "description": "认知偏见检测与逻辑链验证",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "detect_bias",
                    "type": "llm_prompt",
                    "description": "检测报告中的认知偏见",
                    "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
                    "prompt_template": "检测认知偏见：\n\n{content}\n\n常见偏见：1)确认偏差（只选支持预设结论的证据）；2)选择偏差（样本不代表总体）；3)幸存者偏差（只看成功案例）；4)锚定效应（过度依赖首个信息）；5)因果倒置。逐一排查并举证。\n输出 JSON: {{\"biases_found\": [{{type, evidence, impact}}], \"severity\": \"...\"}}",
                    "temperature": 0.3,
                },
                {
                    "name": "validate_logic_chain",
                    "type": "llm_prompt",
                    "description": "验证论证逻辑链（前提→推理→结论）",
                    "parameters": {"type": "object", "properties": {"argument": {"type": "string"}}, "required": ["argument"]},
                    "prompt_template": "验证逻辑链：\n\n{argument}\n\n检查前提→推理→结论是否成立：\n1. 前提是否真实/有据\n2. 推理是否有效（无跳跃/无谬误）\n3. 结论是否被前提充分支撑\n4. 有无逻辑跳跃（缺失中间环节）\n输出 JSON: {{\"valid\": bool, \"gaps\": [...], \"fallacies\": [...]}}",
                    "temperature": 0.2,
                },
            ],
            "prompt_snippet": "## 审阅工具\n- detect_bias: 检测认知偏见\n- validate_logic_chain: 验证逻辑链\n审阅时必查偏见和逻辑跳跃。",
        },
    },
    {
        "id": "skill_research_chart",
        "name": "图表设计工具集",
        "description": "图表规格设计（类型/标题/配色）",
        "source": "platform",
        "manifest": {
            "version": "1.0.0",
            "kind": "executable",
            "tool_definitions": [
                {
                    "name": "design_chart_spec",
                    "type": "llm_prompt",
                    "description": "设计图表规格（类型/标题/配色/可访问性）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "data": {"type": "string", "description": "要可视化的数据"},
                            "key_finding": {"type": "string", "description": "想传达的核心发现"},
                        },
                        "required": ["data", "key_finding"],
                    },
                    "prompt_template": "设计图表规格：\n数据：{data}\n核心发现：{key_finding}\n\n给出：\n1. 图表类型（匹配数据性质）\n2. 标题（标题即结论，不是'XX统计图'而是'A比B高30%'）\n3. 配色（主色+强调色，色盲友好）\n4. 视觉重点（用颜色/大小引导注意）\n5. 去除的冗余（3D/无用网格/装饰）\n6. 配套说明（1-2句解读）\n输出 JSON: {{\"chart_type\": \"...\", \"title\": \"...\", \"colors\": [...], \"caption\": \"...\"}}",
                    "temperature": 0.4,
                },
            ],
            "prompt_snippet": "## 图表工具\n- design_chart_spec: 设计图表规格\n设计前先想清楚'这张图要传达什么发现'，标题即结论。可配合 dataviz 能力生成实际图表。",
        },
    },
]

TOOL_TO_PLATFORM_SKILL = {
    "file_read": "skill_platform_file_read",
    "file_write": "skill_platform_file_write",
    "web_search": "skill_platform_web_search",
    # === P3 新增 ===
    "generate_image": "skill_platform_generate_image",
    "generate_video": "skill_platform_generate_video",
    "generate_music": "skill_platform_generate_music",
    "shell_exec": "skill_platform_shell_exec",
    "asset_query": "skill_platform_asset_query",
    "llm_call": "skill_platform_llm_call",
    "knowledge_query": "skill_platform_knowledge_query",
    "read_url": "skill_platform_read_url",
}


class SkillRegistry:
    """Skill CRUD（SQLite）"""

    async def seed_platform_skills(self) -> None:
        db = await get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for sk in PLATFORM_SKILLS:
            cursor = await db.execute("SELECT id FROM skills WHERE id = ?", (sk["id"],))
            if await cursor.fetchone():
                if sk["id"] == "skill_platform_web_search":
                    await db.execute(
                        "UPDATE skills SET description = ?, updated_at = ? WHERE id = ?",
                        (sk["description"], now, sk["id"]),
                    )
                continue
            await db.execute(
                """INSERT INTO skills (id, name, description, source, status, manifest, mcp_config, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'active', ?, '{}', ?, ?)""",
                (
                    sk["id"],
                    sk["name"],
                    sk["description"],
                    sk["source"],
                    json.dumps(sk["manifest"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
        await db.commit()

    async def list(
        self,
        *,
        source: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[SkillDefinition]:
        db = await get_db()
        sql = "SELECT * FROM skills WHERE 1=1"
        params: list[Any] = []
        if source:
            sql += " AND source = ?"
            params.append(source)
        if status:
            sql += " AND status = ?"
            params.append(status)
        else:
            sql += " AND status != 'archived'"
        sql += " ORDER BY source, name"
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_skill(dict(r)) for r in rows]

    async def get(self, skill_id: str) -> Optional[SkillDefinition]:
        db = await get_db()
        cursor = await db.execute("SELECT * FROM skills WHERE id = ?", (skill_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_skill(dict(row))

    async def create(self, data: dict) -> SkillDefinition:
        db = await get_db()
        skill_id = data.get("id") or f"skill_{int(datetime.now().timestamp() * 1000)}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mcp_config = data.get("mcp_config") or {}
        manifest = self._normalize_manifest(
            data.get("manifest") or {},
            data.get("source", "custom"),
            mcp_config,
        )
        await db.execute(
            """INSERT INTO skills (id, name, description, source, status, manifest, mcp_config, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                skill_id,
                data.get("name", skill_id),
                data.get("description", ""),
                data.get("source", "custom"),
                data.get("status", "active"),
                json.dumps(manifest, ensure_ascii=False),
                json.dumps(mcp_config, ensure_ascii=False),
                now,
                now,
            ),
        )
        await db.commit()
        skill = await self.get(skill_id)
        if skill:
            from core.skills.tool_materializer import register_skill_tools, unregister_skill_tools
            unregister_skill_tools(skill_id)
            if skill.status == "active":
                register_skill_tools(skill_id, skill.manifest)
            # S4 新增：MCP 类型 Skill 创建时刷新 bridge（替代原 expand 读路径的副作用）
            if data.get("source") == "mcp" or (skill.manifest or {}).get("kind") == "mcp":
                try:
                    from core.skills.mcp_bridge import MCPToolBridge
                    await MCPToolBridge.get_instance().ensure_registered()
                except Exception:
                    pass
        return skill

    async def update(self, skill_id: str, updates: dict) -> Optional[SkillDefinition]:
        existing = await self.get(skill_id)
        if not existing:
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mcp_config = updates.get("mcp_config", existing.mcp_config)
        manifest = self._normalize_manifest(
            updates.get("manifest", existing.manifest.model_dump()),
            updates.get("source", existing.source),
            mcp_config,
        )
        db = await get_db()
        await db.execute(
            """UPDATE skills SET name = ?, description = ?, source = ?, status = ?,
               manifest = ?, mcp_config = ?, updated_at = ? WHERE id = ?""",
            (
                updates.get("name", existing.name),
                updates.get("description", existing.description),
                updates.get("source", existing.source),
                updates.get("status", existing.status),
                json.dumps(manifest, ensure_ascii=False),
                json.dumps(mcp_config, ensure_ascii=False),
                now,
                skill_id,
            ),
        )
        await db.commit()
        skill = await self.get(skill_id)
        if skill:
            from core.skills.tool_materializer import register_skill_tools, unregister_skill_tools
            unregister_skill_tools(skill_id)
            if skill.status == "active":
                register_skill_tools(skill_id, skill.manifest)
            # S4 新增：MCP 类型 Skill 更新时刷新 bridge
            if updates.get("source") == "mcp" or (skill.manifest or {}).get("kind") == "mcp":
                try:
                    from core.skills.mcp_bridge import MCPToolBridge
                    await MCPToolBridge.get_instance().ensure_registered()
                except Exception:
                    pass
        return skill

    async def delete(self, skill_id: str) -> bool:
        """软删除（归档）"""
        existing = await self.get(skill_id)
        if not existing:
            return False
        if existing.source == "platform":
            return False
        from core.skills.tool_materializer import unregister_skill_tools
        unregister_skill_tools(skill_id)
        await self.update(skill_id, {"status": "archived"})
        return True

    async def list_mcp_servers(self) -> List[MCPServerDefinition]:
        db = await get_db()
        cursor = await db.execute("SELECT * FROM mcp_servers ORDER BY name")
        rows = await cursor.fetchall()
        return [self._row_to_mcp(dict(r)) for r in rows]

    async def get_mcp_server(self, server_id: str) -> Optional[MCPServerDefinition]:
        db = await get_db()
        cursor = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_mcp(dict(row))

    async def create_mcp_server(self, data: dict) -> MCPServerDefinition:
        db = await get_db()
        server_id = data.get("id") or f"mcp_{int(datetime.now().timestamp() * 1000)}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            """INSERT INTO mcp_servers
               (id, name, transport, url, command, args, env, auth_header, enabled, health_status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                server_id,
                data.get("name", server_id),
                data.get("transport", "sse"),
                data.get("url", ""),
                data.get("command", ""),
                json.dumps(data.get("args") or []),
                json.dumps(data.get("env") or {}),
                data.get("auth_header", ""),
                int(data.get("enabled", True)),
                data.get("health_status", "unknown"),
                now,
                now,
            ),
        )
        await db.commit()
        return await self.get_mcp_server(server_id)

    async def update_mcp_server(self, server_id: str, updates: dict) -> Optional[MCPServerDefinition]:
        existing = await self.get_mcp_server(server_id)
        if not existing:
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db = await get_db()
        await db.execute(
            """UPDATE mcp_servers SET name = ?, transport = ?, url = ?, command = ?,
               args = ?, env = ?, auth_header = ?, enabled = ?, health_status = ?, updated_at = ?
               WHERE id = ?""",
            (
                updates.get("name", existing.name),
                updates.get("transport", existing.transport),
                updates.get("url", existing.url),
                updates.get("command", existing.command),
                json.dumps(updates.get("args", existing.args)),
                json.dumps(updates.get("env", existing.env)),
                updates.get("auth_header", existing.auth_header),
                int(updates.get("enabled", existing.enabled)),
                updates.get("health_status", existing.health_status),
                now,
                server_id,
            ),
        )
        await db.commit()
        return await self.get_mcp_server(server_id)

    async def delete_mcp_server(self, server_id: str) -> bool:
        db = await get_db()
        cursor = await db.execute("SELECT id FROM mcp_servers WHERE id = ?", (server_id,))
        if not await cursor.fetchone():
            return False
        await db.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        await db.commit()
        return True

    def _normalize_manifest(self, manifest: Any, source: str, mcp_config: dict) -> dict:
        from core.skills.kind import ensure_manifest_kind

        if isinstance(manifest, SkillManifest):
            manifest = manifest.model_dump()
        elif not isinstance(manifest, dict):
            manifest = {}
        mcp_config = mcp_config or {}
        return ensure_manifest_kind(manifest, source=source or "custom", mcp_config=mcp_config)

    def _row_to_skill(self, row: dict) -> SkillDefinition:
        mcp_config = json.loads(row.get("mcp_config") or "{}")
        manifest_raw = self._normalize_manifest(
            json.loads(row.get("manifest") or "{}"),
            row.get("source") or "custom",
            mcp_config,
        )
        return SkillDefinition(
            id=row["id"],
            name=row["name"],
            description=row.get("description") or "",
            source=row.get("source") or "custom",
            status=row.get("status") or "active",
            manifest=SkillManifest(**manifest_raw),
            mcp_config=mcp_config,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def _row_to_mcp(self, row: dict) -> MCPServerDefinition:
        return MCPServerDefinition(
            id=row["id"],
            name=row["name"],
            transport=row.get("transport") or "sse",
            url=row.get("url") or "",
            command=row.get("command") or "",
            args=json.loads(row.get("args") or "[]"),
            env=json.loads(row.get("env") or "{}"),
            auth_header=row.get("auth_header") or "",
            enabled=bool(row.get("enabled", 1)),
            health_status=row.get("health_status") or "unknown",
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry
