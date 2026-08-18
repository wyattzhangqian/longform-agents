"""Tool 执行系统 — Agent 可用的动作空间

提供：
- ToolRegistry：管理可用工具的注册表
- ToolExecutor：解析 LLM function call → 执行工具 → 返回结果
- 内置工具：file_read, web_search, shell_exec
"""

from __future__ import annotations
import re
import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Optional, Callable, Any, Dict, List
from dataclasses import dataclass, field
from core.agent.types import AgentTool
from core.agent.tool_execution import ToolExecutionContext, validate_tool_args
from core.gateway.permission import PermissionGate, PermissionMode

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_SECONDS = 120


# ============================================================
# Tool 定义
# ============================================================

@dataclass
class ToolDefinition:
    """工具定义（运行时）"""
    name: str
    description: str
    parameters: dict           # JSON Schema 格式的参数定义
    handler: Callable          # 异步处理函数 async def handler(**kwargs) -> str
    category: str = "general"
    requires_approval: bool = False  # 是否需要用户审批（等价 permission_mode=ask）
    permission_mode: str = ""        # allow | ask | deny（优先于 requires_approval）
    risk_level: str = "low"          # low | medium | high | critical
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS


# ============================================================
# ToolRegistry — 工具注册表
# ============================================================

class ToolRegistry:
    """全局工具注册表（Platform-Core v2: 支持命名空间隔离）

    通用工具注册在空 namespace，领域工具注册在各自 namespace。

    用法：
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="file_read",
            description="读取文件内容",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=file_read_handler,
        ))  # 空 namespace = 全局工具
        registry.register(comic_tool, namespace="comic")  # 领域工具
        tool = registry.get("file_read")
        tool = registry.get("generate_comic", namespace="comic")
    """

    _instance: Optional["ToolRegistry"] = None

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}                          # name → tool (全局)
        self._namespaces: Dict[str, Dict[str, ToolDefinition]] = {}           # ns → {name → tool}
        self._presets: Dict[str, List[dict]] = {}                            # tool_name → [{preset_id, label, params}]
        self._register_builtins()
        self._register_builtin_presets()

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        """获取单例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, tool: ToolDefinition, namespace: str = ""):
        """注册一个工具，可选指定命名空间"""
        if namespace:
            self._namespaces.setdefault(namespace, {})[tool.name] = tool
        else:
            self._tools[tool.name] = tool

    def get(self, name: str, namespace: str = "") -> Optional[ToolDefinition]:
        """获取工具定义（支持 namespace/name 与 MCP 代理名）"""
        if namespace:
            return self._namespaces.get(namespace, {}).get(name)
        if "/" in name:
            ns, bare = name.split("/", 1)
            found = self._namespaces.get(ns, {}).get(bare)
            if found:
                return found
            name = bare
        return self._tools.get(name)

    def resolve(self, tool_name: str, namespaces: List[str] = None) -> Optional[ToolDefinition]:
        """解析工具名（全局 / namespace/name / MCP 代理）"""
        tool = self.get(tool_name)
        if tool:
            return tool
        if namespaces:
            for ns in namespaces:
                found = self.get(tool_name, namespace=ns)
                if found:
                    return found
                if "/" not in tool_name:
                    found = self.get(tool_name.split("/")[-1], namespace=ns)
                    if found:
                        return found
        return None

    def list_names(self, namespace: str = "") -> List[str]:
        """列出工具名"""
        if namespace:
            return list(self._namespaces.get(namespace, {}).keys())
        return list(self._tools.keys())

    def list_namespaces(self) -> List[str]:
        """列出所有命名空间"""
        return list(self._namespaces.keys())

    def register_preset(self, tool_name: str, preset_id: str, label: str, params: dict):
        """注册工具参数预设（如生图的真人风/漫画风/动漫风）"""
        self._presets.setdefault(tool_name, []).append({
            "preset_id": preset_id, "label": label, "params": params,
        })

    def list_presets(self, tool_name: str) -> List[dict]:
        """获取工具的预设列表"""
        return self._presets.get(tool_name, [])

    def get_preset(self, tool_name: str, preset_id: str) -> Optional[dict]:
        """获取单个预设"""
        for p in self._presets.get(tool_name, []):
            if p["preset_id"] == preset_id:
                return p
        return None

    def _register_builtin_presets(self):
        """注册内置工具预设（风格预设归属地）"""
        # 生图预设
        self.register_preset("generate_image", "realistic", "写实风", {
            "style": "photorealistic, high detail, natural lighting, 8K",
            "width": 1024, "height": 1024,
        })
        self.register_preset("generate_image", "comic", "漫画风", {
            "style": "manga style, black and white, screentone, dynamic lines",
            "width": 768, "height": 1024,
        })
        self.register_preset("generate_image", "anime", "动漫风", {
            "style": "anime style, vibrant colors, cel shading, detailed eyes",
            "width": 1024, "height": 1024,
        })
        self.register_preset("generate_image", "watercolor", "水彩风", {
            "style": "watercolor painting, soft colors, artistic brush strokes",
            "width": 1024, "height": 1024,
        })

    def list_for_agent(
        self,
        agent_tools: List[AgentTool] = None,
        namespaces: List[str] = None,
    ) -> List[ToolDefinition]:
        """列出 Agent 可用的工具

        合并全局工具 + 指定命名空间的领域工具，再根据 agent_tools 白名单过滤。
        """
        result = list(self._tools.values())  # 全局工具
        if namespaces:
            for ns in namespaces:
                result.extend(self._namespaces.get(ns, {}).values())
        if agent_tools:
            names = {t.name for t in agent_tools}
            result = [t for t in result if t.name in names]
        return result

    def to_openai_format(
        self,
        tool_names: List[str] = None,
        namespaces: List[str] = None,
    ) -> List[dict]:
        """转换为 OpenAI function calling 格式"""
        tools = self.list_for_agent(namespaces=namespaces)
        if tool_names:
            tools = [t for t in tools if t.name in tool_names]

        result = []
        for tool in tools:
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
        return result

    def _register_builtins(self):
        """注册内置工具"""
        # 文件读取
        self.register(ToolDefinition(
            name="file_read",
            description="读取指定文件的内容。参数 path: 文件路径（相对于输出目录或绝对路径）",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读取的文件路径"},
                    "max_chars": {"type": "integer", "description": "最多读取字符数，默认 3000"},
                },
                "required": ["path"],
            },
            handler=self._file_read,
            category="io",
        ))

        # 文件写入
        self.register(ToolDefinition(
            name="file_write",
            description="将内容写入指定文件。参数 path: 文件路径，content: 要写入的内容",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["path", "content"],
            },
            handler=self._file_write,
            category="io",
        ))

        # Web 搜索（Tavily / SerpAPI / DuckDuckGo / Wikipedia 四通道）
        self.register(ToolDefinition(
            name="web_search",
            description="搜索互联网获取最新信息。参数 query: 搜索关键词",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
            handler=self._web_search,
            category="web",
        ))

        # Shell 执行（子进程沙箱 + 人工审批门禁）
        self.register(ToolDefinition(
            name="shell_exec",
            description=(
                "在沙箱中执行 shell 命令（工作目录固定为项目输出目录，"
                "有 CPU/内存/超时限制，需人工审批）。参数 command: 要执行的命令"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 120"},
                },
                "required": ["command"],
            },
            handler=self._shell_exec,
            category="system",
            risk_level="high",  # PermissionGate → ask，必须人工审批
            timeout_seconds=150,
        ))

        # AI 生图（Volcengine Seedream / OpenAI DALL-E，按 Agent model_profile.image 配置）
        self.register(ToolDefinition(
            name="generate_image",
            description=(
                "使用 AI 生成图片。参数: prompt(图片描述), style(风格，可选), "
                "width(宽度，默认1024), height(高度，默认1024), format(格式，默认png)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片描述，如'赛博朋克城市夜景，霓虹灯，雨雾'"},
                    "style": {"type": "string", "description": "风格描述，会自动合并到prompt中"},
                    "width": {"type": "integer", "description": "宽度（像素），默认1024"},
                    "height": {"type": "integer", "description": "高度（像素），默认1024"},
                    "format": {"type": "string", "description": "格式: png/jpg/webp，默认png"},
                },
                "required": ["prompt"],
            },
            handler=ToolRegistry._generate_image,
            category="media",
            timeout_seconds=120,
        ))

        # AI 生视频（Volcengine Seedance / OpenAI Sora，按 Agent model_profile.video 配置）
        self.register(ToolDefinition(
            name="generate_video",
            description=(
                "使用 AI 生成视频。参数: prompt(视频描述), image_url(参考图URL，图生视频时用)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "视频描述，如'海浪拍打礁石，慢动作，4K'"},
                    "image_url": {"type": "string", "description": "参考图URL（图生视频时提供）"},
                },
                "required": ["prompt"],
            },
            handler=ToolRegistry._generate_video,
            category="media",
            timeout_seconds=300,
        ))

        # AI 音乐生成（Suno API，异步提交→轮询→下载）
        self.register(ToolDefinition(
            name="generate_music",
            description=(
                "使用 AI 生成音乐/歌曲。参数: prompt(音乐描述), lyrics(歌词，可选), "
                "instrumental(是否纯器乐，默认false), style(风格，如pop/rock/jazz), duration(时长秒数，默认60)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "音乐风格和情绪描述，如'温暖的民谣，木吉他伴奏'"},
                    "lyrics": {"type": "string", "description": "歌词文本（可选，有人声时提供）"},
                    "instrumental": {"type": "boolean", "description": "是否纯器乐，默认false"},
                    "style": {"type": "string", "description": "音乐风格标签，如 pop/rock/jazz/classical"},
                    "duration": {"type": "integer", "description": "时长（秒），默认60，最大240"},
                },
                "required": ["prompt"],
            },
            handler=ToolRegistry._generate_music,
            category="media",
            timeout_seconds=360,  # 提交+轮询+下载最多6分钟
        ))

        # 平台资产查询
        self.register(ToolDefinition(
            name="asset_query",
            description="查询平台资产库。获取项目引用的资产内容，或搜索平台资产。",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list_refs", "get_content", "search"],
                               "description": "list_refs=列出项目引用; get_content=读取资产全文; search=搜索平台资产"},
                    "asset_id": {"type": "string", "description": "资产ID（get_content时）"},
                    "name": {"type": "string", "description": "资产名称/别名（get_content的备选）"},
                    "query": {"type": "string", "description": "搜索关键词（search时）"},
                },
                "required": ["action"],
            },
            handler=ToolRegistry._asset_query,
            category="platform",
        ))

        # === P0 新增：llm_call ===
        self.register(ToolDefinition(
            name="llm_call",
            description=(
                "调用 LLM 进行辅助推理。用于执行中需要额外 AI 分析的场景，"
                "如生成角色描述、评估情节一致性、翻译段落、提取结构化信息等。"
                "参数: prompt(提示词,必填), system_prompt(系统提示词,可选), "
                "model(模型名,可选), temperature(温度,可选), max_tokens(最大token,可选)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "提示词（用户消息）"},
                    "system_prompt": {"type": "string", "description": "系统提示词（可选）"},
                    "model": {"type": "string", "description": "模型名（可选）"},
                    "temperature": {"type": "number", "description": "温度 0-2（可选）"},
                    "max_tokens": {"type": "integer", "description": "最大 token 数（可选）"},
                },
                "required": ["prompt"],
            },
            handler=ToolRegistry._llm_call,
            category="ai",
            timeout_seconds=120,
        ))

        # === P0 新增：knowledge_query ===
        self.register(ToolDefinition(
            name="knowledge_query",
            description=(
                "检索知识库获取参考资料。用于查找角色设定、世界观规则、"
                "前情摘要、风格指南、历史剧情等。"
                "参数: query(语义检索关键词), domain(知识领域,可选), "
                "top_k(返回数量,默认5), tags(标签检索,逗号分隔,可选)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "语义检索关键词"},
                    "domain": {"type": "string", "description": "知识领域，默认自动推断"},
                    "top_k": {"type": "integer", "description": "返回条目数，默认5"},
                    "tags": {"type": "string", "description": "按标签检索（逗号分隔）"},
                },
                "required": [],
            },
            handler=ToolRegistry._knowledge_query,
            category="knowledge",
        ))

        # === P0 新增：read_url ===
        self.register(ToolDefinition(
            name="read_url",
            description=(
                "读取指定 URL 的网页内容。用于获取参考资料、研究文章、风格参考等。"
                "与 web_search 的区别：web_search 是搜索关键词获取摘要，"
                "read_url 是打开特定网页获取完整正文。"
                "参数: url(网址,必填), max_chars(最大字符数,默认8000), extract_mode(text/raw,默认text)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要读取的 URL（http/https）"},
                    "max_chars": {"type": "integer", "description": "最多返回字符数，默认8000"},
                    "extract_mode": {"type": "string", "enum": ["text", "raw"], "description": "提取模式"},
                },
                "required": ["url"],
            },
            handler=ToolRegistry._read_url,
            category="web",
            timeout_seconds=45,
        ))

    # ==================== 内置工具实现 ====================

    OUTPUTS_DIR = Path("outputs").resolve()

    @staticmethod
    def resolve_tool_path(output_dir: str, path: str) -> str:
        """将 Agent 传入 path 解析为相对 outputs/ 根的路径（避免 outputs/outputs/ 双前缀）"""
        raw = (path or "").replace("\\", "/").strip().lstrip("/")
        while raw.startswith("outputs/"):
            raw = raw[len("outputs/"):]
        if output_dir:
            # Agent 已自带项目目录前缀时不再重复拼接（proj_x/proj_x/file 双前缀）
            od = output_dir.replace("\\", "/").strip("/")
            while raw == od or raw.startswith(od + "/"):
                raw = raw[len(od):].lstrip("/")
            joined = Path(output_dir) / raw
            return joined.as_posix()
        return raw

    @staticmethod
    def _normalize_output_dir(output_dir: str) -> str:
        """转为相对 OUTPUTS_DIR 的路径，避免 outputs/outputs/ 双前缀"""
        root = ToolRegistry.OUTPUTS_DIR
        raw = (output_dir or "").strip()
        if not raw or raw in ("outputs", "."):
            return ""
        p = Path(raw)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        try:
            rel = p.relative_to(root)
            return "" if str(rel) == "." else str(rel)
        except ValueError:
            stripped = raw.replace("\\", "/").lstrip("/")
            if stripped.startswith("outputs/"):
                return stripped[len("outputs/"):]
            return stripped

    @staticmethod
    def _validate_path(path: str, project_subdir: str = "") -> Path:
        """校验文件路径安全性，防止路径遍历攻击（用 relative_to，禁止 startswith 前缀绕过）"""
        root = ToolRegistry.OUTPUTS_DIR
        if project_subdir:
            root = (root / project_subdir).resolve()
        p = Path(path)
        if not p.is_absolute():
            p = root / path
        p = p.resolve()
        try:
            p.relative_to(root)
        except ValueError as e:
            raise ValueError(f"路径越权访问: {path} 不在 {root} 下") from e
        if ".." in Path(path).parts:
            raise ValueError(f"路径包含非法遍历: {path}")
        return p

    @staticmethod
    def _list_dir_hint(p: Path) -> str:
        """目录现有文件提示（让 Agent 下一轮自我纠正，不再瞎猜文件名）"""
        try:
            parent = p.parent
            if not parent.exists():
                return ""
            try:
                parent.resolve().relative_to(ToolRegistry.OUTPUTS_DIR)
            except ValueError:
                return ""
            files = sorted(
                f.name for f in parent.iterdir()
                if f.is_file() and not f.name.startswith(".")
            )[:15]
            if not files:
                return "（该目录为空）"
            return f"。该目录现有文件: {', '.join(files)}。请改用上述真实存在的文件名重试"
        except Exception:
            return ""

    @staticmethod
    async def _file_read(path: str, max_chars: int = 3000, **kwargs) -> str:
        """读取文件内容"""
        try:
            project_subdir = kwargs.get("_project_subdir", "")
            p = ToolRegistry._validate_path(path, project_subdir=project_subdir)
            if not p.exists():
                return f"[错误] 文件不存在: {path}{ToolRegistry._list_dir_hint(p)}"
            content = p.read_text(encoding="utf-8")
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... (截断，共 {len(content)} 字符)"
            return content
        except ValueError as e:
            return f"[错误] 路径不合法: {e}"
        except Exception as e:
            return f"[错误] 读取文件失败: {e}"

    @staticmethod
    async def _file_write(path: str, content: str, **kwargs) -> str:
        """写入文件"""
        try:
            project_subdir = kwargs.get("_project_subdir", "")
            p = ToolRegistry._validate_path(path, project_subdir=project_subdir)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"文件已写入: {path} ({len(content)} 字符)"
        except ValueError as e:
            return f"[错误] 路径不合法: {e}"
        except Exception as e:
            return f"[错误] 写入文件失败: {e}"

    @staticmethod
    async def _web_search(query: str, **kwargs) -> str:
        """Web 搜索 — Tavily / SerpAPI / DuckDuckGo（见 WEB_SEARCH_* 环境变量）"""
        from tools.web_search import perform_web_search

        try:
            return await perform_web_search(query)
        except Exception as e:
            logger.exception("web_search 失败")
            return f"[错误] 搜索失败: {e}"

    @staticmethod
    async def _shell_exec(command: str, timeout: int = 60, _workdir: str = "", **kwargs) -> str:
        """沙箱中执行 shell 命令（_workdir 由 ToolExecutor 注入）"""
        from core.gateway.sandbox import run_sandboxed_shell, DEFAULT_TIMEOUT_SECONDS

        try:
            timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT_SECONDS), 120))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        workdir = _workdir or str(ToolRegistry.OUTPUTS_DIR)
        return await run_sandboxed_shell(command, workdir, timeout=timeout)

    @staticmethod
    async def _generate_image(prompt: str = "", style: str = "", width: int = 1920, height: int = 1920,
                              format: str = "png", **kwargs) -> str:
        """AI 生图 — 按 Agent model_profile.image 配置调用"""
        # 火山引擎最低 3,686,400 像素（1920×1920），不满足自动放大
        _MIN_PIXELS = 3_686_400
        if width * height < _MIN_PIXELS:
            width, height = 1920, 1920
        if style and style.strip():
            prompt = f"{style}风格：{prompt}"
        if not prompt.strip():
            return "[错误] generate_image 需要 prompt 参数"
        try:
            from core.models.clients import ImageGenClient
            from core.models.router import ModelRouter
            from core.agent.registry import get_registry

            # 从当前执行上下文获取 Agent 定义（由 ToolExecutor 注入 context.agent_id）
            agent_id = kwargs.get("_agent_id", "")
            if agent_id:
                agent_def = await get_registry().get(agent_id)
                if agent_def and agent_def.model_profile.image:
                    cfg = ModelRouter.resolve_agent(agent_def, "image")
                else:
                    return "[错误] 当前 Agent 未配置生图模型（model_profile.image）"
            else:
                return "[错误] 无法确定当前 Agent，请联系管理员配置 model_profile.image"

            client = ImageGenClient(cfg)
            result = await client.generate(prompt, width=width, height=height, format=format)
            if result.get("ok"):
                return f"✅ 图片已生成\n- URL: {result.get('url', '')}\n- 尺寸: {result.get('width', '?')}x{result.get('height', '?')}"
            return f"[错误] 生图失败: {result.get('error', '未知错误')}"
        except Exception as e:
            logger.exception("generate_image 失败")
            return f"[错误] 生图异常: {e}"

    @staticmethod
    async def _generate_video(prompt: str = "", image_url: str = "", **kwargs) -> str:
        """AI 生视频 — 按 Agent model_profile.video 配置调用"""
        if not prompt.strip() and not image_url.strip():
            return "[错误] generate_video 需要 prompt 或 image_url 参数"
        try:
            from core.models.clients import VideoGenClient
            from core.models.router import ModelRouter
            from core.agent.registry import get_registry

            agent_id = kwargs.get("_agent_id", "")
            if agent_id:
                agent_def = await get_registry().get(agent_id)
                if agent_def and agent_def.model_profile.video:
                    cfg = ModelRouter.resolve_agent(agent_def, "video")
                else:
                    return "[错误] 当前 Agent 未配置生视频模型（model_profile.video）"
            else:
                return "[错误] 无法确定当前 Agent"

            client = VideoGenClient(cfg)
            result = await client.generate(prompt, image_url=image_url)
            if result.get("ok"):
                return f"✅ 视频已提交生成\n- 返回数据: {str(result.get('body', result))[:300]}"
            hint = result.get('hint', '')
            return f"[错误] 生视频失败: {result.get('error', '未知错误')}{' — ' + hint if hint else ''}"
        except Exception as e:
            logger.exception("generate_video 失败")
            return f"[错误] 生视频异常: {e}"

    @staticmethod
    async def _generate_music(
        prompt: str = "", lyrics: str = "", instrumental: bool = False,
        style: str = "", duration: int = 0, **kwargs,
    ) -> str:
        """AI 音乐生成 — 按 Agent model_profile.music 配置调用"""
        if not prompt.strip() and not lyrics.strip():
            return "[错误] generate_music 需要 prompt 或 lyrics 参数"

        try:
            from core.models.clients import MusicGenClient
            from core.models.router import ModelRouter
            from core.agent.registry import get_registry

            agent_id = kwargs.get("_agent_id", "")
            if agent_id:
                agent_def = await get_registry().get(agent_id)
                if agent_def and agent_def.model_profile.music:
                    cfg = ModelRouter.resolve_agent(agent_def, "music")
                else:
                    return "[错误] 当前 Agent 未配置音乐模型（model_profile.music）"
            else:
                return "[错误] 无法确定当前 Agent，请联系管理员配置 model_profile.music"

            client = MusicGenClient(cfg)
            result = await client.generate(
                prompt=prompt, lyrics=lyrics, instrumental=instrumental,
                style=style, duration=duration,
            )
            if result.get("ok"):
                path = result.get("file_path", "")
                url = result.get("url", "")
                title = result.get("title", "")
                return (
                    f"✅ 音乐已生成并下载\n"
                    f"- 歌名: {title}\n"
                    f"- 本地路径: {path}\n"
                    f"- 在线URL: {url}\n"
                    f"- task_id: {result.get('task_id', '')}"
                )
            return f"[错误] 音乐生成失败: {result.get('error', '未知错误')}"
        except Exception as e:
            logger.exception("generate_music 失败")
            return f"[错误] 音乐生成异常: {e}"

    @staticmethod
    async def _asset_query(args: Dict[str, Any], context: Dict[str, Any]) -> str:
        from core.vault.asset_library import AssetLibrary
        project_id = context.get("project_id", "")
        action = args.get("action", "list_refs")

        if action == "list_refs":
            refs = await AssetLibrary.list_project_refs(project_id)
            if not refs: return "当前项目未引用任何平台资产。"
            lines = ["## 项目引用的平台资产\n"]
            for ref in refs:
                lines.append(f"- **{ref['alias']}** ({ref['asset_type']}): {ref.get('description','')[:80]}")
            return "\n".join(lines)

        elif action == "get_content":
            asset_id = args.get("asset_id", "")
            if not asset_id:
                alias = args.get("name", "")
                refs = await AssetLibrary.list_project_refs(project_id)
                match = next((r for r in refs if r["alias"]==alias or r["asset_name"]==alias), None)
                if match: asset_id = match["asset_id"]
            if not asset_id: return "未找到指定资产。使用 action='list_refs' 查看可用资产。"
            content = await AssetLibrary.get_asset_content(asset_id)
            return content or "资产内容为空。"

        elif action == "search":
            query = args.get("query", "")
            results = await AssetLibrary.list_assets(search=query, limit=10)
            if not results: return f"未找到与 '{query}' 相关的平台资产。"
            lines = [f"## 搜索结果: {query}\n"]
            for a in results:
                lines.append(f"- [{a['name']}] (id:{a['id']}, 类型:{a['type']})")
            return "\n".join(lines)
        return "未知 action。支持: list_refs / get_content / search"

    # === P0 handler：llm_call — Agent 执行中途的辅助推理 ===
    @staticmethod
    async def _llm_call(args: Dict[str, Any], context: Dict[str, Any]) -> str:
        prompt = args.get("prompt", "")
        if not prompt.strip():
            return "[错误] llm_call 需要 prompt 参数"
        from core.gateway.budget import check_budget
        budget_err = check_budget()
        if budget_err:
            return f"[错误] 预算已耗尽: {budget_err}"
        try:
            from core.models.router import ModelRouter, resolve_llm_config
            from tools.llm_client import LLMClient
            agent_id = context.get("agent_id", "")
            if agent_id:
                from core.agent.registry import get_registry
                agent_def = await get_registry().get(agent_id)
                cfg = ModelRouter.resolve_agent(agent_def, "llm") if agent_def else resolve_llm_config()
            else:
                cfg = resolve_llm_config()
            if args.get("model"):
                cfg = {**cfg, "model": args["model"]}
            if args.get("temperature") is not None:
                cfg = {**cfg, "temperature": args["temperature"]}
            if args.get("max_tokens") is not None:
                cfg = {**cfg, "max_tokens": args["max_tokens"]}
            client = LLMClient(cfg)
            result = await client.chat(prompt, system_prompt=args.get("system_prompt") or None)
            if not result or str(result).startswith("[LLM Error]"):
                return f"[错误] LLM 调用失败: {result}"
            return result
        except Exception as e:
            return f"[错误] llm_call 异常: {e}"

    # === P0 handler：knowledge_query — 知识库检索 ===
    @staticmethod
    async def _knowledge_query(args: Dict[str, Any], context: Dict[str, Any]) -> str:
        query = args.get("query", "")
        tags = args.get("tags", "")
        if not query.strip() and not tags.strip():
            return "[错误] knowledge_query 需要 query 或 tags 参数"
        try:
            from core.knowledge.vector_kb import get_knowledge_base
            kb = get_knowledge_base(args.get("domain") or "general")
            top_k = int(args.get("top_k") or 5)
            if tags.strip():
                tag_list = [t.strip() for t in tags.split(",") if t.strip()]
                items = await kb.search_by_tags(tag_list, limit=top_k)
            else:
                items = await kb.retrieve(query=query, top_k=top_k)
            if not items:
                return f"未找到与「{query or tags}」相关的知识条目。"
            lines = [f"## 知识检索结果（{len(items)} 条）\n"]
            for item in items:
                content = item.get("content", "") if isinstance(item, dict) else getattr(item, "content", "")
                itype = item.get("type", "知识") if isinstance(item, dict) else getattr(item, "type", "知识")
                lines.append(f"- **{itype}**: {str(content)[:500]}")
            return "\n".join(lines)
        except Exception as e:
            return f"[错误] 知识库检索失败: {e}"

    # === P0 handler：read_url — 读取网页正文（含 SSRF 防护）===
    @staticmethod
    async def _read_url(args: Dict[str, Any], context: Dict[str, Any]) -> str:
        import re
        from urllib.parse import urlparse
        url = args.get("url", "")
        if not url.strip():
            return "[错误] read_url 需要 url 参数"
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.startswith("192.168.") or host.startswith("10."):
            return "[错误] 不允许访问内网地址"
        if parsed.scheme not in ("http", "https"):
            return "[错误] 仅支持 http/https 协议"
        max_chars = min(max(1000, int(args.get("max_chars") or 8000)), 30000)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; AgentPlatform/1.0)"}
                resp = await client.get(url, headers=headers)
                if resp.status_code >= 400:
                    return f"[错误] HTTP {resp.status_code}: 无法访问 {url}"
                content_type = resp.headers.get("content-type", "")
                raw = resp.text
                if "application/json" in content_type or args.get("extract_mode") == "raw":
                    return raw[:max_chars] + (f"\n... (截断，共 {len(raw)} 字符)" if len(raw) > max_chars else "")
                text = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', '', raw, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', text)
                for entity, char in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"), ('&nbsp;', ' ')]:
                    text = text.replace(entity, char)
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n... (截断，共 {len(text)} 字符)"
                return text if text.strip() else "(页面内容为空或无法解析)"
        except Exception as e:
            return f"[错误] 读取 URL 失败: {e}"


# ============================================================
# ToolExecutor — 工具执行器
# ============================================================

class ToolExecutor:
    """解析 LLM 输出的 function call 并执行

    支持两种 function call 格式：
    1. OpenAI 标准格式：{"name": "...", "arguments": {...}}
    2. 自然语言格式：`TOOL: tool_name(arg1=val1, arg2=val2)`

    用法：
        executor = ToolExecutor(registry)
        result = await executor.execute(llm_response_json)
    """

    def __init__(
        self,
        registry: ToolRegistry = None,
        output_dir: str = "",
        context: Optional[ToolExecutionContext] = None,
    ):
        self.registry = registry or ToolRegistry.get_instance()
        self.output_dir = ToolRegistry._normalize_output_dir(output_dir or "outputs")
        self.context = context

    async def execute_named(
        self,
        tool_name: str,
        tool_args: Optional[dict] = None,
        allowed_tools: List[str] = None,
        namespaces: List[str] = None,
        bypass_permission: bool = False,
    ) -> Optional[str]:
        """按工具名直接执行（供 phase_entry 等确定性触发使用）"""
        thought = {
            "needs_tool": True,
            "tool_name": tool_name,
            "tool_args": tool_args or {},
        }
        return await self.execute(
            thought,
            allowed_tools=allowed_tools,
            namespaces=namespaces,
            bypass_permission=bypass_permission,
        )

    async def execute(
        self,
        thought: dict,
        allowed_tools: List[str] = None,
        namespaces: List[str] = None,
        bypass_permission: bool = False,
    ) -> Optional[str]:
        """从 Agent 思维 JSON 中解析并执行 tool call"""
        if not thought.get("needs_tool"):
            return None

        tool_name = thought.get("tool_name", "")
        tool_args = thought.get("tool_args", {}) or {}
        if isinstance(tool_args, dict):
            tool_args = dict(tool_args)
            if "file_path" in tool_args and "path" not in tool_args:
                tool_args["path"] = tool_args.pop("file_path")

        if not tool_name:
            return None

        if allowed_tools and tool_name not in allowed_tools:
            bare = tool_name.split("/")[-1]
            allowed_set = set(allowed_tools)
            if tool_name not in allowed_set and bare not in allowed_set:
                return f"[错误] 工具 {tool_name} 不在 Agent 白名单内"

        tool = self.registry.resolve(tool_name, namespaces=namespaces)
        if not tool:
            available = allowed_tools or self.registry.list_names()
            return f"[错误] 未知工具: {tool_name}，可用工具: {', '.join(available)}"

        tool_call_id = str(uuid.uuid4())
        perm = PermissionGate.check(tool, tool_name)
        schema_err = validate_tool_args(tool.parameters, tool_args)
        if schema_err:
            self._audit(tool_call_id, tool_name, tool_args, "schema_error", error=schema_err)
            required = ", ".join(tool.parameters.get("required") or [])
            usage = f"（工具 {tool_name} 必填参数: {required}，请补全后重试）" if required else ""
            return f"[错误] {schema_err}{usage}"

        if not bypass_permission and not perm.allowed:
            self._audit(
                tool_call_id, tool_name, tool_args, "blocked",
                permission_mode=perm.mode.value, reason=perm.reason,
            )
            if perm.mode == PermissionMode.ASK:
                self._emit_approval_required(tool_call_id, tool_name, tool_args, perm.reason)
                self._register_pending_approval(
                    tool_call_id, tool_name, tool_args, perm.reason,
                    allowed_tools, namespaces,
                )
            return f"[{perm.mode.value}] {perm.reason}"

        if "path" in tool_args and not Path(tool_args["path"]).is_absolute():
            tool_args = dict(tool_args)
            tool_args["path"] = ToolRegistry.resolve_tool_path(self.output_dir, tool_args["path"])

        bare_tool = tool_name.split("/")[-1]
        write_path: Optional[str] = tool_args.get("path") if bare_tool == "file_write" else None

        # 将项目子目录传入 file_read/file_write，收紧越权边界到 outputs/<project>/
        if bare_tool in ("file_read", "file_write") and self.output_dir:
            tool_args = dict(tool_args)
            # 修复双前缀：上方 resolve_tool_path 已把 path 拼成相对 outputs/ 的 "<proj>/<file>"，
            # 这里剥回项目相对路径（"<file>"），交由 _project_subdir 在 _validate_path 定界，
            # 避免 outputs/<proj>/<proj>/<file> 双层嵌套。write_path 保持 "<proj>/<file>" 供产物登记。
            od = self.output_dir.replace("\\", "/").strip("/")
            p = str(tool_args.get("path") or "")
            if od and (p == od or p.startswith(od + "/")):
                tool_args["path"] = p[len(od):].lstrip("/")
            tool_args["_project_subdir"] = self.output_dir

        # shell_exec：把执行目录约束到项目输出目录（沙箱 cwd）
        if bare_tool == "shell_exec":
            tool_args = dict(tool_args)
            base = ToolRegistry.OUTPUTS_DIR
            tool_args["_workdir"] = str(base / self.output_dir) if self.output_dir else str(base)

        # 注入当前 Agent ID，供 generate_image/generate_video/generate_music 等工具
        # 查询 Agent 的 model_profile 配置
        if self.context and self.context.agent_id:
            tool_args = dict(tool_args)
            tool_args["_agent_id"] = self.context.agent_id

        # 合并 per-Agent 工具预设默认值（tool_configs 中的参数作为默认值，显式传入的优先）
        if self.context and self.context.agent_id:
            try:
                import json as _json
                from core.agent.registry import get_registry
                agent_def = await get_registry().get(self.context.agent_id)
                if agent_def and getattr(agent_def, 'tool_configs', None):
                    tc = _json.loads(agent_def.tool_configs) if isinstance(agent_def.tool_configs, str) else agent_def.tool_configs
                    presets = tc.get(tool_name, {}) if isinstance(tc, dict) else {}
                    if presets:
                        tool_args = dict(tool_args)
                        for k, v in presets.items():
                            if k not in tool_args:
                                tool_args[k] = v
            except Exception:
                pass  # 预设加载失败不阻塞工具执行

        timeout = PermissionGate.effective_timeout_seconds(tool)
        self._audit(tool_call_id, tool_name, tool_args, "started", timeout_seconds=timeout)
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._run_handler(tool, tool_args),
                timeout=timeout,
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self._audit(
                tool_call_id, tool_name, tool_args, "completed",
                elapsed_ms=elapsed_ms, result_preview=str(result)[:200],
            )
            if (
                bare_tool == "file_write"
                and write_path
                and self.context
                and self.context.project_id
                and not str(result).startswith("[错误]")
            ):
                try:
                    from core.vault.artifact_registry import register_file_artifact

                    resolved = ToolRegistry._validate_path(write_path)
                    await register_file_artifact(
                        project_id=self.context.project_id,
                        agent_id=self.context.agent_id,
                        resolved_path=resolved,
                        name=resolved.name,
                        metadata={"phase_id": tool_args.get("_phase_id", "")},
                        run_id=getattr(self.context, "run_id", "") or "",
                    )
                except Exception as e:
                    logger.debug("file_write 产物登记跳过: %s", e)
            return str(result)
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            err = f"工具 {tool_name} 执行超时（{timeout}s）"
            self._audit(tool_call_id, tool_name, tool_args, "timeout", elapsed_ms=elapsed_ms, error=err)
            return f"[错误] {err}"
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self._audit(tool_call_id, tool_name, tool_args, "failed", elapsed_ms=elapsed_ms, error=str(e))
            return f"[错误] 工具 {tool_name} 执行失败: {e}"

    async def _run_handler(self, tool: ToolDefinition, tool_args: dict) -> Any:
        """执行工具 handler —— 兼容两种签名约定。

        - 旧签名 handler(args, context)：文本工具协议构造的 tool_args 是
          {args: {...}, context: {...}}，展开即匹配；
        - 新签名 handler(**kwargs)：LLM 原生 function calling 传入裸参数
          （如 {query: ..., top_k: ...}）。
        修复前 _run_handler 恒用 **tool_args 展开，function calling 路径触发
        4 个 (args, context) handler（_knowledge_query/_asset_query/_llm_call/_read_url）
        时抛 "unexpected keyword argument"。
        """
        import inspect

        handler = tool.handler
        try:
            params = list(inspect.signature(handler).parameters.keys())
        except (ValueError, TypeError):
            params = []

        if "args" in params or "context" in params:
            # 旧签名：直接传 (args, context)
            if asyncio.iscoroutinefunction(handler):
                return await handler(tool_args, getattr(self, "context", None))
            return handler(tool_args, getattr(self, "context", None))

        if asyncio.iscoroutinefunction(handler):
            return await handler(**tool_args)
        return handler(**tool_args)

    def _audit(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict,
        status: str,
        **extra,
    ) -> None:
        if self.context:
            self.context.audit("tool_audit", {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "status": status,
                **extra,
            })

    def _emit_approval_required(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict,
        reason: str,
    ) -> None:
        if self.context:
            self.context.audit("tool_approval_required", {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "reason": reason,
            })

    def _register_pending_approval(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict,
        reason: str,
        allowed_tools: Optional[List[str]],
        namespaces: Optional[List[str]],
    ) -> None:
        if not self.context or not self.context.project_id:
            return
        from core.gateway.pending_tools import register_pending

        register_pending(
            tool_call_id,
            project_id=self.context.project_id,
            agent_id=self.context.agent_id,
            tool_name=tool_name,
            tool_args=tool_args,
            namespaces=namespaces,
            allowed_tools=allowed_tools,
            output_dir=self.output_dir,
            reason=reason,
        )

    @staticmethod
    def parse_tool_call_from_text(text: str) -> Optional[dict]:
        """从自然语言文本中解析 TOOL: 语法

        支持格式：
            TOOL: file_read(path="script.txt")
            TOOL: web_search(query="xxx")

        Returns:
            {"tool_name": "...", "tool_args": {...}} 或 None
        """
        match = re.search(r'TOOL:\s*(\w+)\((.*?)\)', text, re.DOTALL)
        if not match:
            return None

        tool_name = match.group(1)
        args_str = match.group(2)

        tool_args = {}
        # 解析 key=value 或 key="value"
        for arg_match in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', args_str):
            tool_args[arg_match.group(1)] = arg_match.group(2)
        for arg_match in re.finditer(r'(\w+)\s*=\s*([^,\s)]+)', args_str):
            if arg_match.group(1) not in tool_args:
                val = arg_match.group(2)
                try:
                    val = int(val)
                except ValueError:
                    pass
                tool_args[arg_match.group(1)] = val

        return {"tool_name": tool_name, "tool_args": tool_args}
