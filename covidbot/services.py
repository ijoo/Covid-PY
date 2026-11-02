"""Auto-identify ke NickServ, port autoident/autoident2 dari Covid.5.0.tcl."""

import logging

from .globals import scramble_case
from .protocol import Message, irc_lower, strip_formatting

log = logging.getLogger(__name__)

# Tiap jaringan memakai kalimat berbeda (Covid.5.0.tcl baris 197-207).
_IDENTIFY_PROMPTS = (
    "this nickname is registered",
    "this nick is owned by someone else",
    "you must identify",
    "has protection enabled",
    "nickname is protected",
)
_ACCEPTED = ("password accepted",)
_REJECTED = ("password incorrect", "invalid password", "password salah")


class NickServ:
    """Satu percobaan identify per koneksi.

    Prompt yang sama bisa datang beberapa kali, dan IDENTIFY salah yang
    berulang bisa membuat bot di-kill.
    """

    def __init__(self, bot) -> None:
        self._bot = bot
        self._target = bot.config.nickserv
        self._service = irc_lower(self._target.partition("@")[0])
        self._password = bot.config.nickpass
        self._pending: tuple[str, str] | None = None
        self._attempted = False
        self.identified = False
        bot.on("connected")(self._reset)
        bot.on("NOTICE")(self._on_notice)
        bot.on("NICK")(self._on_nick)

    def _reset(self) -> None:
        self._attempted = False
        self.identified = False
        self._apply_pending()  # koneksi baru bisa langsung memakai nick baru

    def _on_notice(self, msg: Message) -> None:
        if irc_lower(msg.nick) != self._service or len(msg.params) < 2:
            return  # siapa pun bisa kirim NOTICE; hanya NickServ yang dipercaya
        if irc_lower(msg.params[0]) != irc_lower(self._bot.nick):
            return

        text = strip_formatting(msg.params[-1]).lower()
        if any(phrase in text for phrase in _ACCEPTED):
            self.identified = True
            log.info("identify berhasil")
            self._bot.events.emit("identified")
        elif any(phrase in text for phrase in _REJECTED):
            log.error("identify ditolak: %s", msg.params[-1])
            self._bot.events.emit("identify_failed")
            self._bot.notify_admin(scramble_case("identify failed, check nickpass"))
        elif any(phrase in text for phrase in _IDENTIFY_PROMPTS):
            self._identify()

    def prepare(self, nick: str, password: str) -> None:
        """Password untuk nick baru; baru berlaku kalau pergantian nick berhasil."""
        self._pending = (irc_lower(nick), password)

    def _apply_pending(self) -> None:
        if self._pending and self._pending[0] == irc_lower(self._bot.nick):
            self._password = self._pending[1]
            self._pending = None
            self._attempted = False
            self.identified = False

    def _on_nick(self, msg: Message) -> None:
        self._apply_pending()

    def identify_with(self, password: str) -> None:
        """Perintah identify manual; tidak memakai jatah percobaan otomatis."""
        self._bot.quick(f"PRIVMSG {self._target} :identify {password}")

    def release(self, nick: str) -> None:
        """raw_release: minta services melepas nick yang ditahan."""
        pending = self._pending
        password = pending[1] if pending and pending[0] == irc_lower(nick) else self._password
        if not password:
            log.warning("nick %s ditahan, tapi nickpass kosong", nick)
            return
        log.info("meminta release nick %s", nick)
        self._bot.quick(f"PRIVMSG {self._target} :release {nick} {password}")

    def _identify(self) -> None:
        if self._attempted or self.identified:
            return
        self._attempted = True
        if not self._password:
            log.warning("NickServ minta identify, tapi nickpass kosong")
            return
        log.info("identify ke %s", self._target)
        self._bot.quick(f"PRIVMSG {self._target} :identify {self._password}")
