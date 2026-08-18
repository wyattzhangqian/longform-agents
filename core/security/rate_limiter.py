"""RateLimiter — 令牌桶限流中间件

按 IP（匿名）或 user_id（已登录）限流。
配置通过环境变量：RATE_LIMIT_RPM（默认 60 次/分钟）。
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Dict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# 默认限流：300 请求/分钟（SPA 页面加载并发 10+ 请求，需足够 burst 容量）
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "300"))
# 写操作加严：60 次/分钟
WRITE_RATE_LIMIT_RPM = int(os.getenv("WRITE_RATE_LIMIT_RPM", "60"))
# 白名单路径（不限流）— 含 SSE 推送和前端轮询端点
EXEMPT_PATHS = {
    "/api/health", "/api/events", "/metrics", "/api/config",
    "/api/healthz", "/api/ready",
}
EXEMPT_PREFIXES = (
    "/api/projects/",  # 工作台所有子路径（collaboration/outputs/events/handoffs/negotiation/pending-tools）
    "/api/studio/agent-preview",  # SSE 流式预览
    "/api/plan/stream",  # SSE 流式规划
)


class TokenBucket:
    """简单令牌桶"""
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_rate  # tokens/second
        self.last_refill = time.monotonic()

    def consume(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, rpm: int = RATE_LIMIT_RPM, write_rpm: int = WRITE_RATE_LIMIT_RPM):
        super().__init__(app)
        self.rpm = rpm
        self.write_rpm = write_rpm
        self._buckets: Dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(capacity=rpm, refill_rate=rpm / 60.0))
        self._write_buckets: Dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(capacity=write_rpm, refill_rate=write_rpm / 60.0))

    def _get_key(self, request: Request) -> str:
        """按 user_id > API-Key > IP 限流"""
        user = getattr(request.state, "user", None)
        if user and isinstance(user, dict):
            return f"user:{user.get('id', '')}"
        api_key = request.headers.get("X-API-Key", "")
        if api_key:
            return f"key:{api_key[:8]}"
        ip = request.client.host if request.client else "unknown"
        return f"ip:{ip}"

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith("/static") or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        key = self._get_key(request)

        # 写操作（POST/PUT/DELETE）用更严格的桶
        if request.method in ("POST", "PUT", "DELETE"):
            bucket = self._write_buckets[key]
        else:
            bucket = self._buckets[key]

        if not bucket.consume():
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down.", "retry_after_seconds": 60 // self.rpm + 1},
                headers={"Retry-After": str(60 // self.rpm + 1)},
            )

        return await call_next(request)
