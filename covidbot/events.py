"""Event bus sederhana, padanan `bind` di eggdrop."""

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()

    def on(self, event: str) -> Callable:
        """Decorator pendaftaran handler; handler boleh sync atau async."""

        def register(handler: Callable) -> Callable:
            self._handlers[event].append(handler)
            return handler

        return register

    def emit(self, event: str, *args) -> None:
        """Panggil semua handler.

        Handler async dijalankan sebagai task agar tidak menahan pembacaan
        socket: handler lambat tidak boleh menunda balasan PONG. Error handler
        hanya dicatat dan tidak mengganggu koneksi.
        """
        for handler in self._handlers[event]:
            try:
                result = handler(*args)
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    self._tasks.add(task)
                    task.add_done_callback(self._finish)
            except Exception:
                log.exception("handler %s gagal", event)

    def _finish(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("handler async gagal", exc_info=task.exception())
