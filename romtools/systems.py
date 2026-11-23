"""Catalog of known systems: where to get their DAT and which folder names map to them.

Disc systems use official Redump DATs (full track lists, Logiqx XML).
Cartridge systems use No-Intro DATs from the libretro-database mirror,
because Dat-o-Matic has no direct download URLs.  Any DAT can also be
imported by hand from the DAT manager (e.g. a Dat-o-Matic daily pack).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

LIBRETRO_NOINTRO = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/"
REDUMP = "http://redump.org/datfile/{}/"


@dataclass(frozen=True)
class System:
    id: str
    name: str
    source: str            # "redump" | "no-intro"
    ref: str               # redump code or libretro DAT basename
    folders: tuple = field(default_factory=tuple)  # folder names (RomM / ES-DE / Batocera)

    @property
    def url(self) -> str:
        if self.source == "package":
            return ""                 # no DAT: identified from the packages' own data
        if self.source == "redump":
            return REDUMP.format(self.ref)
        return LIBRETRO_NOINTRO + quote(self.ref + ".dat")

    @property
    def label(self) -> str:
        tag = {"redump": "Redump", "package": "package data, no DAT"}.get(self.source, "No-Intro")
        return f"{self.name}  [{tag}]"


def _ni(id_, ref, *folders):
    return System(id_, ref.split(" - ", 1)[-1], "no-intro", ref, (id_,) + folders)


def _rd(id_, name, code, *folders):
    return System(id_, name, "redump", code, (id_,) + folders)


def _pk(id_, name, *folders):
    """A console whose dumps are packages identified from their own metadata (no DAT exists)."""
    return System(id_, name, "package", "", (id_,) + folders)


CATALOG: list[System] = [
    # --- Cartridge / No-Intro ---
    _ni("nes", "Nintendo - Nintendo Entertainment System", "famicom"),
    _ni("fds", "Nintendo - Family Computer Disk System", "famicom-disk-system"),
    _ni("snes", "Nintendo - Super Nintendo Entertainment System", "sfc", "sfam", "superfamicom"),
    _ni("n64", "Nintendo - Nintendo 64"),
    _ni("gb", "Nintendo - Game Boy"),
    _ni("gbc", "Nintendo - Game Boy Color"),
    _ni("gba", "Nintendo - Game Boy Advance"),
    _ni("nds", "Nintendo - Nintendo DS"),
    _ni("3ds", "Nintendo - Nintendo 3DS", "n3ds"),
    _ni("virtualboy", "Nintendo - Virtual Boy", "virtual-boy", "vb"),
    _ni("pokemini", "Nintendo - Pokemon Mini", "pokemon-mini"),
    _ni("genesis", "Sega - Mega Drive - Genesis", "genesis-slash-megadrive", "megadrive", "md"),
    _ni("sms", "Sega - Master System - Mark III", "mastersystem"),
    _ni("gamegear", "Sega - Game Gear", "gg"),
    _ni("sega32", "Sega - 32X", "sega32x", "32x"),
    _ni("sg1000", "Sega - SG-1000", "sg-1000"),
    _ni("tg16", "NEC - PC Engine - TurboGrafx 16", "pcengine", "turbografx16--1", "pce"),
    _ni("supergrafx", "NEC - PC Engine SuperGrafx"),
    _ni("atari2600", "Atari - 2600"),
    _ni("atari5200", "Atari - 5200"),
    _ni("atari7800", "Atari - 7800"),
    _ni("lynx", "Atari - Lynx", "atari-lynx"),
    _ni("jaguar", "Atari - Jaguar", "atari-jaguar"),
    _ni("ngp", "SNK - Neo Geo Pocket"),
    _ni("ngpc", "SNK - Neo Geo Pocket Color", "neo-geo-pocket-color"),
    _ni("wonderswan", "Bandai - WonderSwan", "ws"),
    _ni("wonderswan-color", "Bandai - WonderSwan Color", "wsc"),
    _ni("msx", "Microsoft - MSX"),
    # --- Disc / Redump ---
    _rd("psx", "Sony - PlayStation", "psx", "ps1", "ps"),
    _rd("ps2", "Sony - PlayStation 2", "ps2"),
    _rd("psp", "Sony - PlayStation Portable", "psp"),
    _rd("ps3", "Sony - PlayStation 3", "ps3"),
    _rd("dc", "Sega - Dreamcast", "dc", "dreamcast"),
    _rd("saturn", "Sega - Saturn", "ss", "sega-saturn"),
    _rd("segacd", "Sega - Mega CD & Sega CD", "mcd", "megacd", "sega-cd"),
    _rd("pcecd", "NEC - PC Engine CD & TurboGrafx CD", "pce", "turbografx-cd", "pcenginecd", "tg16cd"),
    _rd("ngc", "Nintendo - GameCube", "gc", "gamecube", "gc"),
    _rd("wii", "Nintendo - Wii", "wii"),
    _rd("wiiu", "Nintendo - Wii U", "wiiu"),
    _rd("xbox", "Microsoft - Xbox", "xbox"),
    _rd("xbox360", "Microsoft - Xbox 360", "xbox360"),
    _rd("3do", "Panasonic - 3DO", "3do"),
    _rd("cdi", "Philips - CD-i", "cdi", "philips-cd-i"),
    _rd("neogeocd", "SNK - Neo Geo CD", "ngcd", "neo-geo-cd"),
    # --- Packages (no DAT; read from the PS4 PKG / Switch NSP / XCI metadata) ---
    _pk("ps4", "Sony - PlayStation 4", "playstation4"),
    _pk("switch", "Nintendo - Switch", "nsw", "ns"),
    _pk("switch2", "Nintendo - Switch 2", "switch-2", "nsw2"),
]

BY_ID = {s.id: s for s in CATALOG}


def guess_system(folder: str) -> System | None:
    f = folder.lower().strip()
    for s in CATALOG:
        if f in s.folders:
            return s
    return None


# Systems where DATs are absent or the dumps are not plain files; we say so instead of "unknown".
UNVERIFIABLE_HINT = {
    "switch": "No public DAT matches NSP/XCI dumps (they are personalised): they're identified from their own "
              "data instead (title, ID, version; needs your prod.keys).",
    "switch-2": "No public DAT for Switch 2 dumps.",
    "ps4": "PS4 PKGs have no public DAT: they're identified from their own data (title, ID, type, version, region).",
    "ps5": "PS5 dumps have no public DAT.",
    "ps3": "PS3 folder (JB) dumps can't be compared with Redump's ISO hashes; only ISOs can.",
    "wiiu": "WUA files are Cemu archives, not disc images; only .wud/.wux/.iso match Redump.",
}
