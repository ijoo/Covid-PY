"""Perintah channel, port pub_* dan msg_join/msg_part dari Covid.5.0.tcl."""

import logging
import os
import platform
import random
import re
import time

from .globals import kickm, lgrnd, notic, partm, scramble_case, verc
from .protocol import Message, irc_lower
from .users import User, mask_match, valid_handle

log = logging.getLogger(__name__)

_MAX_TARGETS = 6  # batas per MODE di TCL
_UNBAN_BATCH = 5
_LIMIT_MAX, _LIMIT_WINDOW = 3, 10.0  # global-flood-kick 3:10
_PING_TTL = 60.0
VOICE_DELAY = (5.0, 20.0)  # voiceq: mass voice dicicil agar tidak mencolok
_CHANNEL = re.compile(r"^[#&][^\s,\x00-\x1f]{1,49}$")
_CTCP_PING = re.compile(r"^\x01PING (\d+)\x01?$")


class ChannelCommands:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.state = bot.state
        self.commands = bot.commands
        self._times: dict[tuple[str, str], list[float]] = {}
        self._pings: dict[str, tuple[str, str, float]] = {}
        self._started = time.monotonic()

        for name, handler in {
            "`k": self._kick,
            "`kb": self._kickban,
            "`+b": self._ban,
            "`-b": self._unban,
            "`mub": self._mass_unban,
            "`join": self._pub_join,
            "`part": self._pub_part,
            "`cycle": self._cycle,
            "`mode": self._mode,
            "`topic": self._topic,
            "`+v": lambda *a: self._set_status("+", "v", *a),
            "`-v": lambda *a: self._set_status("-", "v", *a),
            "`+o": lambda *a: self._set_status("+", "o", *a),
            "`-o": lambda *a: self._set_status("-", "o", *a),
            "`mv": self._mass_voice,
            "`mdv": self._mass_devoice,
            "`ver": self._info,
            "`logo": self._logo,
            "`ping": self._ping,
        }.items():
            self.commands.add_public(name, handler)
        self.commands.add_private("join", self._msg_join)
        self.commands.add_private("part", self._msg_part)
        bot.on("JOIN")(self._autovoice)
        bot.on("NOTICE")(self._ping_reply)
        bot.on("PRIVMSG")(self._ping_reply)

    # Bantuan

    def _reply(self, nick: str, text: str) -> None:
        self.commands.reply(nick, text)

    def _usage(self, nick: str, text: str) -> None:
        self._reply(nick, f"Usage: {text}")

    def _is_op(self, channel: str) -> bool:
        return self.state.is_op(channel, self.bot.nick)

    def _need_op(self, nick: str, channel: str) -> bool:
        if self._is_op(channel):
            return True
        self._reply(nick, f"{scramble_case('im not operator')} {channel}")
        return False

    def _channel_arg(self, nick: str, channel: str, args: list[str]) -> str | None:
        """Channel dari argumen atau tempat perintah; harus channel yang dimasuki."""
        if not args:
            return channel
        name = self._normalize(args[0])
        if name is None or not self.state.has_channel(name):
            self._reply(nick, f"{scramble_case('im not on that channel')}!")
            return None
        return name

    @staticmethod
    def _normalize(name: str) -> str | None:
        """Tolak koma dan karakter kontrol: JOIN menerima banyak channel sekaligus."""
        name = name if name[:1] in ("#", "&") else f"#{name}"
        return name if _CHANNEL.match(name) else None

    def _allowed(self, user: User, command: str, nick: str) -> bool:
        """cvd_cmd_allowed: batas perintah keras per owner."""
        key = (user.handle.lower(), command)
        now = time.monotonic()
        recent = [t for t in self._times.get(key, ()) if now - t < _LIMIT_WINDOW]
        self._times[key] = recent
        if len(recent) >= _LIMIT_MAX:
            text = scramble_case(f"too many {command} command, please wait")
            self._reply(nick, f"{text} {_LIMIT_WINDOW:.0f}s")
            return False
        recent.append(now)
        return True

    def protected(self, nick: str) -> str | None:
        """'owner' atau 'friend' menurut identitas asli, bukan sekadar nama nick."""
        user = self.commands.identify_nick(nick)
        if user is None:
            return None
        if self.commands.is_authed(user) or "n" in user.flags:
            return "owner"
        return "friend" if "f" in user.flags else None

    def _ban_mask(self, requester: str, target: str) -> str | None:
        userhost = self.state.find_userhost(target)
        if userhost is None:
            self._reply(requester, f"{scramble_case('cannot get host of')} {target}")
            return None
        return f"*!*@{userhost.partition('@')[2]}"

    def hits_protected(self, mask: str, channel: str) -> bool:
        """*!*@host juga mengenai semua orang lain di host itu, termasuk bot."""
        for member in self.state.members(channel):
            userhost = self.state.find_userhost(member)
            if userhost is None or not mask_match(mask, f"{member}!{userhost}"):
                continue
            if irc_lower(member) == irc_lower(self.bot.nick) or self.protected(member):
                return True
        return False

    def _send_batches(self, channel, sign, letter, items, size=_MAX_TARGETS) -> None:
        size = min(size, self.state.max_modes)
        for start in range(0, len(items), size):
            batch = items[start:start + size]
            self.bot.serv(
                f"MODE {channel} {sign}{letter * len(batch)} {' '.join(batch)}"
            )

    def _check_target(self, nick, channel, target, action) -> bool:
        """Pemeriksaan bersama kick dan ban."""
        if irc_lower(target) == irc_lower(self.bot.nick):
            self._reply(nick, f"{scramble_case(f'cannot {action} myself')}")
            return False
        if not self.state.on_channel(channel, target):
            self._reply(nick, f"{target} {scramble_case('is not in')} {channel}")
            return False
        kind = self.protected(target)
        if kind is not None:
            self._reply(nick, f"{scramble_case(f'cannot {action} my {kind}')}")
            return False
        return True

    def kick_line(self, channel: str, target: str, reason: str) -> str:
        reason = reason or random.choice(kickm)
        count = self.bot.kick_count.next()
        return f"KICK {channel} {target} :{lgrnd} {reason} -\x1f#\x02{count}\x02\x1f-"

    # Kick dan ban

    def _kick(self, nick, user, channel, args) -> None:
        if not args:
            self._usage(nick, "`k <nick> <reason>")
            return
        if not self._need_op(nick, channel):
            return
        target = args[0]
        if not self._check_target(nick, channel, target, "kick"):
            return
        if self._allowed(user, "kick", nick):
            self.bot.serv(self.kick_line(channel, target, " ".join(args[1:])))

    def _ban_target(self, nick, channel, target, action) -> str | None:
        """Mask ban untuk target, atau None kalau tidak boleh."""
        if not self._check_target(nick, channel, target, action):
            return None
        if self.state.is_op(channel, target):
            self._reply(nick, scramble_case(f"cannot {action} other operator"))
            return None
        mask = self._ban_mask(nick, target)
        if mask is not None and self.hits_protected(mask, channel):
            self._reply(nick, f"{mask} {scramble_case('would hit a protected user')}")
            return None
        return mask

    def _kickban(self, nick, user, channel, args) -> None:
        if not args:
            self._usage(nick, "`kb <nick> <reason>")
            return
        if not self._need_op(nick, channel):
            return
        mask = self._ban_target(nick, channel, args[0], "kickban")
        if mask is None or not self._allowed(user, "kickban", nick):
            return
        # Ban dulu: kalau kick lebih dulu, target sempat masuk lagi.
        self.bot.serv(f"MODE {channel} +b {mask}")
        self.bot.serv(self.kick_line(channel, args[0], " ".join(args[1:])))

    def _ban(self, nick, user, channel, args) -> None:
        if not args:
            self._usage(nick, "`+b <nick>")
            return
        if not self._need_op(nick, channel):
            return
        mask = self._ban_target(nick, channel, args[0], "ban")
        if mask is not None and self._allowed(user, "ban", nick):
            self.bot.serv(f"MODE {channel} +b {mask}")

    def _unban(self, nick, user, channel, args) -> None:
        if not args:
            self._usage(nick, "`-b <nick/hostmask> [#channel]")
            return
        channel = self._channel_arg(nick, channel, args[1:2])
        if channel is None or not self._need_op(nick, channel):
            return
        mask = args[0]
        if self.state.on_channel(channel, mask):
            mask = self._ban_mask(nick, mask)
            if mask is None:
                return
        self.bot.serv(f"MODE {channel} -b {mask}")
        self.bot.help(f"NOTICE {nick} :{notic} UnBaN {mask} ON {channel}")

    def _mass_unban(self, nick, user, channel, args) -> None:
        channel = self._channel_arg(nick, channel, args[:1])
        if channel is None or not self._need_op(nick, channel):
            return
        if self._allowed(user, "munban", nick):
            self._send_batches(channel, "-", "b", self.state.bans(channel), _UNBAN_BATCH)
            self._reply(nick, f"MuNBaNS [{channel}]")

    # Join dan part

    def _join(self, nick, name) -> None:
        channel = self._normalize(name)
        if channel is None:
            self._reply(nick, f"{scramble_case('invalid channel')}: {name}")
            return
        self.bot.chanlist.add(channel)
        self.bot.serv(f"JOIN {channel}")
        self._reply(nick, f"{scramble_case('im joining')} {channel}")

    def _part(self, nick, name) -> None:
        channel = self._normalize(name)
        if channel is None:
            self._reply(nick, f"{scramble_case('invalid channel')}: {name}")
            return
        self.bot.chanlist.remove(channel)
        self.bot.serv(f"PART {channel} :{lgrnd} {random.choice(partm)}")
        self._reply(nick, f"{scramble_case('im parting')} {channel}")

    def _pub_join(self, nick, user, channel, args) -> None:
        if not args:
            self._reply(nick, "Command: `join <#chan>")
            return
        self._join(nick, args[0])

    def _pub_part(self, nick, user, channel, args) -> None:
        if not args:
            self._reply(nick, "Command: `part <#chan>")
            return
        self._part(nick, args[0])

    def _msg_join(self, nick, user, args) -> None:
        if not args:
            self.commands.message(nick, f"Command: /msg {self.bot.nick} join <#chan>")
            return
        self._join(nick, args[0])

    def _msg_part(self, nick, user, args) -> None:
        if not args:
            self.commands.message(nick, f"Command: /msg {self.bot.nick} part <#chan>")
            return
        self._part(nick, args[0])

    def _cycle(self, nick, user, channel, args) -> None:
        self.bot.serv(f"PART {channel} :{lgrnd} {random.choice(partm)}")
        self.bot.serv(f"JOIN {channel}")

    # Mode dan topic

    def _mode(self, nick, user, channel, args) -> None:
        if not args:
            self.bot.help(f"NOTICE {nick} :{notic} Usage: `mode +/- ntspnmcilk")
            return
        if self._need_op(nick, channel):
            self.bot.serv(f"MODE {channel} {' '.join(args)}")

    def _topic(self, nick, user, channel, args) -> None:
        if not args:
            self.bot.help(f"NOTICE {nick} :{notic} Usage: `topic Welcome to {channel}")
            return
        if self._need_op(nick, channel):
            self.bot.serv(f"TOPIC {channel} :{' '.join(args)}")

    def _set_status(self, sign, letter, nick, user, channel, args) -> None:
        """`+v, `-v, `+o, `-o; tanpa argumen berlaku untuk pengirim."""
        if not self._need_op(nick, channel):
            return
        if args:
            targets = [name for name in args[:_MAX_TARGETS] if valid_handle(name)]
            if targets:
                self._send_batches(channel, sign, letter, targets)
            return
        has = self.state.has_status(channel, nick, letter)
        if has == (sign == "+"):
            label = "voiced" if letter == "v" else "oped"
            state = "already" if sign == "+" else "not"
            self._reply(nick, f"{scramble_case(f'youre {state} {label}, usage')}: `{sign}{letter} <nick>")
            return
        self.bot.serv(f"MODE {channel} {sign}{letter} {nick}")

    def _mass_voice(self, nick, user, channel, args) -> None:
        channel = self._channel_arg(nick, channel, args[:1])
        if channel is None or not self._need_op(nick, channel):
            return
        targets = [
            member for member in self.state.members(channel)
            if not self.state.is_op(channel, member)
            and not self.state.has_status(channel, member, "v")
        ]
        size = min(_MAX_TARGETS, self.state.max_modes)
        for start in range(0, len(targets), size):
            delay = random.uniform(*VOICE_DELAY)
            self.bot.later(delay, self._voice_batch, channel, targets[start:start + size])

    def _voice_batch(self, channel: str, nicks: list[str]) -> None:
        """voiceprc: keadaan bisa berubah selama menunggu, jadi periksa ulang."""
        if not self._is_op(channel):
            return
        targets = [
            nick for nick in nicks
            if self.state.on_channel(channel, nick)
            and not self.state.is_op(channel, nick)
            and not self.state.has_status(channel, nick, "v")
        ]
        self._send_batches(channel, "+", "v", targets)

    def _mass_devoice(self, nick, user, channel, args) -> None:
        channel = self._channel_arg(nick, channel, args[:1])
        if channel is None or not self._need_op(nick, channel):
            return
        targets = [
            member for member in self.state.members(channel)
            if self.state.has_status(channel, member, "v")
        ]
        self._send_batches(channel, "-", "v", targets)

    def _autovoice(self, msg: Message) -> None:
        """cvd_autofriend: voice otomatis untuk flag v."""
        if not msg.params or irc_lower(msg.nick) == irc_lower(self.bot.nick):
            return
        channel = msg.params[0]
        user = self.bot.users.match(msg.prefix.raw)
        if user is not None and "v" in user.flags and self._is_op(channel):
            self.bot.serv(f"MODE {channel} +v {msg.nick}")

    # Informasi

    def _logo(self, nick, user, channel, args) -> None:
        self.bot.quick(f"PRIVMSG {channel} :{lgrnd}")

    @staticmethod
    def _os_name() -> str:
        try:
            with open("/etc/os-release", encoding="utf-8") as file:
                for line in file:
                    if line.startswith("PRETTY_NAME="):
                        return line.partition("=")[2].strip().strip('"')
        except OSError:
            pass
        return "unknown"

    def _usage_percent(self) -> tuple[str, str]:
        cpu = 100 * time.process_time() / max(time.monotonic() - self._started, 1)
        try:
            with open("/proc/self/statm", encoding="ascii") as file:
                resident = int(file.read().split(" ")[1])
            memory = 100 * resident / os.sysconf("SC_PHYS_PAGES")
        except (OSError, ValueError, IndexError):
            return "?", f"{cpu:.1f}"
        return f"{memory:.1f}", f"{cpu:.1f}"

    def _info(self, nick, user, channel, args) -> None:
        memory, cpu = self._usage_percent()
        sc = scramble_case
        for text in (
            f"{sc('powered by')} {lgrnd} {verc} {sc('with python')} v{platform.python_version()}",
            f"{sc('running on')}\x02 {self._os_name()} \x02",
            f"{sc('procces number')}:\x02 {os.getpid()} \x02{sc('memory usage')}:"
            f"\x02 {memory}\x02%\x02 {sc('cpu usage')}:\x02 {cpu}\x02%\x02",
            "IRCv3:\x02 none/legacy server \x02",
        ):
            self.bot.help(f"PRIVMSG {channel} :{notic} {text}")

    def _ping(self, nick, user, channel, args) -> None:
        target = nick if not args or args[0].lower() == "me" else args[0]
        if not valid_handle(target):
            self._reply(nick, f"{scramble_case('invalid nick')}: {target}")
            return
        now = time.monotonic()
        self._pings = {
            key: value for key, value in self._pings.items()
            if now - value[2] < _PING_TTL
        }
        token = str(int(time.time() * 1000))
        self._pings[irc_lower(target)] = (token, nick, now)
        self.bot.quick(f"PRIVMSG {target} :\x01PING {token}\x01")

    def _ping_reply(self, msg: Message) -> None:
        """Hanya balasan untuk ping yang kita kirim; CTCP lain tidak dijawab."""
        if len(msg.params) < 2:
            return
        if msg.prefix is None or self.bot.ignores.matches(msg.prefix.raw):
            return
        match = _CTCP_PING.match(msg.params[-1])
        pending = self._pings.get(irc_lower(msg.nick))
        if match is None or pending is None or match[1] != pending[0]:
            return
        del self._pings[irc_lower(msg.nick)]
        seconds = time.monotonic() - pending[2]
        source = scramble_case("ping reply from")
        self._reply(pending[1], f"{source} {msg.nick}: {seconds:.3f} {scramble_case('detik')}")
