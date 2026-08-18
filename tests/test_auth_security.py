"""认证安全测试 — 口令哈希 / JWT / 用户 CRUD / 角色（P0 认证）"""

import time
import pytest

from core.security import users as user_store


class TestPasswordHashing:
    def test_hash_and_verify(self):
        h = user_store.hash_password("s3cret-password")
        assert h != "s3cret-password"
        assert user_store.verify_password("s3cret-password", h) is True

    def test_wrong_password_rejected(self):
        h = user_store.hash_password("correct-password")
        assert user_store.verify_password("wrong-password", h) is False

    def test_malformed_hash_rejected(self):
        assert user_store.verify_password("x", "not-a-hash") is False
        assert user_store.verify_password("x", "") is False


class TestJWT:
    USER = {"id": "user_abc", "username": "alice", "role": "operator"}

    def test_issue_and_verify(self):
        token = user_store.issue_token(self.USER)
        payload = user_store.verify_token(token)
        assert payload is not None
        assert payload["sub"] == "user_abc"
        assert payload["username"] == "alice"
        assert payload["role"] == "operator"

    def test_tampered_token_rejected(self):
        token = user_store.issue_token(self.USER)
        head, body, sig = token.split(".")
        assert user_store.verify_token(f"{head}.{body}.AAAA{sig[4:]}") is None

    def test_expired_token_rejected(self, monkeypatch):
        token = user_store.issue_token(self.USER)
        payload = user_store.verify_token(token)
        assert payload is not None
        # 把时间拨到过期之后
        monkeypatch.setattr(time, "time", lambda: payload["exp"] + 10)
        assert user_store.verify_token(token) is None

    def test_garbage_token_rejected(self):
        assert user_store.verify_token("") is None
        assert user_store.verify_token("abc.def") is None
        assert user_store.verify_token("not a token at all") is None


@pytest.mark.asyncio
class TestUserCrud:
    async def test_create_and_login(self):
        await user_store.create_user("crud_user", "password123", role="operator")
        user = await user_store.verify_login("crud_user", "password123")
        assert user is not None
        assert user["username"] == "crud_user"
        assert "password_hash" not in user

    async def test_login_wrong_password(self):
        await user_store.create_user("crud_user2", "password123")
        assert await user_store.verify_login("crud_user2", "wrong") is None

    async def test_login_unknown_user(self):
        assert await user_store.verify_login("no_such_user", "whatever") is None

    async def test_invalid_role_rejected(self):
        with pytest.raises(ValueError):
            await user_store.create_user("bad_role_user", "password123", role="superuser")

    async def test_short_password_rejected(self):
        with pytest.raises(ValueError):
            await user_store.create_user("weak_user", "short")

    async def test_change_password(self):
        user = await user_store.create_user("pwd_change_user", "oldpassword1")
        await user_store.change_password(user["id"], "newpassword1")
        assert await user_store.verify_login("pwd_change_user", "oldpassword1") is None
        assert await user_store.verify_login("pwd_change_user", "newpassword1") is not None
