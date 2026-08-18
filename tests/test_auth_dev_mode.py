"""P1-6 修复测试：dev 模式（双认证开关关闭）中间件注入匿名身份。

修复前：ApiKeyMiddleware 双关关闭时直接 call_next、不设 request.state.user，
而 knowledge/quality 写接口的 _require_user 无条件抛 401 → dev 模式 CRUD 全挂。
修复后：注入匿名 admin，dev 模式写操作可用。
"""
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from core.auth import ApiKeyMiddleware


def _make_client(monkeypatch):
    """双关关闭环境下的最小 ASGI app + ApiKeyMiddleware。"""
    monkeypatch.setattr("core.auth.API_KEY_ENABLED", False)
    monkeypatch.setattr("core.auth.AUTH_ENABLED", False)

    async def whoami(request: Request):
        user = getattr(request.state, "user", None)
        return JSONResponse({"user": user})

    app = Starlette(routes=[Route("/whoami", whoami, methods=["GET"])])
    app.add_middleware(ApiKeyMiddleware)
    return TestClient(app)


def test_dev_mode_sets_anonymous_user(monkeypatch):
    """dev 模式注入匿名 admin，使 _require_user 类写接口不再 401。"""
    client = _make_client(monkeypatch)
    resp = client.get("/whoami")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["sub"] == "anonymous"
    assert body["user"]["role"] == "operator"
