"""State channel dan nick yang terlihat oleh bot."""

from dataclasses import dataclass, field

from .protocol import irc_lower

_OP_MODES = {"q", "a", "o"}


@dataclass
class Channel:
    name: str
    users: dict[str, str] = field(default_factory=dict)  # lower -> nick asli
    status: dict[str, set[str]] = field(default_factory=dict)  # lower -> huruf mode
    bans: list[str] = field(default_factory=list)
    modes: set[str] = field(default_factory=set)


class NetworkState:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self._hosts: dict[str, str] = {}  # lower -> user@host, nick terlihat saja
        # Nilai awal dari ISUPPORT; diganti saat 005 datang.
        self.prefix = {"q": "~", "a": "&", "o": "@", "h": "%", "v": "+"}
        self.chanmodes = ("beI", "k", "l", "imnpst")
        self.max_modes = 6

    def clear(self) -> None:
        self.channels.clear()
        self._hosts.clear()

    def add_channel(self, name: str) -> None:
        self.channels.setdefault(irc_lower(name), Channel(name))

    def remove_channel(self, name: str) -> None:
        self.channels.pop(irc_lower(name), None)
        self._hosts = {
            key: host for key, host in self._hosts.items() if self.find_nick(key)
        }

    def has_channel(self, name: str) -> bool:
        return irc_lower(name) in self.channels

    def on_channel(self, channel: str, nick: str) -> bool:
        chan = self.channels.get(irc_lower(channel))
        return chan is not None and irc_lower(nick) in chan.users

    def members(self, channel: str) -> list[str]:
        chan = self.channels.get(irc_lower(channel))
        return list(chan.users.values()) if chan else []

    def bans(self, channel: str) -> list[str]:
        chan = self.channels.get(irc_lower(channel))
        return list(chan.bans) if chan else []

    def has_status(self, channel: str, nick: str, mode: str) -> bool:
        chan = self.channels.get(irc_lower(channel))
        return chan is not None and mode in chan.status.get(irc_lower(nick), ())

    def has_mode(self, channel: str, mode: str) -> bool:
        chan = self.channels.get(irc_lower(channel))
        return chan is not None and mode in chan.modes

    def set_mode(self, channel: str, mode: str, active: bool) -> None:
        chan = self.channels.get(irc_lower(channel))
        if chan is not None:
            (chan.modes.add if active else chan.modes.discard)(mode)

    def channels_of(self, nick: str) -> list[str]:
        key = irc_lower(nick)
        return [chan.name for chan in self.channels.values() if key in chan.users]

    def is_op(self, channel: str, nick: str) -> bool:
        """Owner dan admin (~ &) juga operator, walau tanpa @."""
        return any(self.has_status(channel, nick, mode) for mode in _OP_MODES)

    def set_status(self, channel: str, nick: str, mode: str, active: bool) -> None:
        chan = self.channels.get(irc_lower(channel))
        if chan is None:
            return
        modes = chan.status.setdefault(irc_lower(nick), set())
        if active:
            modes.add(mode)
        else:
            modes.discard(mode)

    def set_ban(self, channel: str, mask: str, active: bool) -> None:
        chan = self.channels.get(irc_lower(channel))
        if chan is None:
            return
        if active and mask not in chan.bans:
            chan.bans.append(mask)
        elif not active and mask in chan.bans:
            chan.bans.remove(mask)

    def apply_isupport(self, tokens: list[str]) -> None:
        for token in tokens:
            name, _, value = token.partition("=")
            if name == "PREFIX" and ")" in value:
                modes, _, chars = value[1:].partition(")")
                self.prefix = dict(zip(modes, chars))
            elif name == "CHANMODES" and value.count(",") >= 3:
                self.chanmodes = tuple(value.split(",")[:4])
            elif name == "MODES" and value.isdigit() and int(value) > 0:
                self.max_modes = int(value)

    def set_userhost(self, nick: str, userhost: str) -> None:
        """Catat host nick; nick yang tidak terlihat diabaikan agar tak menumpuk."""
        if self.find_nick(nick) is not None:
            self._hosts[irc_lower(nick)] = userhost

    def find_userhost(self, nick: str) -> str | None:
        return self._hosts.get(irc_lower(nick))

    def visible_nicks(self) -> set[str]:
        return {nick for chan in self.channels.values() for nick in chan.users.values()}

    def add_user(self, channel: str, nick: str) -> None:
        chan = self.channels.get(irc_lower(channel))
        if chan is not None:
            chan.users[irc_lower(nick)] = nick

    def remove_user(self, channel: str, nick: str) -> None:
        chan = self.channels.get(irc_lower(channel))
        if chan is not None:
            chan.users.pop(irc_lower(nick), None)
            chan.status.pop(irc_lower(nick), None)
        if self.find_nick(nick) is None:
            self._hosts.pop(irc_lower(nick), None)

    def add_names(self, channel: str, names: str) -> None:
        """Isi dari balasan 353."""
        modes_by_char = {char: mode for mode, char in self.prefix.items()}
        for name in names.split(" "):
            nick = name.lstrip("".join(modes_by_char))
            if nick:
                self.add_user(channel, nick)
                for char in name[:len(name) - len(nick)]:
                    self.set_status(channel, nick, modes_by_char[char], True)

    def quit_user(self, nick: str) -> list[str]:
        """Hapus nick dari semua channel, kembalikan channel yang dia tempati."""
        key = irc_lower(nick)
        self._hosts.pop(key, None)
        left = []
        for chan in self.channels.values():
            chan.status.pop(key, None)
            if chan.users.pop(key, None) is not None:
                left.append(chan.name)
        return left

    def rename_user(self, old: str, new: str) -> None:
        old_key = irc_lower(old)
        for chan in self.channels.values():
            if chan.users.pop(old_key, None) is not None:
                chan.users[irc_lower(new)] = new
            if old_key in chan.status:
                chan.status[irc_lower(new)] = chan.status.pop(old_key)
        if old_key in self._hosts:
            self._hosts[irc_lower(new)] = self._hosts.pop(old_key)

    def find_nick(self, nick: str) -> str | None:
        """Ejaan nick di channel, atau None kalau tidak terlihat."""
        key = irc_lower(nick)
        for chan in self.channels.values():
            if key in chan.users:
                return chan.users[key]
        return None
