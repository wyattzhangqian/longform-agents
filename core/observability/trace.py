"""请求级 trace_id — 串联 workflow_events 与 SSE"""

from __future__ import annotations
import uuid
from contextvars import ContextVar
from typing import Optional

_trace_id: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


def get_trace_id() -> Optional[str]:
    return _trace_id.get()
