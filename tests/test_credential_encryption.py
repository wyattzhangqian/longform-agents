"""凭证 Fernet 加密 roundtrip + legacy 兼容"""
import base64
import pytest
from core.security.secret_box import encrypt_secret, decrypt_secret


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-fernet-unit")


def test_fernet_roundtrip():
    token = encrypt_secret("sk-live-abc")
    assert token.startswith("fernet:")
    assert decrypt_secret(token) == "sk-live-abc"
    assert "sk-live-abc" not in token


def test_legacy_base64_still_decrypts():
    secret = "test-secret-for-fernet-unit"
    legacy = base64.b64encode(f"{secret}:old-key".encode()).decode()
    assert decrypt_secret(legacy) == "old-key"
