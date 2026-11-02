"""Userlist bot, padanan userfile eggdrop."""

import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import irc_lower
from .storage import atomic_write

log = logging.getLogger(__name__)

MIN_PASSWORD = 10
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_HANDLE = re.compile(r"^[A-Za-z0-9\[\]\\`_^{|}~-]{1,32}$")


class UserFileError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(stored: str, password: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except ValueError:
        return False


def mask_match(mask: str, text: str) -> bool:
    """Cocokkan hostmask tanpa huruf besar-kecil, menurut CASEMAPPING server."""
    return glob_match(irc_lower(mask), irc_lower(text))


def glob_match(mask: str, text: str) -> bool:
    """Wildcard * dan ?, peka huruf besar-kecil.

    fnmatch tidak bisa dipakai karena [ dan ] sah dalam nick, dan regex bisa
    backtracking parah pada mask dengan banyak bintang.
    """
    m = t = 0
    star = -1
    resume = 0
    while t < len(text):
        if m < len(mask) and mask[m] == "*":
            star, resume = m, t
            m += 1
        elif m < len(mask) and mask[m] in ("?", text[t]):
            m += 1
            t += 1
        elif star != -1:
            resume += 1
            m, t = star + 1, resume
        else:
            return False
    return not mask[m:].strip("*")


def valid_handle(handle: str) -> bool:
    return bool(_HANDLE.match(handle))


@dataclass
class User:
    handle: str
    flags: set[str] = field(default_factory=set)
    hosts: list[str] = field(default_factory=list)
    password: str = field(default="", repr=False)


class UserDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._users: dict[str, User] = {}
        self._load()

    def __iter__(self):
        return iter(sorted(self._users.values(), key=lambda u: u.handle.lower()))

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
            for handle, raw in data.items():
                self._users[handle.lower()] = User(
                    handle, set(raw["flags"]), list(raw["hosts"]), raw.get("password", "")
                )
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Jangan lanjut dengan list kosong: simpan berikutnya menimpa file.
            raise UserFileError(f"{self.path} rusak: {exc!r}") from None

    def save(self) -> None:
        """Tulis atomik dengan izin 600, karena file berisi hash password."""
        data = {
            user.handle: {
                "flags": "".join(sorted(user.flags)),
                "hosts": user.hosts,
                "password": user.password,
            }
            for user in self
        }
        atomic_write(self.path, json.dumps(data, indent=2))

    def get(self, handle: str) -> User | None:
        return self._users.get(handle.lower())

    def add(self, handle: str, host: str | None = None, flags: str = "") -> User:
        """Owner tanpa host: mereka dikenali dari password."""
        user = self._users.setdefault(handle.lower(), User(handle))
        user.flags.update(flags)
        if host is not None and host not in user.hosts:
            user.hosts.append(host)
        return user

    def remove(self, handle: str) -> bool:
        return self._users.pop(handle.lower(), None) is not None

    def match(self, mask: str) -> User | None:
        """Handle untuk nick!user@host; mask paling spesifik yang menang."""
        best, best_score = None, (-1, "")
        for user in self:
            for host in user.hosts:
                if mask_match(host, mask):
                    score = (len(host.replace("*", "").replace("?", "")), user.handle)
                    if score > best_score:
                        best, best_score = user, score
        return best

    def has_owner(self) -> bool:
        return any("n" in user.flags for user in self)
