"""Token 流缓冲器 — 攒 token 后批量推送控制频率

供 BaseAgent._llm_call 内部流式使用，避免逐 token 推送的事件风暴。
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable


class _TokenBuffer:
    """攒 token 到一定量或一定时间后批量 flush。

    用法：
        buffer = _TokenBuffer(flush_interval=0.1, max_size=30)
        async for chunk in llm_stream:
            await buffer.add(chunk, flush_fn)
        await buffer.flush(flush_fn)
    """

    def __init__(self, flush_interval: float = 0.1, max_size: int = 30):
        self.buffer = ""
        self.flush_interval = flush_interval
        self.max_size = max_size
        self._last_flush = time.time()

    async def add(self, token: str, flush_fn: Callable[[str], Awaitable[None]]) -> None:
        self.buffer += token
        now = time.time()
        if len(self.buffer) >= self.max_size or (now - self._last_flush) >= self.flush_interval:
            await self.flush(flush_fn)

    async def flush(self, flush_fn: Callable[[str], Awaitable[None]]) -> None:
        if self.buffer:
            await flush_fn(self.buffer)
            self.buffer = ""
            self._last_flush = time.time()
