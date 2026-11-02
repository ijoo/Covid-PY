"""Antrean kirim tiga prioritas, padanan putquick, putserv, dan puthelp."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

QUICK, SERV, HELP = range(3)
_MAX_PENDING = 500


class SendQueue:
    def __init__(self, burst: int, interval: float) -> None:
        self._queues = (deque(), deque(), deque())
        self._burst = burst
        self._interval = interval
        self._tokens = float(burst)
        self._refilled = time.monotonic()
        self._wake = asyncio.Event()

    def put(self, priority: int, line: str) -> None:
        queue = self._queues[priority]
        if len(queue) >= _MAX_PENDING:
            log.warning("antrean kirim penuh, baris dibuang: %.60s", line)
            return
        queue.append(line)
        self._wake.set()

    def clear(self) -> None:
        for queue in self._queues:
            queue.clear()

    def _pop(self) -> str | None:
        for queue in self._queues:
            if queue:
                return queue.popleft()
        return None

    async def _acquire(self) -> None:
        now = time.monotonic()
        refill = (now - self._refilled) / self._interval
        self._tokens = min(self._burst, self._tokens + refill)
        self._refilled = now
        if self._tokens < 1:
            await asyncio.sleep((1 - self._tokens) * self._interval)
            self._tokens = 1.0
            self._refilled = time.monotonic()
        self._tokens -= 1

    async def run(self, write: Callable[[str], Awaitable[None]]) -> None:
        while True:
            if not any(self._queues):
                self._wake.clear()
                await self._wake.wait()
                continue
            # Token diambil dulu, baru baris dipilih: baris QUICK yang masuk
            # selama menunggu throttle tetap kebagian giliran pertama.
            await self._acquire()
            line = self._pop()
            if line is not None:
                await write(line)
