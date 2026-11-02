"""Perintah bot dan sisa perilaku dari Covid.5.0.tcl: cvdset, help, nick, realname,
identify, restart, die, pesan away, dan jawaban CTCP VERSION.
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import replace
from pathlib import Path

from .config import parse_rate
from .globals import lgrnd, notic, notim, scramble_case, verc
from .protocol import Message, irc_lower
from .storage import StoreError, atomic_write
from .users import valid_handle

log = logging.getLogger(__name__)

_MAX_REALNAME = 60
_STOP_DELAY = 1.0
_AWAY_FIRST = 120.0  # timer 2 setaway
_VERSION_LIMIT = "3:60"  # global-flood-ctcp

_SETTINGS = {
    ("rejoin", "delay"): "rejoin_delay",
    ("rejoin", "max"): "rejoin_max",
    ("rejoin", "window"): "rejoin_window",
    ("rejoin", "cooldown"): "rejoin_cooldown",
    ("automod", "max"): "automod_max",
    ("automod", "window"): "automod_window",
    ("automod", "duration"): "automod_duration",
    ("guard", "grace"): "op_grace",
}

_AWAY = (
    "aWaY FrOM KeyBoaRd", "SlEppiNG oN Net", "WaCthiNG Tube6.nET", "Got tO gO!",
    "No oNE CarE!", "CoVID tcl bY iJoo", "EgGDrop noT diES", "i ThiNk aBouT yOu",
    "reMeMbeRiNg You", "Play mOBiLE lEGenD", "CoRoNaViRus iS DeADly",
    "NoT ReaDY F0r QueRY", "Bad Boy nOT AlLoWed", "CrOSsinG tHE StrEet",
)
_VERSIONS = (
    "eGgdRoP v1.5.6+ctcpfix (c) 1996", "xchat 2.8.8 Ubuntu",
    "rZNC Version 1.0 [02/01/11] - Built from ZNC.", "psyBNC-2.3.1-7",
    "HexChat 2.16.1 (x64) / Windows 10", "PIRCH98:WIN 95/98/WIN NT:1.0 (build 1.0.1.1190)",
    "Snak for Macintosh 4.13 English", "mIRC 3.75 via ZNC 1.8.2deb10+2",
    "AdiIRC 4.3 (x64, .NET 4.8.0)", "IceChat 9.12 / Windows 10",
    "irssi v1.4.4 (20230222 1445) / Linux x86_64", "WeeChat 4.1.2 (Linux x86_64)",
)

_HELP_PRIVATE = (
    ("auth <password>", "login owner access"),
    ("deauth", "logout from your bot"),
    ("join <#chan>", "ask bot to join channel"),
    ("part <#chan>", "ask bot to part channel"),
    ("nick <nick> <password>", "ask bot to change botnick"),
    ("realname <text>", "ask bot to change realname"),
    ("identify <password>", "ask bot to identify botnick"),
    ("ignore <add/del/list> <*!*@*>", "ask bot to ignore host user"),
    ("cvdset <category> <option> <value>", "tune protection thresholds live"),
    ("!badword <word> [word2 ...]", "add forbidden word(s) to the filter, wildcards ok"),
    ("restart", "ask bot to restart"),
    ("die", "ask bot to shutdown"),
)
_HELP_PUBLIC = (
    ("`auth", "see your status"),
    ("`deauth", "logout from your bot"),
    ("`ver", "display bot information"),
    ("`join <#chan>", "ask join to channel"),
    ("`part <#chan>", "ask bot to part channel"),
    ("`k <nick> [reason]", "ask bot to kick nick from channel"),
    ("`kb <nick> [reason]", "ask bot to kickban nick from channel"),
    ("`+b <nick>", "ask bot to ban nick"),
    ("`-b <nick> [#chan]", "ask bot to unban nick"),
    ("`mub [#chan]", "ask bot to mass unban"),
    ("`+v <nick>", "ask bot to voice nick"),
    ("`mv [#chan]", "ask bot to mass voice nick"),
    ("`-v <nick>", "ask bot to devoice nick"),
    ("`mdv [#chan]", "ask bot to mass devoice nick"),
    ("`+o <nick>", "ask bot to @op nick"),
    ("`-o <nick>", "ask bot to de@op nick"),
    ("`mode <modes>", "ask bot to set channel modes"),
    ("`topic <text>", "ask bot to set topic"),
    ("`+guard", "activate channel protection engine"),
    ("`-guard", "deactivate channel protection engine"),
    ("`+badword", "activate badword filter on this channel"),
    ("`-badword", "deactivate badword filter on this channel"),
    ("`status", "show active channel protection status"),
    ("`cycle", "ask bot to cycle from channel"),
    ("`logo", "ask bot to send logo"),
    ("`ping [nick]", "ask bot to ctcp ping"),
    ("`+f <nick>", "ask bot to add user to friend list"),
    ("`-f <nick>", "ask bot to remove user from friend list"),
    ("`+av <nick>", "ask bot to add user to autovoice list"),
    ("`-av <nick>", "ask bot to remove user from autovoice list"),
    ("`deluser <handle>", "ask bot to delete a user record"),
    ("`userlist", "ask bot to list all user records"),
)


def format_duration(seconds: float) -> str:
    """Padanan duration di eggdrop: 2 days 3 hours 5 minutes."""
    seconds = int(seconds)
    parts = []
    for label, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {label}{'s' if amount != 1 else ''}")
    return " ".join(parts) or f"{seconds} second{'s' if seconds != 1 else ''}"


class BotCommands:
    def __init__(self, bot, settings_path: Path) -> None:
        self.bot = bot
        self.settings_path = settings_path
        self._settings: dict[str, int] = {}
        self._started = time.monotonic()
        self._version_times: list[float] = []
        self._version_limit = parse_rate(_VERSION_LIMIT)
        self._load_settings()

        for name, handler in {
            "cvdset": self._cvdset,
            "help": self._help,
            "nick": self._nick,
            "realname": self._realname,
            "identify": self._identify,
            "restart": self._restart,
            "die": self._die,
        }.items():
            bot.commands.add_private(name, handler)
        bot.on("PRIVMSG")(self._on_privmsg)
        bot.on("connected")(self._on_connected)

    # cvdset

    def _load_settings(self) -> None:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise StoreError(f"{self.settings_path} rusak: {exc!r}") from None
        known = set(_SETTINGS.values())
        if not isinstance(data, dict) or not set(data) <= known:
            raise StoreError(f"{self.settings_path} berisi setelan tak dikenal")
        for name, value in data.items():
            if type(value) is not int or value < 1:
                raise StoreError(f"{self.settings_path}: {name} harus angka positif")
        self._settings = data
        self.bot.guard.config = replace(self.bot.guard.config, **data)

    def _say(self, nick: str, text: str) -> None:
        self.bot.help(f"PRIVMSG {nick} :{notic} {text}")

    def _cvdset(self, nick, user, args) -> None:
        sc = scramble_case
        if not args:
            self._say(nick, "Usage: cvdset <category> <option> <value>")
            self._say(nick, "Categories: rejoin <delay/max/window/cooldown>,"
                            " automod <max/window/duration>, guard <grace>")
            return
        config = self.bot.guard.config
        category = args[0].lower()
        if len(args) == 1:
            names = [key for key in _SETTINGS if key[0] == category]
            for key in names:
                self._say(nick, f"{category} {key[1]} = {getattr(config, _SETTINGS[key])}")
            if not names:
                self._say(nick, f"{sc('unknown category')}: {category}")
            return
        key = (category, args[1].lower())
        if key not in _SETTINGS:
            self._say(nick, f"{sc('unknown category/option')}: {' '.join(key)}")
            return
        name = _SETTINGS[key]
        if len(args) == 2:
            self._say(nick, f"{key[0]} {key[1]} = {getattr(config, name)}")
            return
        value = args[2]
        if not value.isascii() or not value.isdigit() or int(value) < 1:
            self._say(nick, sc("value must be a positive integer"))
            return
        self._settings[name] = int(value)
        atomic_write(self.settings_path, json.dumps(self._settings, indent=2))
        self.bot.guard.config = replace(config, **{name: int(value)})
        self._say(nick, f"{key[0]} {key[1]} {sc('set to')} {value}")

    # help

    def _help(self, nick, user, args) -> None:
        send = lambda text: self.bot.help(f"PRIVMSG {nick} :{text}")
        sc = scramble_case
        send(f"\x1f\x02{sc('bot command list via private')}\x02\x1f")
        for command, text in _HELP_PRIVATE:
            send(f"\x02{command}\x02 - {sc(text)}")
        send(f"\x1f\x02{sc('bot command list via public')}\x02\x1f")
        for command, text in _HELP_PUBLIC:
            send(f"\x02{command}\x02 - {sc(text)}")
        send(f"{sc('powered by')} {lgrnd} {verc}")

    # Identitas

    def _nick(self, nick, user, args) -> None:
        if len(args) < 2:
            self._say(nick, "Command: \x02nick\x02 <nick> <pass>")
            return
        new, password = args[0], args[1]
        if not valid_handle(new):
            self._say(nick, scramble_case("invalid nickname"))
            return
        # NICK tidak punya kolom password: ini password NickServ untuk identify
        # berikutnya. Berlaku sampai restart, sama seperti di TCL.
        self.bot.nickserv.prepare(new, password)
        self.bot.wanted_nick = new
        self.bot.serv(f"NICK {new}")
        self.bot.quick(f"PRIVMSG {nick} :{notic} {scramble_case('botnick change to')} {new}")

    def _realname(self, nick, user, args) -> None:
        text = " ".join(args)
        if not text:
            self._say(nick, "Command: \x02realname\x02 <realname>")
            return
        if len(text) > _MAX_REALNAME:
            self._say(nick, f"{scramble_case('too long')} (max {_MAX_REALNAME})")
            return
        # Realname hanya dikirim saat mendaftar, jadi harus keluar dulu.
        self.bot.realname = text
        self.bot.quick(f"QUIT :{notic} {scramble_case('realname changed by')} [ {nick} ]")

    def _identify(self, nick, user, args) -> None:
        if not args:
            self.bot.quick(f"PRIVMSG {nick} :{notic} Command: /msg {self.bot.nick} identify <password>")
            return
        self.bot.nickserv.identify_with(args[0])

    # Restart dan die

    def _quit_reason(self, nick, args, label) -> str:
        text = scramble_case(" ".join(args))
        return text or f"{scramble_case(f'{label} request by')} \x030,4 {nick} \x03"

    def _stop_soon(self, reason: str, restart: bool) -> None:
        """Beri waktu balasan terkirim. Bukan bot.later: timer itu batal saat
        koneksi putus, padahal perintah owner tetap harus dijalankan."""
        asyncio.get_running_loop().call_later(_STOP_DELAY, self.bot.stop, reason, restart)

    def _restart(self, nick, user, args) -> None:
        reason = self._quit_reason(nick, args, "restart")
        self.bot.quick(f"PRIVMSG {nick} :{notim} {scramble_case('restart now')}!")
        self._stop_soon(reason, True)

    def _die(self, nick, user, args) -> None:
        reason = self._quit_reason(nick, args, "shutdown")
        self.bot.quick(f"PRIVMSG {nick} :{notim} {scramble_case('shutdown now')}!")
        self._stop_soon(reason, False)

    # Away dan CTCP VERSION

    def _on_connected(self) -> None:
        self.bot.later(_AWAY_FIRST, self.set_away)
        self._schedule_away()

    def _schedule_away(self) -> None:
        """bind time {00 * * * *}: setiap awal jam."""
        self.bot.later(3600 - time.time() % 3600, self._hourly_away)

    def _hourly_away(self) -> None:
        self.set_away()
        self._schedule_away()

    def set_away(self) -> None:
        uptime = format_duration(time.monotonic() - self._started)
        message = random.choice(_AWAY)
        self.bot.serv(f"AWAY :{lgrnd} [\x02Uptime:\x02 {uptime}] « {message} » ")

    def _on_privmsg(self, msg: Message) -> None:
        """autoversion: jawab dengan versi klien acak."""
        if len(msg.params) < 2 or msg.params[1] != "\x01VERSION\x01" or msg.prefix is None:
            return
        target = msg.params[0]
        if irc_lower(target) != irc_lower(self.bot.nick) and target[:1] not in ("#", "&"):
            return
        if self.bot.ignores.matches(msg.prefix.raw):
            return
        limit, window = self._version_limit
        now = time.monotonic()
        self._version_times = [t for t in self._version_times if now - t < window]
        if len(self._version_times) >= limit:
            return  # jangan jadi pemantul flood CTCP
        self._version_times.append(now)
        self.bot.help(f"NOTICE {msg.nick} :\x01VERSION {random.choice(_VERSIONS)}\x01")
