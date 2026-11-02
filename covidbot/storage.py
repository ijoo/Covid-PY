"""Penyimpanan file di folder data."""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class StoreError(Exception):
    pass


def atomic_write(path: Path, text: str) -> None:
    """Tulis lewat file sementara, supaya crash tidak menyisakan file setengah jadi."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


class ChannelStore:
    """Daftar channel yang harus dimasuki, padanan chanfile eggdrop.

    Hanya selisih dari config yang disimpan, jadi channel yang baru ditambah
    ke config tetap dimasuki walaupun file ini sudah ada.
    """

    def __init__(self, path: Path, configured: tuple[str, ...]) -> None:
        self.path = path
        self._configured = configured
        self._joined: list[str] = []
        self._parted: list[str] = []
        self._flags: dict[str, list[str]] = {}  # lower -> flag, seperti setudef
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._joined = list(data["joined"])
            self._parted = list(data["parted"])
            self._flags = dict(data.get("flags", {}))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StoreError(f"{path} rusak: {exc!r}") from None

    @staticmethod
    def _has(names: list[str] | tuple[str, ...], name: str) -> bool:
        return name.lower() in (item.lower() for item in names)

    @staticmethod
    def _without(names: list[str], name: str) -> list[str]:
        return [item for item in names if item.lower() != name.lower()]

    def wanted(self) -> list[str]:
        channels = [c for c in self._configured if not self._has(self._parted, c)]
        channels += [c for c in self._joined if not self._has(channels, c)]
        return channels

    def add(self, name: str) -> None:
        self._parted = self._without(self._parted, name)
        if not self._has(self._configured, name) and not self._has(self._joined, name):
            self._joined.append(name)
        self._save()

    def remove(self, name: str) -> None:
        self._joined = self._without(self._joined, name)
        self._flags.pop(name.lower(), None)
        if self._has(self._configured, name) and not self._has(self._parted, name):
            self._parted.append(name)
        self._save()

    def has_flag(self, name: str, flag: str) -> bool:
        return flag in self._flags.get(name.lower(), ())

    def set_flag(self, name: str, flag: str, active: bool) -> None:
        flags = [item for item in self._flags.get(name.lower(), ()) if item != flag]
        if active:
            flags.append(flag)
        self._flags[name.lower()] = flags
        self._save()

    def _save(self) -> None:
        data = {"joined": self._joined, "parted": self._parted, "flags": self._flags}
        atomic_write(self.path, json.dumps(data, indent=2))


class KickCounter:
    """Nomor kick yang terus naik, padanan khitung dan covidk.db."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._count = 0
        try:
            self._count = int(path.read_text().strip() or 0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("%s tidak terbaca, hitungan kick mulai dari 0", path)

    def next(self) -> int:
        self._count += 1
        atomic_write(self.path, f"{self._count}\n")
        return self._count
