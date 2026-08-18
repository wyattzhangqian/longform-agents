"""OpenTelemetry 分布式追踪（M2 可选）

完全由环境变量门控：
  OTEL_ENABLED=true
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
  OTEL_SERVICE_NAME=agent-platform

未安装 opentelemetry-sdk 时 fail-open，不阻塞启动。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def init_otel() -> bool:
    """初始化 OTel TracerProvider（幂等）"""
    global _initialized
    if _initialized:
        return True

    if os.getenv("OTEL_ENABLED", "false").lower() != "true":
        return False

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.warning("OTEL_ENABLED=true 但未设置 OTEL_EXPORTER_OTLP_ENDPOINT")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL 已启用但 opentelemetry 未安装："
            "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
        )
        return False

    try:
        service = os.getenv("OTEL_SERVICE_NAME", "agent-platform")
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _initialized = True
        logger.info("✅ OpenTelemetry 已启用 service=%s endpoint=%s", service, endpoint)
        return True
    except Exception as e:
        logger.warning("OpenTelemetry 初始化失败（继续运行）: %s", e)
        return False


def otel_status() -> dict:
    return {
        "enabled": os.getenv("OTEL_ENABLED", "false").lower() == "true",
        "initialized": _initialized,
        "service": os.getenv("OTEL_SERVICE_NAME", "agent-platform"),
    }
