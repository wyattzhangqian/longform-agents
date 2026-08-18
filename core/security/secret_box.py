"""凭证加解密 — Fernet（真加密）+ 兼容旧 base64(secret:key)

密文前缀 fernet:；无前缀视为 legacy base64 格式。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

_LEGACY_PREFIX_SEP = ":"
FERNET_PREFIX = "fernet:"
_DEFAULT_SECRET = "agent-platform-secret"
_logger = logging.getLogger(__name__)
_warned_default_secret = False


def _resolve_secret() -> Tuple[str, bool]:
    """返回 (secret, is_default)。未配置 SECRET_KEY/AUTH_SECRET 时用固定 fallback。"""
    secret = os.getenv("SECRET_KEY") or os.getenv("AUTH_SECRET")
    if secret:
        return secret, False
    return _DEFAULT_SECRET, True


def warn_if_default_secret() -> None:
    """启动时调用：默认密钥仅开发可用，上线须配置 SECRET_KEY。"""
    global _warned_default_secret
    _, is_default = _resolve_secret()
    if is_default and not _warned_default_secret:
        _warned_default_secret = True
        _logger.warning(
            "SECRET_KEY/AUTH_SECRET 未配置，凭证加密使用默认密钥 "
            "（仅开发可用；生产务必设置 SECRET_KEY）"
        )


def _fernet() -> Fernet:
    secret, is_default = _resolve_secret()
    if is_default:
        warn_if_default_secret()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return FERNET_PREFIX + token


def decrypt_secret(stored: str) -> Optional[str]:
    if not stored:
        return None
    if stored.startswith(FERNET_PREFIX):
        try:
            return (
                _fernet()
                .decrypt(stored[len(FERNET_PREFIX) :].encode("ascii"))
                .decode("utf-8")
            )
        except InvalidToken:
            return None
    # legacy: base64(secret:plaintext)
    try:
        decoded = base64.b64decode(stored).decode("utf-8")
        secret, _ = _resolve_secret()
        prefix = f"{secret}{_LEGACY_PREFIX_SEP}"
        if decoded.startswith(prefix):
            return decoded[len(prefix) :]
        return decoded
    except Exception:
        return None
