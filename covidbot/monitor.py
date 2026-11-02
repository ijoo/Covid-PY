"""Keep-alive ping, deteksi lag, dan deteksi server stoned."""

import time
from collections.abc import Callable


class Monitor:
    """Satu instance untuk satu koneksi; tick() dipanggil berkala oleh bot."""

    def __init__(
        self,
        ping_interval: float,
        lag_warn: float,
        stoned_timeout: float,
        send_ping: Callable[[str], None],
        on_lag: Callable[[float], None],
        on_recover: Callable[[float], None],
        on_stoned: Callable[[float], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = ping_interval
        self._lag_warn = lag_warn
        self._stoned_timeout = stoned_timeout
        self._send_ping = send_ping
        self._on_lag = on_lag
        self._on_recover = on_recover
        self._on_stoned = on_stoned
        self._clock = clock

        self.lag: float | None = None
        self.lagging = False
        self.stoned = False
        self._token: str | None = None
        self._sent_at = 0.0
        self._next_ping = clock()
        self._counter = 0

    def tick(self) -> None:
        if self.stoned:
            return
        now = self._clock()
        if self._token is None:
            if now >= self._next_ping:
                self._ping(now)
            return

        waited = now - self._sent_at
        if waited >= self._stoned_timeout:
            self.stoned = True
            self._on_stoned(waited)
            return
        # PONG belum datang, tapi lag sudah setidaknya selama ini.
        if self.lag is None or waited > self.lag:
            self._update_lag(waited)

    def on_pong(self, token: str) -> None:
        if token != self._token:
            return
        self._token = None
        self.lag = None
        self._update_lag(self._clock() - self._sent_at)

    def _ping(self, now: float) -> None:
        self._counter += 1
        self._token = f"covid-{self._counter}"
        self._sent_at = now
        self._next_ping = now + self._interval
        self._send_ping(self._token)

    def _update_lag(self, lag: float) -> None:
        self.lag = lag
        if lag >= self._lag_warn and not self.lagging:
            self.lagging = True
            self._on_lag(lag)
        elif lag < self._lag_warn and self.lagging:
            self.lagging = False
            self._on_recover(lag)
