"""APM / 错误追踪接入（P1）

Sentry 集成，完全由环境变量门控：
  - SENTRY_DSN 未设置 → 不初始化，零开销
  - sentry-sdk 未安装  → 记录提示后跳过（不在默认依赖中，按需安装）

  SENTRY_DSN=https://xxx@sentry.io/123
  SENTRY_ENVIRONMENT=production
  SENTRY_TRACES_SAMPLE_RATE=0.1   # 性能追踪采样率，默认 0（仅错误）
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def init_apm() -> bool:
    """初始化 Sentry（幂等）。返回是否已启用。"""
    global _initialized
    if _initialized:
        return True

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        logger.warning("SENTRY_DSN 已配置但 sentry-sdk 未安装：pip install sentry-sdk[fastapi]")
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
            send_default_pii=False,
            integrations=[StarletteIntegration(), FastApiIntegration()],
        )
        _initialized = True
        logger.info("✅ Sentry APM 已启用 (env=%s)", os.getenv("SENTRY_ENVIRONMENT", "production"))
        return True
    except Exception as e:
        logger.warning("Sentry 初始化失败（继续运行，不阻塞）: %s", e)
        return False


def capture_exception(exc: BaseException, **context) -> None:
    """手动上报异常（未启用时为 no-op）"""
    if not _initialized:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in context.items():
                scope.set_tag(k, str(v)[:200])
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass
