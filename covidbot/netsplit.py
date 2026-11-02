"""Deteksi netsplit dan netjoin dari pesan QUIT."""

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .protocol import irc_lower

_HOSTNAME = re.compile(r"^(?:[A-Za-z0-9_*-]+\.)+(?:[A-Za-z]{2,}|\*)$")

Servers = tuple[str, str]
Key = tuple[str, str]


def parse_split(message: str) -> Servers | None:
    """Kembalikan pasangan server kalau pesan QUIT berformat netsplit.

    Pasangan diurutkan, jadi "a b" dan "b a" masuk kelompok yang sama. Di
    jaringan yang tidak memberi awalan "Quit: " pada pesan pengguna, seseorang
    tetap bisa memalsukan format ini; karena itu pesan ini jangan dijadikan
    dasar tindakan yang merugikan orang lain.
    """
    tokens = message.split(" ")
    if len(tokens) != 2:
        return None
    first, second = tokens
    if not (_HOSTNAME.match(first) and _HOSTNAME.match(second)):
        return None
    if first.lower() == second.lower():
        return None
    return (first, second) if first.lower() < second.lower() else (second, first)


@dataclass(frozen=True)
class SplitInfo:
    """Ringkasan satu kejadian; dikirim bersama event netsplit/netjoin."""

    servers: Servers
    members: tuple[tuple[str, str], ...]  # pasangan (nick, channel)

    @property
    def nicks(self) -> list[str]:
        return sorted({nick for nick, _ in self.members}, key=str.lower)

    @property
    def channels(self) -> list[str]:
        return sorted({chan for _, chan in self.members}, key=str.lower)


@dataclass
class _Entry:
    nick: str
    channel: str
    userhost: str
    servers: Servers
    since: float


@dataclass
class _Batch:
    deadline: float
    members: dict[Key, tuple[str, str]] = field(default_factory=dict)


class NetsplitTracker:
    def __init__(
        self,
        window: float,
        timeout: float,
        emit: Callable[..., None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._timeout = timeout
        self._emit = emit
        self._clock = clock
        self._entries: dict[Key, _Entry] = {}
        self._splits: dict[Servers, _Batch] = {}
        self._joins: dict[Servers, _Batch] = {}

    def is_split(self, nick: str, channel: str) -> bool:
        return (irc_lower(nick), irc_lower(channel)) in self._entries

    def on_quit(
        self, nick: str, userhost: str, channels: Iterable[str], message: str
    ) -> bool:
        servers = parse_split(message)
        if servers is None:
            return False
        now = self._clock()
        for channel in channels:
            key = (irc_lower(nick), irc_lower(channel))
            self._entries[key] = _Entry(nick, channel, userhost, servers, now)
            self._add(self._splits, servers, key, nick, channel, now)
        return True

    def on_join(self, nick: str, userhost: str, channel: str) -> bool:
        """True kalau JOIN ini adalah kembalinya nick yang kena split."""
        key = (irc_lower(nick), irc_lower(channel))
        entry = self._entries.pop(key, None)
        if entry is None or entry.userhost != userhost:
            return False  # nick sama dari host lain bukan orang yang split
        self._add(self._joins, entry.servers, key, nick, channel, self._clock())
        return True

    def tick(self) -> None:
        now = self._clock()
        self._flush(self._splits, "netsplit", now)
        self._flush(self._joins, "netjoin", now)

        expired: dict[Servers, list[tuple[str, str]]] = {}
        for key, entry in list(self._entries.items()):
            if now - entry.since >= self._timeout:
                del self._entries[key]
                expired.setdefault(entry.servers, []).append(
                    (entry.nick, entry.channel)
                )
        for servers, members in expired.items():
            self._emit("split_expired", SplitInfo(servers, tuple(members)))

    def reset(self) -> None:
        self._entries.clear()
        self._splits.clear()
        self._joins.clear()

    def _add(self, batches, servers, key, nick, channel, now) -> None:
        batch = batches.setdefault(servers, _Batch(now + self._window))
        batch.deadline = now + self._window  # tunggu sampai rentetannya reda
        batch.members[key] = (nick, channel)

    def _flush(self, batches: dict[Servers, _Batch], event: str, now: float) -> None:
        for servers, batch in list(batches.items()):
            if now >= batch.deadline:
                del batches[servers]
                self._emit(event, SplitInfo(servers, tuple(batch.members.values())))
