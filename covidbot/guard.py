"""Proteksi channel dan rejoin setelah kick, port guard dari Covid.5.0.tcl.

Di eggdrop hukuman flood dijalankan eggdrop sendiri dari setelan flood-*, jadi
mesin itu ditulis di sini: flood chat, CTCP, nick, kick, dan deop berakhir kick;
join flood berakhir ban host plus kick.
"""

import logging
import re
import time

from .config import parse_rate
from .globals import aktif, deaktif, lgrnd, notic, scramble_case
from .protocol import Message, Prefix, irc_lower

log = logging.getLogger(__name__)

_SWEEP_INTERVAL = 30.0
_SERVICE_NICK = re.compile(r"serv", re.IGNORECASE)


class Guard:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.state = bot.state
        self.commands = bot.commands
        self.chanlist = bot.chanlist
        self.config = bot.config.guard

        config = self.config
        self._rates = {
            "chan": parse_rate(config.flood_chan),
            "join": parse_rate(config.flood_join),
            "ctcp": parse_rate(config.flood_ctcp),
            "kick": parse_rate(config.flood_kick),
            "deop": parse_rate(config.flood_deop),
            "nick": parse_rate(config.flood_nick),
        }
        self._floods: dict[tuple[str, str, str], list[float]] = {}
        self._join_times: dict[str, list[float]] = {}
        self._moderated: set[str] = set()
        self._op_pending: set[str] = set()
        self._unbans: dict[tuple[str, str], float] = {}
        self._rejoin_times: dict[str, list[float]] = {}
        self._rejoin_blocked: dict[str, float] = {}

        self.commands.add_public("`+guard", self._plus_guard)
        self.commands.add_public("`-guard", self._minus_guard)
        self.commands.add_public("`status", self._status)
        bot.on("KICK_PRE")(self._on_kick)
        bot.on("user_join")(self._on_join)
        bot.on("names_end")(self._on_names_end)
        bot.on("chanmode")(self._on_chanmode)
        bot.on("PRIVMSG")(self._on_text)
        bot.on("NOTICE")(self._on_text)
        bot.on("NICK")(self._on_nick)
        bot.on("connected")(self._on_connected)
        bot.on("disconnected")(self._on_disconnected)

    # Bantuan

    def is_on(self, channel: str) -> bool:
        return self.chanlist.has_flag(channel, "guard")

    def _is_op(self, channel: str) -> bool:
        return self.state.is_op(channel, self.bot.nick)

    @staticmethod
    def _is_service(prefix: Prefix) -> bool:
        """Server, atau nick *Serv* dari host services.*.

        Nick saja tidak cukup: siapa pun bisa bernama FakeServ.
        """
        if not prefix.user:
            return True
        return bool(_SERVICE_NICK.search(prefix.nick)) and prefix.host.lower().startswith(
            "services."
        )

    def exempt(self, prefix: Prefix | None) -> bool:
        """Bot, layanan, owner yang login, dan friend tidak dihukum."""
        if prefix is None or irc_lower(prefix.nick) == irc_lower(self.bot.nick):
            return True
        if self._is_service(prefix):
            return True
        user = self.commands.identify_prefix(prefix)
        return user is not None and (self.commands.is_authed(user) or "f" in user.flags)

    def _flooded(self, channel: str, kind: str, host: str) -> bool:
        limit, window = self._rates[kind]
        key = (irc_lower(channel), kind, host.lower())
        now = time.monotonic()
        times = [t for t in self._floods.get(key, ()) if now - t < window]
        times.append(now)
        if len(times) > limit:
            self._floods.pop(key, None)
            return True
        self._floods[key] = times
        return False

    def _punish(self, channel: str, prefix: Prefix, kind: str) -> None:
        log.warning("flood %s dari %s di %s", kind, prefix.raw, channel)
        self.bot.events.emit("flood", channel, kind, prefix)
        if not self._is_op(channel):
            log.warning("bukan operator di %s, flood tidak ditindak", channel)
            return
        if kind == "join":
            mask = f"*!*@{prefix.host}"
            channel_commands = self.bot.channel_commands
            if not channel_commands.hits_protected(mask, channel):
                self.bot.quick(f"MODE {channel} +b {mask}")
                self._unbans[(channel, mask)] = (
                    time.monotonic() + self.config.ban_time * 60
                )
        if self.state.on_channel(channel, prefix.nick):
            reason = scramble_case(f"{kind} flood detected")
            self.bot.quick(
                self.bot.channel_commands.kick_line(channel, prefix.nick, reason)
            )

    def _count(self, channel: str, prefix: Prefix, kind: str) -> None:
        if self.is_on(channel) and not self.exempt(prefix):
            if self._flooded(channel, kind, prefix.host):
                self._punish(channel, prefix, kind)

    # Perintah

    def _plus_guard(self, nick, user, channel, args) -> None:
        if self.is_on(channel):
            self.commands.reply(nick, f"PrOTeksI ChanNel is AlReady [{aktif}]")
            return
        self.chanlist.set_flag(channel, "guard", True)
        self.commands.reply(nick, f"PrOTeksI ChanNel\x02 {channel} \x02iS [{aktif}] ")
        self.ensure_op(channel)

    def _minus_guard(self, nick, user, channel, args) -> None:
        if not self.is_on(channel):
            self.commands.reply(nick, f"PrOTeksI ChanNel is AlReady [{deaktif}]")
            return
        self.chanlist.set_flag(channel, "guard", False)
        self.commands.reply(nick, f"PrOTeksI ChanNel\x02 {channel} \x02iS [{deaktif}] ")

    def _status(self, nick, user, channel, args) -> None:
        sc = scramble_case
        say = lambda text: self.bot.help(f"PRIVMSG {channel} :{notic} {text}")
        badword = f"{sc('badword filter')} [{self.bot.badwords.state_label(channel)}]"
        if not self.is_on(channel):
            say(f"{sc('channel protection is currently')} [{deaktif}]"
                f" - \x1f`+guard\x1f {sc('to activate')}")
            say(sc("nothing is protecting this channel right now"))
            say(badword)
            return
        config = self.config
        parts = [
            f"[F]{sc('lood-protect')}",
            f"[D]{sc('e@p-revenge')}",
            f"[K]{sc('ick-revenge')}",
            f"[J]{sc('oin-burst')} {config.automod_max}:{config.automod_window}"
            f"/{config.automod_duration}s",
        ]
        if irc_lower(channel) in self._moderated:
            parts.append(f"[!]{sc('moderated now')}")
        say(f"{sc('channel protection for')} {channel} [{aktif}]: {' '.join(parts)}")
        say(
            f"{sc('flood limits')}: {config.flood_chan} {sc('chan')},"
            f" {config.flood_join} {sc('join')}, {config.flood_ctcp} {sc('ctcp')},"
            f" {config.flood_kick} {sc('kick')}, {config.flood_deop} {sc('deop')},"
            f" {config.flood_nick} {sc('nick')}"
        )
        say(badword)
        if self._is_op(channel):
            say(f"{sc('i am operator here')} [{aktif}]")
        else:
            say(f"{sc('i am not operator here, guard will switch itself off')}"
                f" [{deaktif}] - \x1f{config.opcmd}\x1f")

    # Memastikan op

    def ensure_op(self, channel: str) -> None:
        """cvd_guard_ensure_op: minta op, matikan guard kalau tidak didapat."""
        key = irc_lower(channel)
        if not self.is_on(channel) or self._is_op(channel) or key in self._op_pending:
            return
        self._op_pending.add(key)
        log.info("+guard aktif di %s tapi bot belum op, meminta op", channel)
        request = self.config.opcmd.replace("%c%", channel).replace("%n%", self.bot.nick)
        self.bot.serv(request)
        self.bot.later(self.config.op_grace, self._op_check, channel)

    def _op_check(self, channel: str) -> None:
        self._op_pending.discard(irc_lower(channel))
        if not self.is_on(channel) or self._is_op(channel):
            return
        if not self.state.has_channel(channel):
            return  # sedang di luar channel; diperiksa lagi saat masuk
        self.chanlist.set_flag(channel, "guard", False)
        log.warning("gagal mendapat op di %s, +guard dimatikan", channel)
        sc = scramble_case
        self.bot.notify_admin(
            f"{sc('guard auto-disabled on')} {channel}"
            f" - {sc('im not operator there, protection would be useless')}"
        )

    def _on_names_end(self, channel: str) -> None:
        # Sesudah 366 status bot sudah pasti; saat JOIN op belum diketahui.
        self.ensure_op(channel)

    # Kick dan rejoin

    def _on_kick(self, msg: Message) -> None:
        """Dipanggil sebelum state berubah, jadi korban masih bisa dikenali."""
        if len(msg.params) < 2 or msg.prefix is None:
            return
        channel, target = msg.params[:2]
        if irc_lower(target) == irc_lower(self.bot.nick):
            self._kicked(channel, msg)
            return
        if not self.is_on(channel) or self.exempt(msg.prefix):
            return
        if irc_lower(msg.nick) == irc_lower(target):
            return
        if self._flooded(channel, "kick", msg.prefix.host):
            self._punish(channel, msg.prefix, "kick")
            return
        if self.bot.channel_commands.protected(target) is None:
            return
        if self._is_op(channel):
            log.warning("%s menendang %s di %s, dibalas", msg.nick, target, channel)
            reason = scramble_case("unauthorized kick detected")
            self.bot.quick(f"KICK {channel} {msg.nick} :{lgrnd} {reason}")
        else:
            log.warning("%s menendang %s di %s, bot bukan op", msg.nick, target, channel)

    def _kicked(self, channel: str, msg: Message) -> None:
        user = self.commands.identify_prefix(msg.prefix)
        if self.commands.is_authed(user):
            log.info("ditendang dari %s oleh owner %s, tidak rejoin", channel, msg.nick)
            return
        key = irc_lower(channel)
        config = self.config
        now = time.monotonic()
        if now < self._rejoin_blocked.get(key, 0):
            log.info("ditendang lagi dari %s, rejoin masih cooldown", channel)
            return
        times = [t for t in self._rejoin_times.get(key, ()) if now - t <= config.rejoin_window]
        times.append(now)
        self._rejoin_times[key] = times
        if len(times) > config.rejoin_max:
            self._rejoin_blocked[key] = now + config.rejoin_cooldown
            log.warning(
                "terlalu sering ditendang dari %s, berhenti %ss",
                channel, config.rejoin_cooldown,
            )
            return
        log.info("ditendang dari %s oleh %s, rejoin %ss lagi",
                 channel, msg.nick, config.rejoin_delay)
        self.bot.later(config.rejoin_delay, self._rejoin, channel)

    def _rejoin(self, channel: str) -> None:
        wanted = {name.lower() for name in self.chanlist.wanted()}
        if channel.lower() in wanted and not self.state.has_channel(channel):
            self.bot.serv(f"JOIN {channel}")

    # Mode

    def _on_chanmode(self, msg: Message, changes: list) -> None:
        """cvd_modeprotect ditambah hitungan flood-deop."""
        channel = msg.params[0]
        if msg.prefix is None or not self.is_on(channel) or self.exempt(msg.prefix):
            return
        for sign, mode, target in changes:
            if sign != "-" or mode != "o" or target is None:
                continue
            if irc_lower(target) == irc_lower(msg.nick):
                continue
            if self._flooded(channel, "deop", msg.prefix.host):
                self._punish(channel, msg.prefix, "deop")
                return
            if irc_lower(target) == irc_lower(self.bot.nick):
                log.warning("%s mencabut op bot di %s", msg.nick, channel)
                self.ensure_op(channel)
            elif self.bot.channel_commands.protected(target) is not None:
                self._revenge_deop(channel, msg, target)

    def _revenge_deop(self, channel: str, msg: Message, target: str) -> None:
        if not self._is_op(channel):
            log.warning("%s mencabut op %s di %s, bot bukan op", msg.nick, target, channel)
            return
        log.warning("%s mencabut op %s di %s, dibalas", msg.nick, target, channel)
        reason = scramble_case("unauthorized deop detected")
        self.bot.quick(f"KICK {channel} {msg.nick} :{lgrnd} {reason}")
        self.bot.quick(f"MODE {channel} +o {target}")

    # Trafik

    def _on_text(self, msg: Message) -> None:
        if len(msg.params) < 2 or msg.prefix is None or msg.params[0][:1] not in ("#", "&"):
            return
        text = msg.params[-1]
        is_ctcp = text.startswith("\x01") and not text.startswith("\x01ACTION")
        self._count(msg.params[0], msg.prefix, "ctcp" if is_ctcp else "chan")

    def _on_nick(self, msg: Message) -> None:
        if msg.prefix is None or not msg.params:
            return
        for channel in self.state.channels_of(msg.params[0]):
            prefix = Prefix(msg.prefix.raw, msg.params[0], msg.prefix.user, msg.prefix.host)
            self._count(channel, prefix, "nick")

    def _on_join(self, msg: Message) -> None:
        """Hanya join sungguhan; netjoin tidak lewat sini."""
        channel = msg.params[0]
        self._count(channel, msg.prefix, "join")
        self._join_burst(channel)

    def _join_burst(self, channel: str) -> None:
        """cvd_joinburst: banyak join sekaligus membuat channel +m sementara."""
        key = irc_lower(channel)
        if not self.is_on(channel) or not self._is_op(channel) or key in self._moderated:
            return
        config = self.config
        now = time.monotonic()
        times = [t for t in self._join_times.get(key, ()) if now - t <= config.automod_window]
        times.append(now)
        self._join_times[key] = times
        if len(times) <= config.automod_max or self.state.has_mode(channel, "m"):
            return
        self._moderated.add(key)
        self._join_times.pop(key, None)
        self.bot.quick(f"MODE {channel} +m")
        log.warning("join burst di %s, +m selama %ss", channel, config.automod_duration)
        self.bot.later(config.automod_duration, self._unmoderate, channel)

    def _unmoderate(self, channel: str) -> None:
        self._moderated.discard(irc_lower(channel))
        if self._is_op(channel) and self.state.has_mode(channel, "m"):
            self.bot.quick(f"MODE {channel} -m")

    # Pemeliharaan

    def _on_connected(self) -> None:
        self.bot.later(_SWEEP_INTERVAL, self._sweep)

    def _on_disconnected(self, reason: str) -> None:
        # Timer sudah dibatalkan bot, jadi penanda yang menunggunya ikut dihapus.
        self._op_pending.clear()
        self._moderated.clear()
        self._floods.clear()
        self._join_times.clear()

    def _sweep(self) -> None:
        """Cabut ban yang sudah kedaluwarsa dan buang hitungan flood lama."""
        now = time.monotonic()
        for (channel, mask), expires in list(self._unbans.items()):
            if now >= expires and self._is_op(channel):
                del self._unbans[(channel, mask)]
                if mask in self.state.bans(channel):
                    self.bot.serv(f"MODE {channel} -b {mask}")
        longest = max(window for _, window in self._rates.values())
        self._floods = {
            key: times for key, times in self._floods.items()
            if times and now - times[-1] < longest
        }
        self.bot.later(_SWEEP_INTERVAL, self._sweep)
