"""Daftar ignore, port msg_cuek dari Covid.5.0.tcl.

Seperti eggdrop, ignore memutus reaksi bot, bukan pengawasannya: perintah dan
filter badword diabaikan, tetapi guard tetap menghitung flood. Owner tidak
pernah terkunci, karena /msg auth dan sesi yang sudah login tidak terkena.
"""

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .globals import scramble_case
from .storage import StoreError, atomic_write
from .users import mask_match

_MASK = re.compile(r"^[^!@\s]+![^!@\s]+@[^!@\s]+$")
_REASON = "iGnored By CoVID"
_MAX_MINUTES = 10 * 365 * 24 * 60  # waktu yang lebih jauh merusak ignore list


@dataclass
class Ignore:
    mask: str
    creator: str
    created: float
    expires: float  # 0 berarti permanen


def valid_mask(mask: str) -> bool:
    """Harus nick!user@host dan bukan mask yang cocok untuk semua orang."""
    return bool(_MASK.match(mask)) and bool(mask.strip("*?!@."))


class IgnoreList:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: list[Ignore] = []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._items = [Ignore(**item) for item in data]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            raise StoreError(f"{path} rusak: {exc!r}") from None

    def _save(self) -> None:
        atomic_write(self.path, json.dumps([asdict(i) for i in self._items], indent=2))

    def _prune(self) -> None:
        now = time.time()
        fresh = [i for i in self._items if not i.expires or i.expires > now]
        if len(fresh) != len(self._items):
            self._items = fresh
            self._save()

    def items(self) -> list[Ignore]:
        self._prune()
        return list(self._items)

    def has(self, mask: str) -> bool:
        return any(item.mask.lower() == mask.lower() for item in self.items())

    def matches(self, source: str) -> bool:
        return any(mask_match(item.mask, source) for item in self.items())

    def add(self, mask: str, creator: str, minutes: int) -> None:
        expires = time.time() + minutes * 60 if minutes else 0
        self._items.append(Ignore(mask, creator, time.time(), expires))
        self._save()

    def remove(self, mask: str) -> None:
        self._items = [i for i in self._items if i.mask.lower() != mask.lower()]
        self._save()


class IgnoreCommands:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.ignores = bot.ignores
        bot.commands.add_private("ignore", self._ignore)

    def _send(self, nick: str, text: str) -> None:
        self.bot.quick(f"PRIVMSG {nick} :{text}")

    def _ignore(self, nick, user, args) -> None:
        action = args[0].lower() if args else ""
        if action == "add":
            self._add(nick, user, args[1:])
        elif action == "del":
            self._del(nick, args[1:])
        elif action == "list":
            self._list(nick)
        else:
            self._send(nick, "\x1fERROR\x1f: Usage add / del / list")

    def _add(self, nick, user, args) -> None:
        if not args or not valid_mask(args[0]):
            self._send(nick, "\x1fERROR\x1f: Usage add <nick!user@host> [minutes]")
            return
        mask = args[0]
        if self.ignores.has(mask):
            self._send(nick, "\x1fERROR\x1f: This is already a Valid Ignore.")
            return
        minutes = args[1] if len(args) > 1 else "0"
        if not minutes.isascii() or not minutes.isdigit() or len(minutes) > 8 \
                or int(minutes) > _MAX_MINUTES:
            self._send(nick, f"\x1fERROR\x1f: {scramble_case('duration must be minutes')}")
            return
        self.ignores.add(mask, user.handle, int(minutes))
        self._send(
            nick,
            f"\x02New Ignore\x02: {mask} - \x02Duration\x02: {int(minutes)} minutes"
            f" - \x02Reason\x02: {_REASON}",
        )

    def _del(self, nick, args) -> None:
        if not args or not self.ignores.has(args[0]):
            self._send(nick, "\x1fERROR\x1f: This is NOT a Valid Ignore.")
            return
        self.ignores.remove(args[0])
        self._send(nick, f"\x02Removed Ignore\x02: {args[0]}")

    def _list(self, nick) -> None:
        items = self.ignores.items()
        if not items:
            self._send(nick, "\x02There are Currently no Ignores\x02")
            return
        self._send(nick, "\x02Current Ignore List\x02")
        for item in items:
            created = time.ctime(item.created)
            expires = time.ctime(item.expires) if item.expires else "Permanent!"
            self.bot.serv(
                f"PRIVMSG {nick} :\x02Mask\x02: {item.mask} - \x02Set by\x02: {item.creator}"
                f" - \x02Created On\x02: {created} - \x02Expired On\x02: {expires} "
            )
