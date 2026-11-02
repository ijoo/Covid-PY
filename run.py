#!/usr/bin/env python3
"""Entry point CoVIDbot: python3 run.py [config.toml] [--debug]"""

import argparse
import asyncio
import getpass
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from covidbot.bot import Bot
from covidbot.config import Config, ConfigError, load_config
from covidbot.protocol import strip_formatting
from covidbot.storage import StoreError
from covidbot.users import (
    MIN_PASSWORD,
    UserDB,
    UserFileError,
    hash_password,
    valid_handle,
)


class PlainFormatter(logging.Formatter):
    """Log tanpa kode warna IRC."""

    def format(self, record: logging.LogRecord) -> str:
        return strip_formatting(super().format(record))


def setup_logging(config: Config, debug: bool) -> None:
    Path(config.log_file).parent.mkdir(parents=True, exist_ok=True)
    formatter = PlainFormatter("%(asctime)s %(levelname)-7s !cVd! %(message)s")
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            config.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
    ]
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)


def add_owner(config: Config, handle: str) -> None:
    """Buat owner atau reset password yang lupa. Jalankan saat bot mati."""
    if not valid_handle(handle):
        sys.exit(f"Handle tidak valid: {handle}")
    password = getpass.getpass("Password owner: ")
    if len(password) < MIN_PASSWORD or " " in password:
        sys.exit(f"Password minimal {MIN_PASSWORD} karakter dan tanpa spasi")
    if password != getpass.getpass("Ulangi password: "):
        sys.exit("Password tidak sama")
    users = UserDB(config.users_file)
    users.add(handle, flags="nf").password = hash_password(password)
    users.save()
    print(f"Owner {handle} tersimpan di {config.users_file}")


async def main(config: Config) -> bool:
    """Jalankan bot; True kalau proses harus dimulai ulang."""
    bot = Bot(config)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, bot.stop)
    await bot.run()
    return bot.restart_requested


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CoVIDbot")
    parser.add_argument("config", nargs="?", default="config.toml")
    parser.add_argument("--debug", action="store_true", help="catat semua baris IRC")
    parser.add_argument(
        "--add-owner", metavar="HANDLE",
        help="buat owner atau reset password yang lupa",
    )
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        if args.add_owner:
            add_owner(cfg, args.add_owner)
            sys.exit(0)
        setup_logging(cfg, args.debug)
        if asyncio.run(main(cfg)):
            logging.shutdown()
            os.execv(sys.executable, [sys.executable, *sys.argv])
    except (ConfigError, UserFileError, StoreError) as exc:
        sys.exit(f"Config error: {exc}")
