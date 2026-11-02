"""Variabel global gaya pesan, dipindahkan dari Covid.5.0.tcl."""

import random

# Kode format IRC
BOLD = "\x02"
COLOR = "\x03"
RESET = "\x0f"
UNDERLINE = "\x1f"

# Nomor warna mIRC
WHITE, BLACK, BLUE, GREEN, RED, BROWN, PURPLE, ORANGE = range(8)
YELLOW, LIME, TEAL, CYAN, ROYAL, PINK, GREY, SILVER = range(8, 16)

# Nilai dari Covid.5.0.tcl baris 20-28
verc = "5.0"
notic = "!\x02c\x1fV\x1fd\x02!"
notim = "!\x02c\x1fV\x1fd\x02!"
tolak = "\x02\x034deNieD!\x03\x02"
tuan = "\x02\x0312oWNeR!\x03\x02"
aktif = "\x02ACTIVATED\x02"
deaktif = "\x02DEACTIVATED\x02"
enable = "\x02ENABLE\x02"
disable = "\x02DISABLE\x02"

partm = (
    "bAlIk Base!", "No beTAH!", "Bad ChANneL!", "BaCk To BaSe", "Be Right Back!",
    "No one Join Forever!", "Damn! Wrong Channel!", "Access Denied!", "Return To Base!",
    "Access Rejected!", "Going Back!", "Ilegal Channel!", "Going Home!", "Good Bye!",
    "Sayonara!", "Ups! Wrong Room :P", "Got To Go!", "Goodbye! Ugly :P", "Go To Hell!",
)

kickm = (
    "WroNG ChaNneL!", "Bad UsER!", "Go ToHeLL!", "KiccckeedDdd!!", "HaTe iT!",
    "NoT WelCoMe!", "wHatt!!", "MiSs Mee!?!", "TasTed", "FiRSt BloOD!", "DoUble KiLL!",
    "TrIPLe KiLL!", "MaNiaC..!", "SaVaGee..!", "LeGenDarY..", "DrOp OuT!", "StAY OuT!",
    "BrOkEn BoNE..!", "FiRE in ThE hOLe.. ", "Bad Boy nOT AlLoWed",
)

# Pasangan warna logo: hitam/putih dengan warna lain. Putih dengan kuning
# dibuang karena sulit dibaca.
_LOGO_PAIRS = tuple(
    (plain, tint)
    for plain in (WHITE, BLACK)
    for tint in range(BLUE, SILVER + 1)
    if (plain, tint) != (WHITE, YELLOW)
)


def bold(text: str) -> str:
    return f"{BOLD}{text}{BOLD}"


def underline(text: str) -> str:
    return f"{UNDERLINE}{text}{UNDERLINE}"


def color(text: str, fg: int, bg: int | None = None) -> str:
    """Bungkus teks dengan warna.

    Nomor warna selalu dua digit, supaya teks yang diawali angka tidak
    terbaca sebagai bagian dari nomor warna.
    """
    code = f"{fg:02d}" if bg is None else f"{fg:02d},{bg:02d}"
    return f"{COLOR}{code}{text}{COLOR}"


def scramble_case(text: str) -> str:
    """Acak besar kecil tiap huruf (katakata + atasbawah di TCL)."""
    scrambled = "".join(random.choice((c.lower(), c.upper())) for c in text)
    # Karakter dibatasi eksplisit: strip() biasa juga membuang \x1f (UNDERLINE)
    # karena Python menganggapnya whitespace.
    return scrambled.strip(" \t\r\n")


class _Lgrnd:
    """Teks CoVID berwarna acak; warnanya baru setiap kali dirender."""

    def __str__(self) -> str:
        a, b = random.choice(_LOGO_PAIRS)
        return (
            f"\x03{a},{b}C\x03{b},{a}o"
            f"\x03{a},{b}V\x03{b},{a}I"
            f"\x03{a},{b}D\x03"
        )


lgrnd = _Lgrnd()
