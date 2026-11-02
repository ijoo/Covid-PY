"""Koneksi TCP/TLS ke server IRC."""

import asyncio
import logging
import ssl

from .config import Server
from .protocol import encode_line, redact

log = logging.getLogger(__name__)

_READ_LIMIT = 65536  # baris dengan IRCv3 tags bisa jauh melebihi 512 byte


class Connection:
    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self, server: Server, timeout: float) -> None:
        context = None
        if server.tls:
            context = ssl.create_default_context()
            if not server.tls_verify:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        async with asyncio.timeout(timeout):
            self._reader, self._writer = await asyncio.open_connection(
                server.host,
                server.port,
                ssl=context,
                server_hostname=server.host if context else None,
                limit=_READ_LIMIT,
            )

    async def read_line(self) -> str | None:
        """Baca satu baris. None berarti koneksi tertutup."""
        try:
            data = await self._reader.readline()
        except ValueError:
            log.warning("baris dari server terlalu panjang, dilewati")
            return ""
        if not data:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    def write(self, line: str) -> None:
        log.debug(">> %s", redact(line))
        self._writer.write(encode_line(line))

    async def drain(self) -> None:
        await self._writer.drain()

    def abort(self) -> None:
        """Putus paksa. close() bisa menggantung kalau peer sudah mati."""
        if self._writer is not None:
            self._writer.transport.abort()
