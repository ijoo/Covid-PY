"""Parsing dan encoding baris IRC.

Semua pemecahan memakai split(" "), bukan split() tanpa argumen: split() biasa
ikut memecah di \\x1f (kode underline) karena Python menganggapnya whitespace.
"""

import re
from dataclasses import dataclass, field

MAX_LINE_BYTES = 510  # 512 dikurangi CRLF

_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_CASEMAPS = {
    "ascii": str.maketrans(_UPPER, _LOWER),
    "rfc1459": str.maketrans(_UPPER + "[]\\~", _LOWER + "{}|^"),
    "strict-rfc1459": str.maketrans(_UPPER + "[]\\", _LOWER + "{}|"),
    "rfc1459-strict": str.maketrans(_UPPER + "[]\\", _LOWER + "{}|"),
}
_casemap = _CASEMAPS["rfc1459"]
_FORMATTING = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x11\x16\x1d\x1e\x1f]")
_SECRET_LINE = re.compile(
    r"^((?:@\S+ )?(?::\S+ )?(?:PASS|PRIVMSG \S+ :\s*(?:identify|auth|setpass|release|nick)))\b.*",
    re.IGNORECASE,
)
_secrets: set[str] = set()


def set_casemapping(name: str) -> bool:
    """Pakai aturan dari ISUPPORT CASEMAPPING; nilai tak dikenal diabaikan."""
    global _casemap
    table = _CASEMAPS.get(name.lower())
    if table is None:
        return False
    _casemap = table
    return True


def irc_lower(text: str) -> str:
    """Huruf kecil menurut CASEMAPPING server (default rfc1459)."""
    return text.translate(_casemap)


def redact(line: str) -> str:
    """Samarkan baris berisi password sebelum masuk log."""
    for secret in _secrets:
        line = line.replace(secret, "***")
    return _SECRET_LINE.sub(r"\1 ***", line)


def add_secret(text: str) -> None:
    """Daftarkan teks yang tidak boleh muncul di log, seperti kata kunci owner."""
    if text:
        _secrets.add(text)


def strip_formatting(text: str) -> str:
    """Buang kode warna dan format mIRC."""
    return _FORMATTING.sub("", text)


@dataclass(frozen=True)
class Prefix:
    raw: str
    nick: str
    user: str = ""
    host: str = ""

    @property
    def userhost(self) -> str:
        return f"{self.user}@{self.host}"

    @classmethod
    def parse(cls, raw: str) -> "Prefix":
        nick, _, rest = raw.partition("!")
        user, _, host = rest.partition("@")
        if not rest:
            nick, _, host = raw.partition("@")
        return cls(raw, nick, user, host)


@dataclass(frozen=True)
class Message:
    command: str
    params: tuple[str, ...] = ()
    prefix: Prefix | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def nick(self) -> str:
        return self.prefix.nick if self.prefix else ""


def parse_line(line: str) -> Message | None:
    """Parse satu baris tanpa CRLF. Baris rusak menghasilkan None."""
    rest = line.strip("\r\n")
    tags: dict[str, str] = {}
    prefix = None

    if rest.startswith("@"):
        raw_tags, _, rest = rest.partition(" ")
        for item in raw_tags[1:].split(";"):
            key, _, value = item.partition("=")
            tags[key] = value
        rest = rest.lstrip(" ")

    if rest.startswith(":"):
        raw_prefix, _, rest = rest.partition(" ")
        if len(raw_prefix) < 2:
            return None
        prefix = Prefix.parse(raw_prefix[1:])
        rest = rest.lstrip(" ")

    head, sep, trailing = rest.partition(" :")
    if rest.startswith(":"):
        return None
    params = [word for word in head.split(" ") if word]
    if not params:
        return None
    command = params.pop(0).upper()
    if sep:
        params.append(trailing)
    return Message(command, tuple(params), prefix, tags)


def parse_modes(
    params: tuple[str, ...], chanmodes: tuple[str, ...], prefix_modes: str
) -> list[tuple[str, str, str | None]]:
    """Pecah parameter MODE menjadi (tanda, huruf, argumen).

    chanmodes adalah empat kelompok ISUPPORT: selalu berargumen (A dan B), hanya
    saat diset (C), tanpa argumen (D).
    """
    list_modes, always, on_set = chanmodes[0], chanmodes[1], chanmodes[2]
    args = list(params[1:])
    changes = []
    sign = "+"
    for char in params[0]:
        if char in "+-":
            sign = char
            continue
        takes_arg = (
            char in prefix_modes
            or char in list_modes
            or char in always
            or (char in on_set and sign == "+")
        )
        changes.append((sign, char, args.pop(0) if takes_arg and args else None))
    return changes


def encode_line(line: str) -> bytes:
    """Siapkan satu baris keluar.

    CR, LF, dan NUL dibuang supaya teks dari pengguna tidak bisa menyisipkan
    perintah IRC tambahan. Baris dipotong tanpa memecah karakter UTF-8.
    """
    clean = line.translate({0x0d: None, 0x0a: None, 0x00: None})
    data = clean.encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        data = data[:MAX_LINE_BYTES].decode("utf-8", "ignore").encode("utf-8")
    return data + b"\r\n"
