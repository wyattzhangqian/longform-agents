"""结构化日志 — 基于 loguru

替代所有 print() 调用，提供：
- 结构化字段（bind/contextualize）
- 自动注入模块名/函数名/行号
- JSON 格式（生产） / 彩色终端（开发）
- 日志轮转 + 保留策略
"""

from __future__ import annotations
import logging
import sys
import os
from pathlib import Path
from loguru import logger


# ============================================================
# 配置
# ============================================================

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FORMAT_DEV = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[module]:<20}</cyan> | "
    "<level>{message}</level>"
)

LOG_FORMAT_PROD = (
    '{{"time":"{time:YYYY-MM-DDTHH:mm:ss.SSSZ}","level":"{level}",'
    '"module":"{extra[module]}","function":"{function}","line":{line},'
    '"message":"{message}","extra":{extra[data]}}}'
)

# 是否 JSON 输出（生产环境）
_JSON_MODE = os.getenv("LOG_FORMAT", "dev") == "json"


class InterceptHandler(logging.Handler):
    """把标准 logging 转发到 loguru（统一输出到文件/终端）。

    平台日志系统是 loguru，但部分模块（如 tools.llm_client）仍用标准
    logging.getLogger —— 若无拦截，这些日志没有任何 handler，直接丢失
    （排障时"看不到 LLM 日志"的元凶之一，2026-08-08 定位）。
    """

    def emit(self, record: "logging.LogRecord"):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # getMessage() 按 %s 格式化；代码库部分模块用 loguru 的 {} 语法但走
        # 标准 logging → % 格式化抛 "not all arguments converted"。
        # 此时保留 msg + args，交给 loguru 用 {} 格式化（不丢参数）。
        try:
            msg = record.getMessage()
            _args: tuple = ()
        except Exception:
            msg = str(record.msg)
            _args = tuple(record.args or ())
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        # bind module：格式模板用 {extra[module]}，不 bind 会 KeyError
        module_short = (record.name or "root").split(".")[-1] or "root"
        _lg = logger.bind(module=module_short, data={})
        if _args:
            _lg.opt(depth=depth, exception=record.exc_info).log(level, msg, *_args)
        else:
            _lg.opt(depth=depth, exception=record.exc_info).log(level, msg)


def _intercept_stdlib_logging():
    """根 logger 挂 InterceptHandler，标准 logging 全部转发到 loguru。"""
    logging.basicConfig(handlers=[InterceptHandler()], level=0)
    for _name in ("aiohttp", "aiosqlite", "uvicorn", "asyncio"):
        _lg = logging.getLogger(_name)
        _lg.handlers = [InterceptHandler()]
        _lg.setLevel(logging.WARNING)  # 第三方只转发 WARNING+，避免 DEBUG 噪音
        _lg.propagate = False


def setup_logging():
    """初始化 loguru 日志系统"""
    # 移除默认 handler
    logger.remove()
    # 标准 logging → loguru 转发（llm_client 等模块的日志才能进文件）
    _intercept_stdlib_logging()

    # 终端输出 — 开发模式彩色格式
    logger.add(
        sys.stderr,
        format=LOG_FORMAT_DEV,
        level=os.getenv("LOG_LEVEL", "DEBUG"),
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # 文件输出 — JSON 结构化
    logger.add(
        LOG_DIR / "app_{time:YYYY-MM-DD}.log",
        format=LOG_FORMAT_PROD if _JSON_MODE else LOG_FORMAT_DEV,
        level="INFO",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        backtrace=True,
        diagnose=False,
        serialize=_JSON_MODE,
    )

    # 错误日志单独文件
    logger.add(
        LOG_DIR / "error_{time:YYYY-MM-DD}.log",
        format=LOG_FORMAT_PROD if _JSON_MODE else LOG_FORMAT_DEV,
        level="ERROR",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        backtrace=True,
        diagnose=True,
        serialize=_JSON_MODE,
    )

    # 绑定全局模块名（子模块可通过 bind 覆盖）
    return logger.bind(module="main", data={})


# ============================================================
# 便捷工厂
# ============================================================

def get_logger(module_name: str, **extra):
    """获取带模块上下文的 logger"""
    module_short = module_name.split(".")[-1] if "." in module_name else module_name
    return logger.bind(module=module_short, data=extra or {})


# ============================================================
# 初始化
# ============================================================

_logger = setup_logging()

# 导出供其他模块直接使用
__all__ = ["logger", "get_logger", "setup_logging"]
