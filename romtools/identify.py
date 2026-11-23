"""What is this file, going by its own bytes only - not its name, extension or folder.

Used by ROM Verifier (which DAT to use, what an unknown file is, "wrong folder") and ROM Renamer (sorting into
system folders). Only signatures that don't occur by accident count; formats without one (Atari 2600, PC Engine,
SG-1000, Virtual Boy, MSX …) return None, and are then recognised by their hash or not at all.

Everything here only reads, and only a few KB (or, for WonderSwan, the whole small ROM).
"""

from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Read = Callable[[int, int], bytes]          # read(offset, count)


@dataclass
class Ident:
    system: str = ""            # catalogue id ("snes"), "" for containers that don't say
    what: str = ""              # in words: "SNES ROM header", "ZIP archive"
    title: str = ""             # the game's own internal title, if it has one
    container: str = ""         # archive | chd | disc | package | "" (a plain ROM / disc image)
    ext: str = ""               # the usual extension, when one fits


# the extensions emulators expect for a system's plain ROMs (first = the one to use when the file has none of them)
SYSTEM_EXTS: dict[str, tuple[str, ...]] = {
    "nes": (".nes", ".unf", ".unif"), "fds": (".fds",), "snes": (".sfc", ".smc", ".fig", ".swc", ".bs", ".st"),
    "n64": (".z64", ".n64", ".v64"), "gb": (".gb",), "gbc": (".gbc", ".gb"), "gba": (".gba",),
    "nds": (".nds", ".dsi"), "3ds": (".3ds", ".cci", ".cia"), "virtualboy": (".vb",), "pokemini": (".min",),
    "genesis": (".md", ".gen", ".smd", ".bin"), "sms": (".sms",), "gamegear": (".gg",), "sega32": (".32x", ".bin"),
    "sg1000": (".sg",), "tg16": (".pce",), "supergrafx": (".sgx", ".pce"), "atari2600": (".a26", ".bin"),
    "atari5200": (".a52", ".bin"), "atari7800": (".a78", ".bin"), "lynx": (".lnx", ".lyx"), "jaguar": (".j64",),
    "ngp": (".ngp",), "ngpc": (".ngc", ".ngp"), "wonderswan": (".ws",), "wonderswan-color": (".wsc", ".ws"),
    "msx": (".rom", ".mx1", ".mx2"),
}
_BY_EXT = {e: s for s, exts in SYSTEM_EXTS.items() for e in exts[:1]}
_BY_EXT.update({".smc": "snes", ".fig": "snes", ".swc": "snes", ".n64": "n64", ".v64": "n64", ".gen": "genesis",
                ".smd": "genesis", ".unf": "nes", ".dsi": "nds", ".cia": "3ds", ".cci": "3ds", ".lyx": "lynx"})


def system_from_ext(name: str) -> str:
    """The system an extension stands for, "" when it's shared or generic (.bin, .iso, .rom …)."""
    ext = Path(name).suffix.lower()
    return "" if ext in (".rom", ".bin", ".md") else _BY_EXT.get(ext, "")


def _text(b: bytes) -> str:
    s = b.split(b"\0")[0].decode("ascii", "replace")
    s = re.sub(r"[^\x20-\x7e]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


GB_LOGO = bytes.fromhex("ceed6666cc0d000b03730083000c000d0008111f8889000e")
GBA_LOGO = bytes.fromhex("24ffae51699aa2213d84820a84e409ad")
XBOX_MAGIC = b"MICROSOFT*XBOX*MEDIA"


# ---------------------------------------------------------------- containers

def _container(h: bytes, read: Read, size: int) -> Ident | None:
    if h[:4] == b"PK\x03\x04":
        return Ident(what="ZIP archive", container="archive", ext=".zip")
    if h[:6] == b"7z\xbc\xaf\x27\x1c":
        return Ident(what="7z archive", container="archive", ext=".7z")
    if h[:6] == b"Rar!\x1a\x07":
        return Ident(what="RAR archive", container="archive", ext=".rar")
    if h[:8] == b"MComprHD":
        return Ident(what="CHD disc image", container="chd", ext=".chd")
    if h[:4] in (b"RVZ\x01", b"WIA\x01"):
        disc = read(0x58, 0x20)
        fmt = h[:3].decode()
        return Ident(_nintendo_disc(disc), f"{fmt} disc image", container="disc", ext="." + fmt.lower())
    if h[:4] == b"CISO":
        if struct.unpack_from("<I", h, 4)[0] >= 0x8000:             # GameCube/Wii CISO: block size at 4
            return Ident(_nintendo_disc(read(0x8000, 0x20)), "CISO disc image", container="disc", ext=".ciso")
        return Ident("psp", "CSO disc image", ext=".cso")           # shared with PS2, but that's rare
    if h[:4] == b"\x01\xc0\x0b\xb1":
        return Ident(what="GCZ disc image", container="disc", ext=".gcz")
    if h[:4] == b"WBFS":
        return Ident("wii", "WBFS disc image", container="disc", ext=".wbfs")
    if h[:4] == b"WUX0":
        return Ident("wiiu", "WUX disc image", ext=".wux")
    if h[:4] == b"\x7fCNT":
        return Ident("ps4", "PS4 package", container="package", ext=".pkg")
    if h[:4] == b"\x7fPKG":
        return Ident("ps3" if h[6:8] == b"\0\x01" else "", "PS3 / PSP / Vita package", ext=".pkg")
    if h[:4] == b"PFS0":
        return Ident("switch", "Switch package", container="package", ext=".nsp")
    if h[0x100:0x104] == b"HEAD" and size > 0x10000 and read(0xF000, 4) == b"HFS0":
        return Ident("switch", "Switch game card image", container="package", ext=".xci")
    if h[:4] == b"\0PBP":
        return Ident("psp", "PSP / PS1 EBOOT", ext=".pbp")
    return None


def _nintendo_disc(h: bytes) -> str:
    if h[0x1C:0x20] == b"\xc2\x33\x9f\x3d":
        return "ngc"
    if h[0x18:0x1C] == b"\x5d\x1c\x9e\xa3":
        return "wii"
    return ""


# ---------------------------------------------------------------- cartridges

def snes_header(read: Read, size: int) -> tuple[int, str] | None:
    """(copier header length, title) when the internal SNES header is valid: checksum and complement add up, the map
    mode and sizes make sense and the title is text."""
    for skip in ((512, 0) if size % 1024 == 512 else (0,)):
        for base in (0x7FC0, 0xFFC0, 0x40FFC0):
            if skip + base + 0x40 > size:
                continue
            b = read(skip + base, 0x40)
            if len(b) < 0x40:
                continue
            comp, chk = struct.unpack_from("<HH", b, 0x1C)
            if comp ^ chk != 0xFFFF or chk in (0, 0xFFFF):
                continue
            mode, romsize = b[0x15], b[0x17]
            if mode & 0xE0 != 0x20 or not 5 <= romsize <= 13:
                continue
            reset = struct.unpack_from("<H", b, 0x3C)[0]
            if reset < 0x8000:
                continue
            return skip, _text(b[:21])
    return None


def n64_order(h: bytes) -> str:
    return {b"\x80\x37\x12\x40": "z64", b"\x37\x80\x40\x12": "v64", b"\x40\x12\x37\x80": "n64"}.get(h[:4], "")


def n64_swap(data: bytes, order: str) -> bytes:
    """Bytes of a .v64 / .n64 dump in the .z64 order the DATs use (len(data) must be a multiple of 4)."""
    import array
    a = array.array("H" if order == "v64" else "I", data)
    a.byteswap()
    return a.tobytes()


def smd_decode(block: bytes) -> bytes:
    """One 16 KB block of a .smd (Super Magic Drive copier) dump in normal order."""
    half = len(block) // 2
    out = bytearray(half * 2)
    out[1::2], out[0::2] = block[:half], block[half:half * 2]
    return bytes(out)


def _mega_drive(sega: bytes, what: str) -> Ident | None:
    s = sega[:16]
    if b"SEGA 32X" in s:
        return Ident("sega32", "32X ROM header" + what, _text(sega[0x20:0x50]), ext=".32x")
    if s.lstrip().startswith(b"SEGA") and (b"MEGA" in s or b"GENESIS" in s or s[4:5] == b" "):
        if b"PICO" in s or b"TOYS" in s:
            return None
        return Ident("genesis", "Mega Drive ROM header" + what, _text(sega[0x20:0x50]), ext=".md")
    return None


def _cartridge(h: bytes, read: Read, size: int) -> Ident | None:
    if h[:4] == b"NES\x1a":
        return Ident("nes", "iNES header", ext=".nes")
    if h[:4] == b"UNIF":
        return Ident("nes", "UNIF header", ext=".unf")
    if h[:4] == b"FDS\x1a" or h[:15] == b"\x01*NINTENDO-HVC*":
        return Ident("fds", "Famicom Disk System header", ext=".fds")
    if h[:4] == b"LYNX" and h[4:8] != b"\0\0\0\0":
        return Ident("lynx", "Lynx header", _text(h[10:42]), ext=".lnx")
    if h[1:10] == b"ATARI7800":
        return Ident("atari7800", "Atari 7800 header", _text(h[17:49]), ext=".a78")
    if h[0x104:0x11C] == GB_LOGO:
        color = h[0x143] & 0x80
        return Ident("gbc" if color else "gb", "Game Boy Color header" if color else "Game Boy header",
                     _text(h[0x134:0x143 if color else 0x144]), ext=".gbc" if color else ".gb")
    if h[0x04:0x14] == GBA_LOGO and h[0xB2] == 0x96:
        return Ident("gba", "Game Boy Advance header", _text(h[0xA0:0xAC]), ext=".gba")
    if h[0xC0:0xD0] == GBA_LOGO and h[0x15C:0x15E] == b"\x56\xcf":
        return Ident("nds", "Nintendo DS header", _text(h[:12]), ext=".nds")
    if h[0x100:0x104] == b"NCSD":
        return Ident("3ds", "Nintendo 3DS header", ext=".3ds")
    if h[:8] == b"\x20\x20\0\0\0\0\0\0" and h[8:12] == b"\0\x0a\0\0":
        return Ident("3ds", "Nintendo 3DS CIA", ext=".cia")
    order = n64_order(h)
    if order:
        t = h[:0x40] if order == "z64" else n64_swap(h[:0x40], order)
        return Ident("n64", "Nintendo 64 header", _text(t[0x20:0x34]), ext="." + order)
    md = _mega_drive(h[0x100:0x160], "")
    if md:
        return md
    if size % 16384 == 512 and h[8:10] == b"\xaa\xbb" and size > 16384:
        md = _mega_drive(smd_decode(read(512, 16384))[0x100:0x160], " (.smd copier dump)")
        if md:
            md.ext = ".smd"
            return md
    if h[:28] in (b"COPYRIGHT BY SNK CORPORATION", b" LICENSED BY SNK CORPORATION"):
        color = h[0x23] == 0x10
        return Ident("ngpc" if color else "ngp", "Neo Geo Pocket header", _text(h[0x24:0x30]),
                     ext=".ngc" if color else ".ngp")
    if size > 0x21B0 and read(0x2100, 2) == b"MN" and read(0x21A4, 8) == b"NINTENDO":
        return Ident("pokemini", "Pokemon Mini header", _text(read(0x21B0, 12)), ext=".min")
    for off in (0x7FF0, 0x3FF0, 0x1FF0):
        if off + 16 <= size:
            b = read(off, 16)
            if b[:8] == b"TMR SEGA":
                gg = b[15] >> 4 in (5, 6, 7)
                return Ident("gamegear" if gg else "sms", "Game Gear header" if gg else "Master System header",
                             ext=".gg" if gg else ".sms")
    sn = snes_header(read, size) if size <= 16 << 20 else None
    if sn:
        return Ident("snes", "SNES header" + (" after a copier header" if sn[0] else ""), sn[1],
                     ext=".smc" if sn[0] else ".sfc")
    if 0x10000 <= size <= 16 << 20 and size % 0x10000 == 0:          # WonderSwan: footer + checksum of the whole ROM
        f = read(size - 16, 16)
        if f[:1] == b"\xea" and f[5] & 0x0F == 0 and f[7] in (0, 1) and f[10] <= 0x0B:
            data = read(0, size)
            if sum(data[:-2]) & 0xFFFF == struct.unpack("<H", data[-2:])[0]:
                return Ident("wonderswan-color" if f[7] else "wonderswan", "WonderSwan footer and checksum",
                             ext=".wsc" if f[7] else ".ws")
    return None


# ---------------------------------------------------------------- discs

def _sectors(h: bytes, read: Read) -> tuple[Read, bool] | None:
    """(read sector data at (sector*2048 + offset), raw?) for a plain or raw (2352) disc image."""
    if h[:12] == b"\0" + b"\xff" * 10 + b"\0":
        skip = 24 if h[15] == 2 else 16

        def raw(off: int, n: int) -> bytes:
            out = b""
            while n > 0:
                lba, rest = divmod(off, 2048)
                part = read(lba * 2352 + skip + rest, min(n, 2048 - rest))
                if not part:
                    break
                out += part
                off += len(part)
                n -= len(part)
            return out
        return raw, True
    return read, False


def _root_file(sec: Read, pvd: bytes, *names: str) -> bytes | None:
    """Contents (first 4 KB) of a file in an ISO 9660 root directory."""
    lba, size = struct.unpack_from("<I", pvd, 158)[0], struct.unpack_from("<I", pvd, 166)[0]
    data = sec(lba * 2048, min(size, 64 * 2048))
    pos = 0
    while pos < len(data):
        ln = data[pos]
        if ln == 0:
            pos = (pos // 2048 + 1) * 2048
            continue
        rec = data[pos:pos + ln]
        if len(rec) > 33:
            name = rec[33:33 + rec[32]].decode("ascii", "replace").split(";")[0].upper()
            if name in names:
                return sec(struct.unpack_from("<I", rec, 2)[0] * 2048, min(struct.unpack_from("<I", rec, 10)[0], 4096))
        pos += ln
    return None


def _disc(h: bytes, read: Read, size: int) -> Ident | None:
    n = _nintendo_disc(h)
    if n:
        return Ident(n, "GameCube disc header" if n == "ngc" else "Wii disc header", _text(h[0x20:0x60]), ext=".iso")
    if h[:4] == b"WUP-" and size > 1 << 30:
        return Ident("wiiu", "Wii U disc header", ext=".wud")
    sec, raw = _sectors(h, read)
    ext = ".bin" if raw else ".iso"
    s0 = sec(0, 0x200)
    if s0.startswith(b"SEGA SEGASATURN"):
        return Ident("saturn", "Saturn disc header", _text(s0[0x60:0xD0]), ext=ext)
    if s0.startswith(b"SEGADISCSYSTEM") or s0[0x10:0x1E] == b"SEGADISCSYSTEM":
        return Ident("segacd", "Sega CD disc header", _text(s0[0x150:0x180]), ext=ext)
    if s0.startswith(b"SEGA SEGAKATANA"):
        return Ident("dc", "Dreamcast disc header", _text(s0[0x80:0x100]), ext=ext)
    if s0[:1] == b"\x01" and s0[1:6] == b"ZZZZZ" and s0[6:7] == b"\x01" and b"CD-ROM" in s0[:0x40]:
        return Ident("3do", "3DO disc header", ext=ext)
    if sec(0x800, 12) == b"PlayStation3":
        return Ident("ps3", "PlayStation 3 disc header", ext=".iso")
    if b"PC Engine CD-ROM SYSTEM" in sec(0x800, 0x80):
        return Ident("pcecd", "PC Engine CD header", ext=ext)
    if not raw:
        for off, sysid in ((0x18310000, "xbox"), (0xFDA0000, "xbox360"), (0x2090000, "xbox360"),
                           (0x2080000, "xbox360"), (0x10000, "")):
            if off + 20 <= size and read(off, 20) == XBOX_MAGIC:
                return Ident(sysid, "Xbox disc header", ext=".iso")
    pvd = sec(16 * 2048, 2048)
    if len(pvd) < 2048:
        return None
    if pvd[1:6] == b"CD-I ":
        return Ident("cdi", "CD-i disc", _text(pvd[40:72]), ext=ext)
    if pvd[1:6] != b"CD001":
        return Ident(what="raw CD track", ext=".bin") if raw else None
    sys_id, vol = _text(pvd[8:40]), _text(pvd[40:72])
    if sys_id.startswith("PSP GAME"):
        return Ident("psp", "PSP disc", vol, ext=".iso")
    if sys_id.startswith("PLAYSTATION"):
        cnf = (_root_file(sec, pvd, "SYSTEM.CNF") or b"").upper()
        m = re.search(rb"([A-Z]{4})[_-]?(\d{3})\.?(\d{2})", cnf)
        serial = f"{m[1].decode()}-{(m[2] + m[3]).decode()}" if m else vol
        if b"BOOT2" in cnf:
            return Ident("ps2", "PlayStation 2 disc", serial, ext=ext)
        if b"BOOT" in cnf or _root_file(sec, pvd, "PSX.EXE") is not None:
            return Ident("psx", "PlayStation disc", serial, ext=ext)
        return Ident("", "PlayStation disc", vol, ext=ext)
    if _root_file(sec, pvd, "IPL.TXT") is not None:
        return Ident("neogeocd", "Neo Geo CD disc", vol, ext=ext)
    return Ident("", "data disc (ISO 9660)", vol, ext=ext)


# ---------------------------------------------------------------- entry points

def sniff(read: Read, size: int) -> Ident | None:
    """Identify by content; None when nothing is recognised."""
    if size < 16:
        return None
    h = read(0, 0x400)
    h = h + bytes(max(0, 0x400 - len(h)))
    return _container(h, read, size) or _cartridge(h, read, size) or _disc(h, read, size)


def from_bytes(data: bytes, size: int | None = None) -> Ident | None:
    return sniff(lambda off, n: data[off:off + n], len(data) if size is None else size)


def identify(path: Path) -> Ident | None:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            def read(off: int, n: int) -> bytes:
                fh.seek(off)
                return fh.read(n)
            return sniff(read, size)
    except (OSError, struct.error):
        return None


INNER_MAX = 8 << 20         # how much of an archive member is read to recognise it


def identify_archive(path: Path) -> Ident | None:
    """The first recognised file inside an archive (largest first)."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for info in sorted((i for i in zf.infolist() if not i.is_dir()), key=lambda i: -i.file_size)[:4]:
                    with zf.open(info) as fh:
                        hit = from_bytes(fh.read(INNER_MAX), info.file_size)
                    if hit and hit.system:
                        return hit
            return None
        import subprocess
        from .hashing import SEVENZIP, _7z_list
        if not SEVENZIP:
            return None
        for member, size in sorted(_7z_list(path), key=lambda m: -m[1])[:2]:
            proc = subprocess.Popen([SEVENZIP, "e", "-so", "-bd", str(path), member],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                hit = from_bytes(proc.stdout.read(INNER_MAX), size)
            finally:
                proc.kill()
                proc.wait()
            if hit and hit.system:
                return hit
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, struct.error):
        pass
    return None


# ---------------------------------------------------------------- extracted games

_EXTRACTED = (("ps3_game", "ps3"), ("ps3_disc.sfb", "ps3"), ("sce_sys", "ps4"), ("default.xex", "xbox360"),
              ("default.xbe", "xbox"), ("psp_game", "psp"))


def extracted_game(d: Path, names: set[str] | None = None) -> str | None:
    """The system of a game that was already extracted to this folder, else None. Their thousands of data files aren't
    ROMs and mustn't be scanned as such. ``names``: the folder's entries in lower case, when they're known already."""
    if names is None:
        import os
        try:
            with os.scandir(d) as it:
                names = {e.name.lower() for e in it}
        except OSError:
            return None
    for n, sysid in _EXTRACTED:
        if n in names:
            return sysid
    if "exefs" in names and (d / "exefs" / "main.npdm").is_file():
        return "switch"
    if {"code", "content", "meta"} <= names:
        return "wiiu"
    if "sys" in names and "files" in names and (d / "sys" / "boot.bin").is_file():
        return "ngc"
    return None
