"""Memuat dan memvalidasi config.toml."""

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .users import valid_handle

MIN_OWNER_KEY = 12  # nick bisa dipakai siapa saja, jadi kata kunci yang melindungi


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Server:
    host: str
    port: int = 6667
    tls: bool = False
    tls_verify: bool = True
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class Timing:
    ping_interval: float = 60
    lag_warn: float = 10
    stoned_timeout: float = 180
    split_timeout: float = 600
    split_window: float = 3
    connect_timeout: float = 30
    register_timeout: float = 60
    reconnect_min: float = 15
    reconnect_max: float = 300
    stable_after: float = 120
    send_burst: int = 4
    send_interval: float = 2
    tick: float = 1


@dataclass(frozen=True)
class Guard:
    """Nilai awal dari Covid.5.0.tcl baris 58-63 dan 104-113."""

    flood_chan: str = "10:60"
    flood_join: str = "5:60"
    flood_ctcp: str = "3:60"
    flood_kick: str = "3:10"
    flood_deop: str = "3:10"
    flood_nick: str = "5:60"
    ban_time: float = 120  # menit
    rejoin_delay: float = 10
    rejoin_max: int = 3
    rejoin_window: float = 60
    rejoin_cooldown: float = 300
    automod_max: int = 10
    automod_window: float = 8
    automod_duration: float = 60
    op_grace: float = 30
    opcmd: str = "PRIVMSG ChanServ :op %c% %n%"


def parse_rate(text: str) -> tuple[int, float]:
    """"10:60" menjadi (10, 60.0): maksimal 10 kejadian per 60 detik."""
    try:
        count, seconds = text.split(":")
        rate = int(count), float(seconds)
    except (ValueError, AttributeError):
        raise ConfigError(f"[guard] format harus jumlah:detik, bukan {text!r}") from None
    if rate[0] <= 0 or not 0 < rate[1] < math.inf:  # nan juga gagal di sini
        raise ConfigError(f"[guard] nilai harus positif: {text!r}")
    return rate


@dataclass(frozen=True)
class Config:
    nick: str
    servers: tuple[Server, ...]
    alt_nick: str = ""
    user: str = "covid"
    realname: str = "!cVd!"
    umode: str = "+xiB-ws"
    channels: tuple[str, ...] = ()
    admins: tuple[str, ...] = ()
    owner: str = ""
    owner_key: str = field(default="", repr=False)
    nickpass: str = field(default="", repr=False)
    nickserv: str = "NickServ"
    log_file: str = "logs/covidbot.log"
    users_file: str = "data/users.json"
    data_dir: str = "data"
    timing: Timing = field(default_factory=Timing)
    guard: Guard = field(default_factory=Guard)


def _section(cls, raw: dict, name: str):
    try:
        return cls(**raw)
    except TypeError as exc:
        raise ConfigError(f"[{name}] tidak valid: {exc}") from None


def parse_config(data: dict) -> Config:
    try:
        return _build_config(data)
    except (TypeError, AttributeError, ValueError) as exc:
        raise ConfigError(f"tipe nilai salah: {exc}") from None


def _build_config(data: dict) -> Config:
    bot = dict(data.get("bot", {}))
    bot.setdefault("alt_nick", f"{bot.get('nick', '')}_")
    bot["channels"] = tuple(bot.get("channels", ()))
    bot["admins"] = tuple(bot.get("admins", ()))

    servers = tuple(_section(Server, s, "servers") for s in data.get("servers", ()))
    timing = _section(Timing, data.get("timing", {}), "timing")
    guard = _section(Guard, data.get("guard", {}), "guard")
    try:
        config = Config(servers=servers, timing=timing, guard=guard, **bot)
    except TypeError as exc:
        raise ConfigError(f"[bot] tidak valid: {exc}") from None

    _validate(config)
    return config


def _validate(config: Config) -> None:
    for name in ("nick", "alt_nick", "user", "realname", "umode", "owner",
                 "owner_key", "nickpass", "nickserv", "log_file", "users_file", "data_dir"):
        if not isinstance(getattr(config, name), str):
            raise ConfigError(f"[bot] {name} harus teks")
    for name in ("channels", "admins"):
        if not all(isinstance(item, str) for item in getattr(config, name)):
            raise ConfigError(f"[bot] {name} harus daftar teks")
    if not config.servers:
        raise ConfigError("minimal satu [[servers]] harus diisi")
    for server in config.servers:
        if not isinstance(server.host, str) or not server.host:
            raise ConfigError("[[servers]] butuh host")
        if not isinstance(server.port, int) or isinstance(server.port, bool):
            raise ConfigError(f"port tidak valid: {server.port!r}")
        if not 0 < server.port < 65536:
            raise ConfigError(f"port tidak valid: {server.port}")

    for name in (config.nick, config.alt_nick, config.user):
        if not name or " " in name:
            raise ConfigError(f"nick/user tidak valid: {name!r}")

    if config.owner and not valid_handle(config.owner):
        raise ConfigError(f"owner tidak valid: {config.owner!r}")
    if config.owner_key and not config.owner:
        raise ConfigError("owner_key butuh owner")
    if config.owner and (len(config.owner_key) < MIN_OWNER_KEY or " " in config.owner_key):
        raise ConfigError(f"owner_key minimal {MIN_OWNER_KEY} karakter dan tanpa spasi")
    if " " in config.nickpass:
        raise ConfigError("nickpass tidak boleh mengandung spasi")
    if not config.nickserv or " " in config.nickserv:
        raise ConfigError(f"nickserv tidak valid: {config.nickserv!r}")

    for channel in config.channels:
        if channel[:1] not in ("#", "&") or " " in channel:
            raise ConfigError(f"nama channel tidak valid: {channel!r}")

    guard = config.guard
    for name, value in vars(guard).items():
        if name.startswith("flood_"):
            parse_rate(value)
        elif name == "opcmd":
            if not value or "\r" in value or "\n" in value:
                raise ConfigError("[guard] opcmd tidak valid")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"[guard] {name} harus angka positif")

    timing = config.timing
    for name, value in vars(timing).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"[timing] {name} harus angka positif")
    if timing.stoned_timeout <= timing.lag_warn:
        raise ConfigError("stoned_timeout harus lebih besar dari lag_warn")
    if timing.reconnect_max < timing.reconnect_min:
        raise ConfigError("reconnect_max tidak boleh lebih kecil dari reconnect_min")


def load_config(path: str | Path) -> Config:
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except OSError as exc:
        raise ConfigError(f"tidak bisa membaca {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} bukan TOML yang valid: {exc}") from None
    return parse_config(data)
