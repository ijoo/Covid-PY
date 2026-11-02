"""Filter kata terlarang, port badword dari Covid.5.0.tcl."""

import logging
import re
import time
from pathlib import Path

from .globals import aktif, deaktif, scramble_case
from .protocol import Message, irc_lower, strip_formatting
from .storage import atomic_write
from .users import glob_match

log = logging.getLogger(__name__)

_WILDCARD = re.compile(r"[*?]")
_PUNCTUATION = " \t,.!?;:'\"()[]<>"
_KICK_COOLDOWN = 10.0  # satu pengirim, satu kick; baris berikutnya jangan antre
_ACTION = re.compile(r"^\x01ACTION (.*?)\x01?$")


def parse_words(text: str) -> list[str]:
    """Pisah di spasi, tapi "frasa dalam kutip" tetap satu (cvd_parse_words)."""
    words = []
    current = ""
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        elif not quoted and char in " \t":
            if current:
                words.append(current)
                current = ""
        else:
            current += char
    if current:
        words.append(current)
    return words


def find_hit(patterns: list[str], text: str) -> str | None:
    """Pola yang cocok (cvd_badword_hit).

    Pola berspasi dicocokkan pada baris, pola satu kata pada tiap kata dengan
    dan tanpa tanda baca di sekitarnya.
    """
    lowered = text.strip().lower()
    for pattern in patterns:
        stripped = pattern.strip().lower()
        if " " not in stripped:
            continue
        if _WILDCARD.search(stripped):
            length = len(stripped.split(" "))
            words = lowered.split(" ")
            for start in range(len(words) - length + 1):
                if glob_match(stripped, " ".join(words[start:start + length])):
                    return pattern
        elif stripped in lowered:
            return pattern

    single = [p for p in patterns if " " not in p.strip()]
    for word in text.split(" "):
        token = word.strip(_PUNCTUATION).lower()
        if not token:
            continue
        for pattern in single:
            lowered_pattern = pattern.strip().lower()
            if glob_match(lowered_pattern, token) or glob_match(lowered_pattern, word.lower()):
                return pattern
    return None


class BadwordFilter:
    def __init__(self, bot, path: Path) -> None:
        self.bot = bot
        self.path = path
        self.patterns: list[str] = []
        self._kicked: dict[tuple[str, str], float] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            self.patterns = [line.strip() for line in lines if line.strip()]
        except FileNotFoundError:
            pass

        commands = bot.commands
        commands.add_public("`+badword", self._plus)
        commands.add_public("`-badword", self._minus)
        commands.add_private("!badword", self._add)
        bot.on("PRIVMSG")(self._on_privmsg)

    def is_on(self, channel: str) -> bool:
        return self.bot.chanlist.has_flag(channel, "badword")

    def state_label(self, channel: str) -> str:
        return aktif if self.is_on(channel) else deaktif

    # Perintah

    def _plus(self, nick, user, channel, args) -> None:
        reply = self.bot.commands.reply
        if self.is_on(channel):
            reply(nick, f"BadWoRD FiLTeR is AlReady [{aktif}]")
            return
        self.bot.chanlist.set_flag(channel, "badword", True)
        reply(nick, f"BadWoRD FiLTeR\x02 {channel} \x02iS [{aktif}] ")

    def _minus(self, nick, user, channel, args) -> None:
        reply = self.bot.commands.reply
        if not self.is_on(channel):
            reply(nick, f"BadWoRD FiLTeR is AlReady [{deaktif}]")
            return
        self.bot.chanlist.set_flag(channel, "badword", False)
        reply(nick, f"BadWoRD FiLTeR\x02 {channel} \x02iS [{deaktif}] ")

    def _add(self, nick, user, args) -> None:
        send = self.bot.commands.message
        sc = scramble_case
        words = parse_words(" ".join(args))
        if not words:
            send(nick, f'Usage: !badword <word> [<word2> ...] '
                       f'({sc("wildcards * and ? are supported, quote phrases like")} "buy now")')
            return
        known = {pattern.lower() for pattern in self.patterns}
        added, skipped = [], []
        for word in words:
            if word.lower() in known:
                skipped.append(word)
            else:
                known.add(word.lower())
                self.patterns.append(word)
                added.append(word)
        if added:
            atomic_write(self.path, "\n".join(self.patterns) + "\n")
            send(nick, f"{sc('badword added')}: {' '.join(added)}")
        if skipped:
            send(nick, f"{sc('already in list, skipped')}: {' '.join(skipped)}")

    # Filter

    def _on_privmsg(self, msg: Message) -> None:
        """cvd_badwordcheck"""
        if len(msg.params) < 2 or msg.prefix is None:
            return
        channel, text = msg.params[0], msg.params[1]
        if channel[:1] not in ("#", "&") or not self.is_on(channel) or not self.patterns:
            return
        action = _ACTION.match(text)
        if action:
            text = action[1]
        elif text.startswith("\x01"):
            return
        if self.bot.ignores.matches(msg.prefix.raw):
            return
        # Tanpa ini kata bisa disembunyikan dengan kode warna di tengahnya.
        pattern = find_hit(self.patterns, strip_formatting(text))
        if pattern is None:
            return
        # Dicek setelah cocok: baris biasa tidak perlu melewati pencarian user.
        if self.bot.guard.exempt(msg.prefix):
            log.info('badword "%s" dari %s diabaikan, owner/friend', pattern, msg.nick)
            return
        log.warning('badword "%s" di %s dari %s', pattern, channel, msg.prefix.raw)
        self.bot.events.emit("badword", channel, msg.prefix, pattern)
        if not self.bot.state.is_op(channel, self.bot.nick):
            log.warning("bukan operator di %s, badword tidak ditindak", channel)
            return
        if self._recently_kicked(channel, msg.nick):
            return
        reason = scramble_case("forbidden word detected")
        self.bot.quick(self.bot.channel_commands.kick_line(channel, msg.nick, reason))

    def _recently_kicked(self, channel: str, nick: str) -> bool:
        now = time.monotonic()
        self._kicked = {
            key: at for key, at in self._kicked.items() if now - at < _KICK_COOLDOWN
        }
        key = (irc_lower(channel), irc_lower(nick))
        if key in self._kicked:
            return True
        self._kicked[key] = now
        return False
