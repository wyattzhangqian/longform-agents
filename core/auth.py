"""认证中间件 — API Key（机器）+ JWT 用户会话（P0 加固）

支持三种凭证：
1. Header:   Authorization: Bearer <api_key 或 JWT>
2. Header:   X-API-Key: <token>
3. Query:    ?api_key=<token>（仅 SSE）

AUTH_ENABLED=true 时所有非白名单请求必须携带有效 API Key 或 JWT；
JWT 用户附带角色：viewer 仅允许只读（GET/HEAD/OPTIONS）。
白名单路径自动跳过认证。
"""

from __future__ import annotations
import os
import secrets
from typing import Optional, Set
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from config import API_KEY, API_KEY_ENABLED, AUTH_ENABLED, PROJECT_ACCESS_ENABLED


# ============================================================
# 白名单路径（不校验认证）
# ============================================================

_WHITELIST: Set[str] = {
    "/",
    "/health",
    "/metrics",
    "/api/health",
    "/api/auth/login",
    "/api/auth/status",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
}

_WHITELIST_PREFIXES: tuple = (
    "/api/events",    # SSE 流通过 query param 传 key
)


# ============================================================
# 安全校验
# ============================================================

def _verify_token(token: str) -> bool:
    """常量时间比较，防时序攻击。未配置 API_KEY 时拒绝（fail-closed）。"""
    if not API_KEY:
        return False
    return secrets.compare_digest(token, API_KEY)


def _extract_token(request: Request) -> Optional[str]:
    """从请求中提取 API Key"""
    # 1. Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    # 2. X-API-Key header
    api_key = request.headers.get("X-API-Key", "")
    if api_key:
        return api_key.strip()

    # 3. Query parameter (for SSE)
    if request.url.path.startswith("/api/events"):
        return request.query_params.get("api_key", "")

    return None


# ============================================================
# 中间件
# ============================================================

_READONLY_METHODS = ("GET", "HEAD", "OPTIONS")


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"success": False, "error": "unauthorized", "detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """认证中间件：API Key（机器调用）+ JWT 用户会话

    用法（在 main.py 中）:
        app.add_middleware(ApiKeyMiddleware)
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        # 两套开关都关闭 → 跳过
        if not API_KEY_ENABLED and not AUTH_ENABLED:
            # P1-6: dev 模式（双开关关闭）也注入匿名身份，否则 knowledge/quality
            # 写接口的 _require_user 一律 401，dev 模式 CRUD 全挂（开箱即用失效）。
            #
            # ⚠️ 安全边界（已评审）：此分支是显式"无鉴权"开发模式（AUTH_ENABLED=false
            # 的本义即不做认证），注入后 dev 环境下写操作对网络开放。为降低风险：
            #   - 不声明 admin，用 operator（_require_user 仅需非空 sub，写操作不受影响）
            #   - 仅限开发环境；生产必须启用 AUTH_ENABLED，且 uvicorn 仅绑定 127.0.0.1
            request.state.user = {"sub": "anonymous", "username": "anonymous", "role": "operator"}
            return await call_next(request)

        # 白名单 → 跳过
        path = request.url.path
        if path in _WHITELIST or path.startswith(_WHITELIST_PREFIXES):
            return await call_next(request)

        token = _extract_token(request)
        if not token:
            return _unauthorized(
                "缺失凭证。请在请求头添加 Authorization: Bearer <JWT 或 API Key>",
            )

        # 1) API Key（机器调用，等同 admin）
        if API_KEY and secrets.compare_digest(token, API_KEY):
            request.state.user = {"sub": "api_key", "username": "api_key", "role": "admin"}
            return await call_next(request)

        # 2) JWT 用户会话
        if AUTH_ENABLED:
            from core.security.users import verify_token as verify_jwt
            payload = verify_jwt(token)
            if payload:
                request.state.user = payload
                if (
                    payload.get("role") == "viewer"
                    and request.method.upper() not in _READONLY_METHODS
                ):
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={
                            "success": False,
                            "error": "forbidden",
                            "detail": "viewer 角色仅有只读权限",
                        },
                    )
                return await call_next(request)

        # 3) 仅启用 API Key 却未配置 API_KEY → 拒绝（fail-closed，勿静默放行）
        if API_KEY_ENABLED and not AUTH_ENABLED and not API_KEY:
            return _unauthorized("API_KEY_ENABLED=true 但未配置 API_KEY")

        return _unauthorized("无效或过期的凭证")


# ============================================================
# FastAPI 依赖注入方式（用于特定路由）
# ============================================================

async def verify_api_key(request: Request):
    """依赖注入式认证（用于单个路由）"""
    if not API_KEY_ENABLED:
        return True
    token = _extract_token(request)
    if not token or not _verify_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或缺失 API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


# ============================================================
# 项目级 access_token 鉴权
# ============================================================

def generate_project_access_token() -> str:
    return secrets.token_urlsafe(24)


async def require_project_access(project_id: str, request: Request) -> None:
    """校验调用方是否有权访问指定项目"""
    if not PROJECT_ACCESS_ENABLED:
        return

    if API_KEY_ENABLED:
        token = _extract_token(request)
        if token and _verify_token(token):
            return

    header_token = request.headers.get("X-Project-Token", "").strip()
    if not header_token:
        header_token = request.query_params.get("project_token", "").strip()
    if not header_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="缺少项目访问令牌。请在请求头添加 X-Project-Token",
        )

    from core.project.manager import ProjectManager

    stored = await ProjectManager.get_access_token(project_id)
    if not stored or not secrets.compare_digest(header_token, stored):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无效的项目访问令牌",
        )
