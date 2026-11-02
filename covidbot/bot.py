"""Loop utama bot: registrasi, dispatch pesan, dan reconnect."""

import asyncio
import logging
import random
import time
from pathlib import Path

from .config import Config, Server
from .chancmds import ChannelCommands
from .badword import BadwordFilter
from .botcmds import BotCommands
from .commands import Commands
from .connection import Connection
from .events import EventBus
from .globals import lgrnd, notic, scramble_case
from .guard import Guard
from .ignores import IgnoreCommands, IgnoreList
from .monitor import Monitor
from .netsplit import NetsplitTracker, SplitInfo
from .protocol import (
    Message,
    add_secret,
    irc_lower,
    parse_line,
    parse_modes,
    redact,
    set_casemapping,
)
from .sendqueue import HELP, QUICK, SERV, SendQueue
from .services import NickServ
from .state import NetworkState
from .storage import ChannelStore, KickCounter
from .users import UserDB

log = logging.getLogger(__name__)

_QUIT_WAIT = 2.0  # waktu menunggu server menutup koneksi setelah QUIT
_RELEASE_WAIT = 5.0  # jeda antara release dan mengambil nick kembali
_MAX_NICK_ATTEMPTS = 5


class Bot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.events = EventBus()
        self.on = self.events.on
        self.state = NetworkState()
        self.nick = config.nick
        self.wanted_nick = config.nick  # berubah lewat perintah nick
        self.realname = config.realname
        self.restart_requested = False
        self.registered = False
        self.monitor: Monitor | None = None

        timing = config.timing
        self.queue = SendQueue(timing.send_burst, timing.send_interval)
        self.netsplit = NetsplitTracker(
            timing.split_window, timing.split_timeout, self.events.emit
        )

        self._conn: Connection | None = None
        self._timers: set[asyncio.TimerHandle] = set()
        self._stop = asyncio.Event()
        self._stop_reason = ""
        self._server_index = 0
        self._backoff = timing.reconnect_min
        self._registered_at: float | None = None
        self._disconnect_reason = ""
        self._reconnect_note = ""
        self._release_nick = False
        self._nick_attempts = 0

        self.on("netsplit")(self._notify_netsplit)
        self.on("netjoin")(self._notify_netjoin)
        self.nickserv = NickServ(self)
        self.users = UserDB(config.users_file)
        self.commands = Commands(self)
        data_dir = Path(config.data_dir)
        self.chanlist = ChannelStore(data_dir / "channels.json", config.channels)
        self.kick_count = KickCounter(data_dir / "kickcount")
        self.channel_commands = ChannelCommands(self)
        self.guard = Guard(self)
        self.ignores = IgnoreList(data_dir / "ignores.json")
        self.ignore_commands = IgnoreCommands(self)
        self.badwords = BadwordFilter(self, data_dir / "badwords.db")
        self.bot_commands = BotCommands(self, data_dir / "settings.json")
        add_secret(config.owner_key)
        add_secret(config.nickpass)
        if not self.users.has_owner():
            if config.owner:
                log.info("menunggu kata kunci dari owner %s", config.owner)
            else:
                log.warning("belum ada owner; isi owner dan owner_key di config")

    # API kirim, padanan putquick / putserv / puthelp

    def quick(self, line: str) -> None:
        self.queue.put(QUICK, line)

    def serv(self, line: str) -> None:
        self.queue.put(SERV, line)

    def help(self, line: str) -> None:
        self.queue.put(HELP, line)

    @property
    def lag(self) -> float | None:
        return self.monitor.lag if self.monitor else None

    def later(self, delay: float, func, *args) -> None:
        """Jalankan nanti, padanan utimer. Batal kalau koneksi putus."""

        def fire() -> None:
            self._timers.discard(handle)
            try:
                func(*args)
            except Exception:
                log.exception("timer gagal")

        handle = asyncio.get_running_loop().call_later(delay, fire)
        self._timers.add(handle)

    def notify_admin(self, text: str) -> None:
        """Kirim NOTICE ke admin dan owner yang login, seperti cvd_notify_owner."""
        log.info("admin: %s", text)
        admins = (self.state.find_nick(admin) for admin in self.config.admins)
        targets = {nick for nick in admins if nick is not None}
        targets.update(self.commands.authed_nicks())
        for nick in sorted(targets):
            self.serv(f"NOTICE {nick} :{notic} {text}")

    def stop(self, reason: str = "", restart: bool = False) -> None:
        self._stop_reason = reason
        self.restart_requested = restart
        self._stop.set()

    # Siklus koneksi

    async def run(self) -> None:
        timing = self.config.timing
        while not self._stop.is_set():
            server = self._next_server()
            self._registered_at = None
            self._disconnect_reason = "koneksi tertutup"
            log.info("menghubungi %s:%d", server.host, server.port)
            try:
                await self._session(server)
            except OSError as exc:
                self._disconnect_reason = str(exc) or type(exc).__name__
            except Exception as exc:
                log.exception("error tak terduga pada sesi")
                self._disconnect_reason = type(exc).__name__

            if self._stop.is_set():
                break
            log.warning("terputus: %s", self._disconnect_reason)
            self.events.emit("disconnected", self._disconnect_reason)

            connected_for = (
                time.monotonic() - self._registered_at if self._registered_at else 0
            )
            if connected_for >= timing.stable_after:
                self._backoff = timing.reconnect_min
            if self._registered_at:
                self._reconnect_note = self._disconnect_reason
            await self._wait_before_reconnect()

    def _next_server(self) -> Server:
        servers = self.config.servers
        server = servers[self._server_index % len(servers)]
        self._server_index += 1
        return server

    async def _wait_before_reconnect(self) -> None:
        maximum = self.config.timing.reconnect_max
        delay = min(self._backoff * random.uniform(1, 1.25), maximum)
        self._backoff = min(self._backoff * 2, maximum)
        log.info("reconnect dalam %.1f dtk", delay)
        try:
            async with asyncio.timeout(delay):
                await self._stop.wait()
        except TimeoutError:
            pass

    async def _session(self, server: Server) -> None:
        timing = self.config.timing
        conn = Connection()
        stop_wait = asyncio.create_task(self._stop.wait())
        opening = asyncio.create_task(conn.open(server, timing.connect_timeout))
        tasks: set[asyncio.Task] = set()
        try:
            await asyncio.wait({opening, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if not opening.done():
                return
            opening.result()

            self._conn = conn
            self._register(conn, server)
            reader = asyncio.create_task(self._read_loop(conn))
            tasks = {
                reader,
                asyncio.create_task(self.queue.run(self._write_queued)),
                asyncio.create_task(self._ticker(conn)),
            }
            done, _ = await asyncio.wait(
                tasks | {stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done:
                await self._quit(conn, reader)
            for task in done - {stop_wait}:
                task.result()
        finally:
            pending = tasks | {opening, stop_wait}
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            conn.abort()
            self._reset()

    def _reset(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        self._conn = None
        self.monitor = None
        self.registered = False
        self.queue.clear()
        self.state.clear()
        self.netsplit.reset()

    def _register(self, conn: Connection, server: Server) -> None:
        self.nick = self.wanted_nick
        self._release_nick = False
        self._nick_attempts = 0
        if server.password:
            conn.write(f"PASS {server.password}")
        conn.write(f"NICK {self.nick}")
        conn.write(f"USER {self.config.user} 0 * :{self.realname}")

    async def _quit(self, conn: Connection, reader: asyncio.Task) -> None:
        reason = self._stop_reason or scramble_case("shutdown")
        conn.write(f"QUIT :{lgrnd} {reason}")
        try:
            async with asyncio.timeout(_QUIT_WAIT):
                await conn.drain()
        except (TimeoutError, OSError):
            return
        await asyncio.wait({reader}, timeout=_QUIT_WAIT)

    async def _write_queued(self, line: str) -> None:
        self._conn.write(line)
        await self._conn.drain()

    async def _read_loop(self, conn: Connection) -> None:
        while (line := await conn.read_line()) is not None:
            shown = redact(line.rstrip("\r\n"))
            log.debug("<< %s", shown)
            message = parse_line(line)
            if message is None:
                continue
            try:
                self._handle(conn, message)
            except Exception:
                log.exception("gagal memproses baris: %s", shown)

    async def _ticker(self, conn: Connection) -> None:
        timing = self.config.timing
        connected_at = time.monotonic()
        while True:
            await asyncio.sleep(timing.tick)
            if self.monitor is None:
                if time.monotonic() - connected_at >= timing.register_timeout:
                    self._disconnect_reason = "registrasi timeout"
                    conn.abort()
                    return
                continue
            self.monitor.tick()
            self.netsplit.tick()

    # Dispatch pesan

    def _handle(self, conn: Connection, msg: Message) -> None:
        handler = getattr(self, f"_on_{msg.command.lower()}", None)
        if handler is not None:
            handler(conn, msg)
        if msg.prefix and msg.prefix.user:
            self.state.set_userhost(msg.nick, msg.prefix.userhost)
        self.events.emit(msg.command, msg)

    def _on_ping(self, conn: Connection, msg: Message) -> None:
        # Langsung ke socket, tidak lewat antrean yang kena throttle.
        token = msg.params[-1] if msg.params else ""
        conn.write(f"PONG :{token}")

    def _on_pong(self, conn: Connection, msg: Message) -> None:
        if self.monitor is not None and msg.params:
            self.monitor.on_pong(msg.params[-1])

    def _on_error(self, conn: Connection, msg: Message) -> None:
        self._disconnect_reason = msg.params[-1] if msg.params else "ERROR"

    def _on_001(self, conn: Connection, msg: Message) -> None:
        timing = self.config.timing
        if msg.params:
            self.nick = msg.params[0]
        self.registered = True
        self._registered_at = time.monotonic()
        self.monitor = Monitor(
            timing.ping_interval,
            timing.lag_warn,
            timing.stoned_timeout,
            send_ping=lambda token: conn.write(f"PING :{token}"),
            on_lag=self._notify_lag,
            on_recover=self._notify_recover,
            on_stoned=lambda waited: self._handle_stoned(conn, waited),
        )
        log.info("terdaftar sebagai %s", self.nick)
        if self.config.umode:
            self.quick(f"MODE {self.nick} {self.config.umode}")
        for channel in self.chanlist.wanted():
            self.serv(f"JOIN {channel}")
        self.events.emit("connected")
        if self._release_nick:
            self._release_nick = False
            self.nickserv.release(self.wanted_nick)
            self.later(_RELEASE_WAIT, self.serv, f"NICK {self.wanted_nick}")

    def _on_432(self, conn: Connection, msg: Message) -> None:
        """Nick ditahan services. PRIVMSG belum bisa sebelum terdaftar."""
        if self.registered:
            self.nickserv.release(self.wanted_nick)
            self.wanted_nick = self.nick  # jangan diulang saat reconnect
            return
        self._release_nick = True
        self._try_alt_nick(conn)

    def _on_433(self, conn: Connection, msg: Message) -> None:
        if self.registered:
            log.warning("nick %s sedang dipakai", self.wanted_nick)
            self.notify_admin(f"{scramble_case('nick is in use')}: {self.wanted_nick}")
            self.wanted_nick = self.nick
            return
        self._try_alt_nick(conn)

    def _try_alt_nick(self, conn: Connection) -> None:
        self._nick_attempts += 1
        if self._nick_attempts > _MAX_NICK_ATTEMPTS:
            log.error("semua nick ditolak, menunggu registrasi timeout")
            return
        alt = self.config.alt_nick
        first = self._nick_attempts == 1
        self.nick = alt if first else f"{alt}{random.randrange(100, 1000)}"
        log.warning("nick dipakai, mencoba %s", self.nick)
        conn.write(f"NICK {self.nick}")

    def _on_005(self, conn: Connection, msg: Message) -> None:
        self.state.apply_isupport(list(msg.params[1:]))
        for token in msg.params[1:]:
            name, _, value = token.partition("=")
            if name == "CASEMAPPING" and not set_casemapping(value):
                log.warning("CASEMAPPING tidak dikenal: %s", value)

    def _on_352(self, conn: Connection, msg: Message) -> None:
        if len(msg.params) >= 6:
            _, _, user, host, _, nick = msg.params[:6]
            self.state.set_userhost(nick, f"{user}@{host}")

    def _on_367(self, conn: Connection, msg: Message) -> None:
        if len(msg.params) >= 3:
            self.state.set_ban(msg.params[1], msg.params[2], True)

    def _on_mode(self, conn: Connection, msg: Message) -> None:
        if len(msg.params) < 2 or msg.params[0][:1] not in ("#", "&"):
            return
        channel = msg.params[0]
        state = self.state
        changes = parse_modes(msg.params[1:], state.chanmodes, "".join(state.prefix))
        for sign, mode, arg in changes:
            if mode in state.prefix:
                if arg is not None:
                    state.set_status(channel, arg, mode, sign == "+")
            elif mode == "b":
                if arg is not None:
                    state.set_ban(channel, arg, sign == "+")
            elif mode not in state.chanmodes[0]:
                state.set_mode(channel, mode, sign == "+")
        self.events.emit("chanmode", msg, changes)

    def _on_324(self, conn: Connection, msg: Message) -> None:
        if len(msg.params) >= 3:
            state = self.state
            changes = parse_modes(msg.params[2:], state.chanmodes, "".join(state.prefix))
            for sign, mode, _ in changes:
                state.set_mode(msg.params[1], mode, sign == "+")

    def _on_353(self, conn: Connection, msg: Message) -> None:
        if len(msg.params) >= 4:
            self.state.add_names(msg.params[2], msg.params[3])

    def _on_366(self, conn: Connection, msg: Message) -> None:
        if len(msg.params) >= 2:
            self.events.emit("names_end", msg.params[1])
        # Admin baru terlihat setelah daftar NAMES selesai.
        if self._reconnect_note:
            note, self._reconnect_note = self._reconnect_note, ""
            self.notify_admin(f"{scramble_case('reconnected')} ({note})")

    def _is_me(self, nick: str) -> bool:
        return irc_lower(nick) == irc_lower(self.nick)

    def _on_join(self, conn: Connection, msg: Message) -> None:
        if not msg.params:
            return
        channel = msg.params[0]
        if self._is_me(msg.nick):
            self.state.add_channel(channel)
            self.help(f"WHO {channel}")  # 353 tidak berisi host
            self.help(f"MODE {channel} b")  # daftar ban untuk `mub
            self.help(f"MODE {channel}")  # mode channel, dibalas 324
            self.state.add_user(channel, msg.nick)
            return
        netjoin = self.netsplit.on_join(msg.nick, msg.prefix.userhost, channel)
        self.state.add_user(channel, msg.nick)
        if not netjoin:
            self.events.emit("user_join", msg)

    def _on_part(self, conn: Connection, msg: Message) -> None:
        if not msg.params:
            return
        if self._is_me(msg.nick):
            self.state.remove_channel(msg.params[0])
        else:
            self.state.remove_user(msg.params[0], msg.nick)

    def _on_kick(self, conn: Connection, msg: Message) -> None:
        if len(msg.params) < 2:
            return
        self.events.emit("KICK_PRE", msg)  # sebelum korban hilang dari state
        channel, target = msg.params[:2]
        if self._is_me(target):
            self.state.remove_channel(channel)
        else:
            self.state.remove_user(channel, target)

    def _on_quit(self, conn: Connection, msg: Message) -> None:
        channels = self.state.quit_user(msg.nick)
        reason = msg.params[-1] if msg.params else ""
        self.netsplit.on_quit(msg.nick, msg.prefix.userhost, channels, reason)

    def _on_nick(self, conn: Connection, msg: Message) -> None:
        if not msg.params:
            return
        new = msg.params[0]
        if self._is_me(msg.nick):
            self.nick = new
        self.state.rename_user(msg.nick, new)

    # Notifikasi

    def _notify_lag(self, lag: float) -> None:
        log.warning("lag tinggi: %.1f dtk", lag)
        self.events.emit("lag", lag)
        self.notify_admin(f"{scramble_case('high lag')}: {lag:.1f}s")

    def _notify_recover(self, lag: float) -> None:
        log.info("lag pulih: %.1f dtk", lag)
        self.events.emit("lag_recovered", lag)
        self.notify_admin(f"{scramble_case('lag recovered')}: {lag:.1f}s")

    def _handle_stoned(self, conn: Connection, waited: float) -> None:
        self._disconnect_reason = f"server stoned, tidak ada PONG {waited:.0f} dtk"
        self.events.emit("stoned", waited)
        conn.abort()

    def _notify_netsplit(self, info: SplitInfo) -> None:
        self.notify_admin(
            f"{scramble_case('netsplit')}: {info.servers[0]} <-> {info.servers[1]}"
            f" ({len(info.nicks)} user, {' '.join(info.channels)})"
        )

    def _notify_netjoin(self, info: SplitInfo) -> None:
        self.notify_admin(
            f"{scramble_case('netjoin')}: {info.servers[0]} <-> {info.servers[1]}"
            f" ({len(info.nicks)} user, {' '.join(info.channels)})"
        )
