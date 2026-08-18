"""用户体系 — bcrypt 口令 + JWT 会话 + 角色（P0 认证加固）

角色模型（最小可用 RBAC）：
  - admin    全部权限（含用户管理）
  - operator 创建 / 运行 / 修改业务资源
  - viewer   只读（仅 GET）

JWT 密钥优先级：AUTH_SECRET 环境变量 > data/.auth_secret（首次启动自动生成并持久化）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any, Dict, Optional

from config import (
    AUTH_ADMIN_PASSWORD,
    AUTH_ADMIN_USERNAME,
    AUTH_ENABLED,
    AUTH_SECRET,
    AUTH_TOKEN_TTL_HOURS,
    DATA_DIR,
)

_logger = __import__("logging").getLogger(__name__)

ROLES = ("admin", "operator", "viewer")

try:
    import bcrypt as _bcrypt
except ImportError:  # pragma: no cover
    _bcrypt = None


# ============================================================
# 密钥管理
# ============================================================

_SECRET_FILE = DATA_DIR / ".auth_secret"
_cached_secret: Optional[bytes] = None


def get_auth_secret() -> bytes:
    """JWT 签名密钥：env 优先，否则生成并持久化到 data/.auth_secret"""
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    if AUTH_SECRET:
        _cached_secret = AUTH_SECRET.encode("utf-8")
        return _cached_secret
    if _SECRET_FILE.exists():
        _cached_secret = _SECRET_FILE.read_text(encoding="utf-8").strip().encode("utf-8")
        return _cached_secret
    secret = secrets.token_urlsafe(48)
    _SECRET_FILE.write_text(secret, encoding="utf-8")
    try:
        _SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    _cached_secret = secret.encode("utf-8")
    _logger.info("已生成并持久化 JWT 签名密钥: %s", _SECRET_FILE)
    return _cached_secret


# ============================================================
# 口令哈希
# ============================================================

def hash_password(password: str) -> str:
    if _bcrypt is not None:
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    # bcrypt 不可用时退化为加盐 PBKDF2（仍然安全，但建议安装 bcrypt）
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        if password_hash.startswith("pbkdf2$"):
            _, salt, digest = password_hash.split("$", 2)
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000,
            ).hex()
            return hmac.compare_digest(candidate, digest)
        if _bcrypt is not None:
            return _bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
    return False


# ============================================================
# JWT（HS256，零外部依赖实现）
# ============================================================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user: Dict[str, Any], ttl_hours: int = 0) -> str:
    """签发 JWT：{sub, username, role, iat, exp}"""
    ttl = (ttl_hours or AUTH_TOKEN_TTL_HOURS) * 3600
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user.get("role", "operator"),
        "iat": now,
        "exp": now + ttl,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    signature = hmac.new(get_auth_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """校验 JWT，返回 payload；无效 / 过期返回 None"""
    try:
        signing_input, _, sig_part = token.rpartition(".")
        if not signing_input:
            return None
        expected = hmac.new(get_auth_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), sig_part):
            return None
        payload = json.loads(_b64url_decode(signing_input.split(".", 1)[1]))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ============================================================
# 用户 CRUD（users 表）
# ============================================================

def _row_to_user(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


async def create_user(username: str, password: str, role: str = "operator") -> Dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"非法角色: {role}（可选: {', '.join(ROLES)}）")
    if not username or len(password) < 8:
        raise ValueError("用户名不能为空，密码至少 8 位")
    from core.storage.database import get_db
    db = await get_db()
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, username, hash_password(password), role),
    )
    await db.commit()
    return {"id": user_id, "username": username, "role": role, "status": "active"}


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = await cursor.fetchone()
    if not row:
        return None
    user = _row_to_user(row)
    user["password_hash"] = row["password_hash"]
    return user


async def list_users() -> list:
    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute("SELECT * FROM users ORDER BY created_at")
    rows = await cursor.fetchall()
    return [_row_to_user(r) for r in rows]


async def count_users() -> int:
    from core.storage.database import get_db
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS cnt FROM users")
    row = await cursor.fetchone()
    return int(row["cnt"])


async def verify_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验登录；成功返回用户（不含 password_hash）并刷新 last_login_at"""
    user = await get_user_by_username(username)
    if not user or user.get("status") != "active":
        return None
    if not verify_password(password, user.pop("password_hash", "")):
        return None
    from core.storage.database import get_db
    db = await get_db()
    await db.execute(
        "UPDATE users SET last_login_at = datetime('now', 'localtime') WHERE id = ?",
        (user["id"],),
    )
    await db.commit()
    return user


async def change_password(user_id: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError("密码至少 8 位")
    from core.storage.database import get_db
    db = await get_db()
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    await db.commit()


async def bootstrap_admin() -> Optional[str]:
    """首次启动创建管理员（仅当 users 表为空）。

    - AUTH_ADMIN_PASSWORD 已配置 → 用它
    - 未配置但 AUTH_ENABLED=true → 生成随机密码并打印一次（仅日志）
    - AUTH_ENABLED=false 且未配置密码 → 跳过
    """
    if await count_users() > 0:
        return None
    password = AUTH_ADMIN_PASSWORD
    generated = False
    if not password:
        if not AUTH_ENABLED:
            return None
        password = secrets.token_urlsafe(12)
        generated = True
    await create_user(AUTH_ADMIN_USERNAME, password, role="admin")
    if generated:
        _logger.warning(
            "已生成初始管理员密码（仅显示一次，请立即修改）: 用户名=%s 密码=%s",
            AUTH_ADMIN_USERNAME, password,
        )
    return AUTH_ADMIN_USERNAME
