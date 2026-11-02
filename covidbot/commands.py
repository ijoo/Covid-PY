"""Perintah auth dan userlist, port msg_* dan pub_* dari Covid.5.0.tcl.

Owner dikenali dari handle + password, bukan host, karena host bisa berubah.
Akibatnya password adalah satu-satunya pelindung: sesi diikat ke user@host saat
login, percobaan dibatasi per host, dan kata kunci hanya berlaku sampai owner
pertama selesai dibuat.

Pengirim tanpa hak tidak mendapat balasan apa pun, sama seperti bind eggdrop.
"""

import asyncio
import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .globals import notic, scramble_case, tolak, tuan
from .protocol import Message, Prefix, irc_lower
from .users import MIN_PASSWORD, User, check_password, hash_password, valid_handle

log = logging.getLogger(__name__)

_BOTNAME_COMMANDS = {  # pub_botcmd: <botnick> <perintah>, termasuk aliasnya
    "auth": "`auth", "deauth": "`deauth", "cycle": "`cycle", "ver": "`ver",
    "+v": "`+v", "-v": "`-v", "dv": "`-v", "+o": "`+o", "-o": "`-o", "do": "`-o",
    "mv": "`mv", "mdv": "`mdv", "k": "`k", "kb": "`kb", "b": "`+b", "+b": "`+b",
    "-b": "`-b", "ub": "`-b", "mub": "`mub",
    "+guard": "`+guard", "-guard": "`-guard", "status": "`status",
    "+badword": "`+badword", "-badword": "`-badword",
}
_MAX_PER_HOST = 5
_MAX_GLOBAL = 30
_ATTEMPT_WINDOW = 300.0
_PENDING_TTL = 300.0
_NOTICE_CHUNK = 380


class _Attempts:
    """Batas percobaan gagal.

    Dihitung per host, bukan per handle: kalau per handle, siapa pun bisa
    memakai nick owner lalu sengaja gagal untuk mengunci owner. Batas global
    menahan tebakan yang tersebar dari banyak host.
    """

    def __init__(self) -> None:
        self._hosts: dict[str, list[float]] = {}
        self._all: list[float] = []
        self._warned: dict[str, float] = {}

    def _prune(self, now: float) -> None:
        fresh = {}
        for host, times in self._hosts.items():
            times = [t for t in times if now - t < _ATTEMPT_WINDOW]
            if times:
                fresh[host] = times
        self._hosts = fresh
        self._all = [t for t in self._all if now - t < _ATTEMPT_WINDOW]

    def blocked(self, host: str) -> bool:
        self._prune(time.monotonic())
        return (
            len(self._hosts.get(host, ())) >= _MAX_PER_HOST
            or len(self._all) >= _MAX_GLOBAL
        )

    def record(self, host: str) -> None:
        now = time.monotonic()
        self._hosts.setdefault(host, []).append(now)
        self._all.append(now)

    def clear(self, host: str) -> None:
        self._hosts.pop(host, None)

    def should_warn(self, host: str) -> bool:
        """Sekali per jendela: pemberitahuan jangan jadi alat banjir antrean."""
        now = time.monotonic()
        self._warned = {
            key: at for key, at in self._warned.items() if now - at < _ATTEMPT_WINDOW
        }
        if host in self._warned:
            return False
        self._warned[host] = now
        return True


@dataclass
class _Session:
    nick: str
    handle: str
    userhost: str


@dataclass
class _PendingOwner:
    nick: str
    userhost: str
    expires: float


class Commands:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.users = bot.users
        self._sessions: dict[str, _Session] = {}  # lower nick -> sesi
        self._pending: _PendingOwner | None = None
        self._attempts = _Attempts()
        self._public: dict[str, tuple[Callable, bool]] = {
            # handler, butuh login (flag Z)
            "`auth": (self._pub_auth, False),
            "`deauth": (self._pub_deauth, True),
            "`+f": (lambda *a: self._add_flag("f", "friend list", *a), True),
            "`-f": (lambda *a: self._del_flag("f", "friend list", *a), True),
            "`+av": (lambda *a: self._add_flag("v", "autovoice list", *a), True),
            "`-av": (lambda *a: self._del_flag("v", "autovoice list", *a), True),
            "`deluser": (self._pub_deluser, True),
            "`userlist": (self._pub_userlist, True),
        }
        self._private_commands: dict[str, Callable] = {}
        bot.on("PRIVMSG")(self._on_privmsg)
        bot.on("QUIT")(self._on_quit)
        bot.on("NICK")(self._on_nick)
        bot.on("disconnected")(self._on_disconnected)

    def add_public(self, name: str, handler: Callable) -> None:
        """Perintah channel; hanya untuk yang sudah login."""
        self._public[name] = (handler, True)

    def add_private(self, name: str, handler: Callable) -> None:
        """Perintah /msg; hanya untuk yang sudah login."""
        self._private_commands[name] = handler

    # Sesi

    def is_authed(self, user: User | None) -> bool:
        if user is None:
            return False
        handle = user.handle.lower()
        return any(s.handle.lower() == handle for s in self._sessions.values())

    def authed_nicks(self) -> list[str]:
        """Nick terlihat yang sudah login, untuk cvd_notify_owner."""
        return [
            session.nick
            for session in self._sessions.values()
            if self.bot.state.find_nick(session.nick) is not None
        ]

    def identify_nick(self, nick: str) -> User | None:
        """User di balik nick terlihat, dari sesi atau host, bukan nama nick."""
        userhost = self.bot.state.find_userhost(nick)
        if userhost is None:
            return None
        return self._identify(nick, userhost)

    def identify_prefix(self, prefix: Prefix) -> User | None:
        """Sama seperti identify_nick, untuk pengirim yang bisa sudah keluar."""
        return self._identify(prefix.nick, prefix.userhost)

    def _identify(self, nick: str, userhost: str) -> User | None:
        session = self._sessions.get(irc_lower(nick))
        if session is not None and session.userhost == userhost:
            return self.users.get(session.handle)
        return self.users.match(f"{nick}!{userhost}")

    def _session_user(self, msg: Message) -> User | None:
        """User yang login, hanya kalau user@host masih sama seperti saat login."""
        session = self._sessions.get(irc_lower(msg.nick))
        if session is None or session.userhost != msg.prefix.userhost:
            return None
        return self.users.get(session.handle)

    def _login(self, msg: Message, handle: str) -> None:
        self._sessions[irc_lower(msg.nick)] = _Session(
            msg.nick, handle, msg.prefix.userhost
        )
        log.info("%s login sebagai %s", msg.nick, handle)

    def _logout(self, handle: str) -> None:
        self._sessions = {
            key: session
            for key, session in self._sessions.items()
            if session.handle.lower() != handle.lower()
        }

    def _on_quit(self, msg: Message) -> None:
        self._sessions.pop(irc_lower(msg.nick), None)

    def _on_nick(self, msg: Message) -> None:
        session = self._sessions.pop(irc_lower(msg.nick), None)
        if session is not None and msg.params:
            session.nick = msg.params[0]
            self._sessions[irc_lower(session.nick)] = session

    def _on_disconnected(self, reason: str) -> None:
        # Selama terputus QUIT tidak terlihat, jadi sesi tidak bisa dipercaya.
        self._sessions.clear()
        self._pending = None

    # Routing

    def reply(self, nick: str, text: str) -> None:
        self.bot.quick(f"NOTICE {nick} :{notic} {text}")

    def message(self, nick: str, text: str) -> None:
        self.bot.quick(f"PRIVMSG {nick} :{notic} {text}")

    async def _on_privmsg(self, msg: Message) -> None:
        if len(msg.params) < 2 or msg.prefix is None or not msg.prefix.user:
            return
        if irc_lower(msg.nick) == irc_lower(self.bot.nick):
            return
        words = [word for word in msg.params[1].split(" ") if word]
        if not words:
            return
        target = msg.params[0]
        if irc_lower(target) == irc_lower(self.bot.nick):
            await self._private(msg, words)
        elif target[:1] in ("#", "&"):
            self._channel(msg, target, words)

    async def _private(self, msg: Message, words: list[str]) -> None:
        command, args = words[0].lower(), words[1:]
        if command == "auth":
            await self._msg_auth(msg, args)
        elif command == "deauth":
            self._msg_deauth(msg)
        elif command == "setpass":
            await self._setpass(msg, args)
        elif command in self._private_commands:
            user = self._session_user(msg)
            if user is not None:
                self._private_commands[command](msg.nick, user, args)
        elif not args:
            self._owner_key(msg, words[0])

    def _channel(self, msg: Message, channel: str, words: list[str]) -> None:
        command, args = words[0].lower(), words[1:]
        if irc_lower(words[0]) == irc_lower(self.bot.nick):
            if not args:
                return
            word, args = args[0].lower(), args[1:]
            command = _BOTNAME_COMMANDS.get(word)
            if command is None:
                if self._session_user(msg) is not None:
                    self.bot.help(
                        f"NOTICE {msg.nick} :This \x02`{word}\x02 CaNNot UsiNg BotName"
                    )
                return
        entry = self._public.get(command)
        if entry is None:
            return
        handler, needs_auth = entry
        user = self._session_user(msg)
        if user is None and self.bot.ignores.matches(msg.prefix.raw):
            return  # /msg tidak dicek: ignore tidak boleh mengunci owner dari auth
        if user is None and (needs_auth or not self._is_owner(msg.nick)):
            return
        handler(msg.nick, user, channel, args)

    def _is_owner(self, handle: str) -> bool:
        user = self.users.get(handle)
        return user is not None and "n" in user.flags

    # Owner pertama

    def _owner_key(self, msg: Message, text: str) -> None:
        config = self.bot.config
        if self.users.has_owner() or not config.owner:
            return  # kata kunci mati setelah owner pertama ada
        if irc_lower(msg.nick) != irc_lower(config.owner):
            return
        host = msg.prefix.host
        if self._attempts.blocked(host):
            return
        if not hmac.compare_digest(text.encode(), config.owner_key.encode()):
            self._attempts.record(host)
            log.warning("kata kunci salah dari %s", msg.nick)
            return
        self._attempts.clear(host)
        self._pending = _PendingOwner(
            irc_lower(msg.nick), msg.prefix.userhost, time.monotonic() + _PENDING_TTL
        )
        log.info("%s mengirim kata kunci owner", msg.nick)
        self.message(
            msg.nick,
            f"{scramble_case('hello owner, set your password')}: "
            f"/msg {self.bot.nick} setpass <password>",
        )

    async def _setpass(self, msg: Message, args: list[str]) -> None:
        pending = self._pending
        if (
            pending is None
            or time.monotonic() >= pending.expires
            or pending.nick != irc_lower(msg.nick)
            or pending.userhost != msg.prefix.userhost
        ):
            return
        if len(args) != 1 or len(args[0]) < MIN_PASSWORD:
            self.message(msg.nick, f"Command: setpass <password> (min {MIN_PASSWORD}, no spaces)")
            return
        self._pending = None
        hashed = await asyncio.to_thread(hash_password, args[0])
        if self.users.has_owner():
            return
        handle = self.bot.config.owner
        self.users.add(handle, flags="nf").password = hashed
        self.users.save()
        self._login(msg, handle)
        self.message(msg.nick, f"{scramble_case('password saved, access')} {tuan}")

    # Auth

    async def _msg_auth(self, msg: Message, args: list[str]) -> None:
        nick = msg.nick
        if self._session_user(msg) is not None:
            self.reply(nick, f"{scramble_case('already auth with access')}: {tuan}")
            return
        if not args:
            if self._is_owner(nick):
                self.reply(nick, f"Command: /msg {self.bot.nick} auth <password>")
            return
        handle, password = (args[0], args[1]) if len(args) >= 2 else (nick, args[0])
        if not self._is_owner(handle):
            log.warning("%s mencoba auth tanpa hak owner", nick)
            return
        host = msg.prefix.host
        if self._attempts.blocked(host):
            log.warning("auth dari %s diblokir sementara", nick)
            if self._attempts.should_warn(host):
                self.reply(nick, scramble_case("too many attempts, please wait"))
            return

        # Dicatat sebelum hashing supaya percobaan paralel tidak lolos dari batas.
        self._attempts.record(host)
        user = self.users.get(handle)
        if await asyncio.to_thread(check_password, user.password, password):
            self._attempts.clear(host)
            self._login(msg, user.handle)
            self.message(nick, f"{scramble_case('access')} {tuan}")
        else:
            log.warning("password salah dari %s (%s)", nick, user.handle)
            self.message(nick, f"{scramble_case('wrong password')}!")

    def _msg_deauth(self, msg: Message) -> None:
        user = self._session_user(msg)
        if user is None:
            if self._is_owner(msg.nick):
                self.reply(msg.nick, f"{scramble_case('please auth 1st')}!")
            return
        self._logout(user.handle)
        self.message(msg.nick, f"{scramble_case('successfully deauth')}!")

    def _pub_auth(self, nick, user, channel, args) -> None:
        status = tuan if user is not None else tolak
        self.bot.quick(f"PRIVMSG {channel} :{nick}, {status}")

    def _pub_deauth(self, nick, user, channel, args) -> None:
        self._logout(user.handle)
        self.reply(nick, f"{scramble_case('successfully deauth')}!")

    # Userlist

    def _resolve(self, name: str) -> User | None:
        """Nick atau handle menjadi user terdaftar (cvd_gethand)."""
        found = self.users.get(name)
        if found is not None:
            return found
        userhost = self.bot.state.find_userhost(name)
        return self.users.match(f"{name}!{userhost}") if userhost else None

    def _add_flag(self, flag, label, nick, user, channel, args) -> None:
        if not args:
            command = "+f" if flag == "f" else "+av"
            self.reply(nick, f"Command: `{command} <nick>")
            return
        name = args[0]
        target = self._resolve(name)
        if target is None:
            userhost = self.bot.state.find_userhost(name)
            if userhost is None or not valid_handle(name):
                self.reply(
                    nick, f"{name} - {scramble_case('not a known user and not in')} {channel}"
                )
                return
            # Bukan nick!*@* seperti TCL: mask itu cocok untuk siapa saja yang
            # kebetulan memakai nick tersebut.
            mask = f"*!*@{userhost.partition('@')[2]}"
            target = self.users.add(name, mask)
        elif flag in target.flags:
            self.reply(
                nick, f"\x02{target.handle}\x02 {scramble_case(f'already on {label}')}"
            )
            return
        target.flags.add(flag)
        self.users.save()
        added = scramble_case(f"is successfully \x1fadded\x1f to {label}")
        self.bot.help(f"NOTICE {nick} :{notic} \x02{target.handle}\x02 {added}!")

    def _del_flag(self, flag, label, nick, user, channel, args) -> None:
        if not args:
            command = "-f" if flag == "f" else "-av"
            self.reply(nick, f"Command: `{command} <nick>")
            return
        target = self._resolve(args[0])
        if target is None or flag not in target.flags:
            shown = target.handle if target else args[0]
            self.reply(nick, f"\x02{shown}\x02 {scramble_case(f'not in {label}')}")
            return
        target.flags.discard(flag)
        self.users.save()
        removed = scramble_case(f"is successfully \x1fdelete\x1f from {label}")
        self.bot.help(f"NOTICE {nick} :{notic} \x02{target.handle}\x02 {removed}!")

    def _pub_deluser(self, nick, user, channel, args) -> None:
        if not args:
            self.reply(nick, f"{scramble_case('invalid handle')}!")
            return
        target = self._resolve(args[0])
        if target is None:
            self.reply(
                nick, f"{args[0]} {scramble_case('is not available database list')}!"
            )
            return
        if "n" in target.flags:
            self.reply(nick, f"{scramble_case('cannot delete own owner')}!")
            return
        self.users.remove(target.handle)
        self.users.save()
        removed = scramble_case("is successfully \x1fdelete\x1f from database list")
        self.reply(nick, f"{target.handle} {removed}!")

    def _pub_userlist(self, nick, user, channel, args) -> None:
        entries = []
        for item in self.users:
            flags = "".join(sorted(item.flags)) + ("Z" if self.is_authed(item) else "")
            entries.append(f"\x034,1 {item.handle}\x030,1 ({flags or '-'}) \x03")
        chunk = f"{scramble_case('userlist')}: "
        for entry in entries:
            if len(chunk.encode()) + len(entry.encode()) > _NOTICE_CHUNK:
                self.bot.help(f"NOTICE {nick} :{notic} {chunk}")
                chunk = ""
            chunk += entry + " "
        self.bot.help(f"NOTICE {nick} :{notic} {chunk}")
