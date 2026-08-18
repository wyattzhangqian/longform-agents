"""子进程沙箱 — shell_exec 工具的隔离执行层（P1 工具执行隔离）

隔离手段（纵深防御，人工审批是最后闸门）：
  1. 权限门禁：shell_exec 注册为 risk_level=high → PermissionGate 强制人工审批
  2. 工作目录约束：cwd 固定在 outputs/<project>/ 下
  3. 环境净化：仅保留最小 PATH，不继承进程环境（API Key 等不泄漏给子进程）
  4. 资源限制（POSIX）：CPU 时间 / 内存 / 文件大小 / 文件句柄
  5. 进程组隔离：超时杀整个进程组，防止子进程逃逸
  6. 命令黑名单：明显破坏性命令直接拒绝
  7. 输出截断：最多回传 16KB
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import sys
from pathlib import Path
from typing import Optional

_logger = __import__("logging").getLogger(__name__)

MAX_OUTPUT_CHARS = 16_000
DEFAULT_TIMEOUT_SECONDS = 60

# 明显破坏性 / 提权命令（防呆，不是完整安全边界——完整边界靠人工审批 + 容器化部署）
_DENY_PATTERNS = [
    r"\bsudo\b",
    r"\bsu\s",
    r"\brm\s+(-[a-zA-Z]*\s+)*/(\s|$)",   # rm ... /
    r"\bmkfs\b",
    r"\bshutdown\b|\breboot\b",
    r">\s*/dev/(sd|disk|nvme)",
    r"\bdd\s+.*of=/dev/",
    r"\bchown\s+.*\s/(\s|$)",
    r":\(\)\s*\{.*\};\s*:",               # fork bomb
    r"\bcurl\b.*\|\s*(ba)?sh",            # 管道执行远程脚本
    r"\bwget\b.*\|\s*(ba)?sh",
]
_DENY_RE = re.compile("|".join(_DENY_PATTERNS), re.IGNORECASE)

# 路径越权模式：拦截 ../（含编码变体）和绝对路径访问宿主机敏感目录
_PATH_TRAVERSAL_RE = re.compile(
    r"(?:^|\s|['\"(;|&])"           # 命令前缀
    r"(?:\.\./|\.\\)"              # ../ 或 ..\
    r"|"                           # 或
    r"(?:%2e%2e%2f|%2e%2e/|\.\.%2f)" # URL 编码变体
    r"|"                           # 或绝对路径读宿主机
    r"(?:/etc/(?:passwd|shadow|hosts|sudoers))"  # 直接读宿主机敏感文件
    r"|"                           # 或 /proc/self/environ 泄漏环境变量
    r"(?:/proc/self/environ|/proc/\d+/environ)"
    r"|"                           # 或读取 ~/.ssh, ~/.aws 等
    r"(?:~/.ssh/|~/.aws/|~/.config/)",
    re.IGNORECASE,
)


def check_command_denied(command: str) -> Optional[str]:
    """返回拒绝原因；None = 通过"""
    if not command or not command.strip():
        return "命令为空"
    m = _DENY_RE.search(command)
    if m:
        return f"命令包含禁止模式: {m.group(0)!r}"
    # 路径越权检测
    pt = _PATH_TRAVERSAL_RE.search(command)
    if pt:
        return f"命令包含路径越权模式: {pt.group(0)!r}"
    return None


def _posix_limits() -> None:
    """子进程资源限制（仅 POSIX；preexec_fn 中执行）"""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))                       # CPU 30s
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024,) * 2)      # 单文件 50MB
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    except ValueError:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)   # 内存 1GB
    except (ValueError, OSError):
        pass  # macOS 不支持 RLIMIT_AS 收紧


async def run_sandboxed_shell(
    command: str,
    workdir: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """在沙箱中执行 shell 命令，返回组合输出（stdout + stderr）"""
    deny = check_command_denied(command)
    if deny:
        return f"[错误] 命令被沙箱拒绝: {deny}"

    cwd = Path(workdir).resolve()
    cwd.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": str(cwd),
    }

    exec_command = command
    if os.getenv("SANDBOX_NO_NETWORK", "false").lower() == "true" and sys.platform == "linux":
        import shutil
        if shutil.which("unshare"):
            exec_command = f"unshare -n sh -c {shlex.quote(command)}"

    kwargs: dict = {
        "cwd": str(cwd),
        "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
        "start_new_session": True,
        "stdin": asyncio.subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        kwargs["preexec_fn"] = _posix_limits

    try:
        proc = await asyncio.create_subprocess_shell(exec_command, **kwargs)
    except Exception as e:
        return f"[错误] 沙箱启动失败: {e}"

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        return f"[错误] 命令执行超时（{timeout}s），进程组已终止"

    output = (stdout or b"").decode("utf-8", errors="replace")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n... (输出截断，共 {len(output)} 字符)"

    if proc.returncode != 0:
        return f"[退出码 {proc.returncode}]\n{output}"
    return output or "(无输出)"
