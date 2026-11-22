"""Decide whether a disc image should become a CD or a DVD CHD - and say why.

Evidence, strongest first:
  1. the disc's own contents (reads only a few KB):
     - ISO 9660 system identifier "PSP GAME"              -> PSP (DVD/UMD)
     - SYSTEM.CNF with BOOT2                               -> PS2
     - SYSTEM.CNF with BOOT                                -> PS1 (CD)
     - "MICROSOFT*XBOX*MEDIA" at an XDVDFS offset          -> Xbox / Xbox 360 (DVD)
     - GameCube / Wii disc magic                           -> DVD
     - "SEGA SEGASATURN" / "SEGADISCSYSTEM" / "SEGA SEGAKATANA" headers -> CD
  2. for PS2: the Redump DAT (CD games are listed as .bin/.cue, DVD games as .iso)
  3. the system folder the file sits in (psp/, ps2/, psx/, ...)
  4. size (> 800 MB can only be a DVD)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .systems import guess_system

CD, DVD = "cd", "dvd"
SECTOR = 2048
CD_MAX = 900 * 1024 * 1024          # largest possible CD (99 min) is ~870 MB of 2048-byte sectors

# Folder/system id -> media, for systems that only ever used one kind of disc.
SYSTEM_MEDIA = {
    "psx": CD, "dc": CD, "saturn": CD, "segacd": CD, "pcecd": CD, "3do": CD, "cdi": CD, "neogeocd": CD,
    "psp": DVD, "xbox": DVD, "xbox360": DVD, "ngc": DVD, "wii": DVD, "wiiu": DVD, "ps3": DVD,
}
SYSTEM_NAMES = {
    "psx": "PlayStation", "ps2": "PlayStation 2", "psp": "PSP", "dc": "Dreamcast", "saturn": "Saturn",
    "segacd": "Sega CD", "pcecd": "PC Engine CD", "3do": "3DO", "cdi": "CD-i", "neogeocd": "Neo Geo CD",
    "xbox": "Xbox", "xbox360": "Xbox 360", "ngc": "GameCube", "wii": "Wii", "wiiu": "Wii U", "ps3": "PS3",
}
# Emulators that can't read CHD for these systems (the conversion still works, but warn).
NO_CHD_SUPPORT = {
    "ngc": "Dolphin can't read CHD - RVZ is the lossless format for GameCube",
    "wii": "Dolphin can't read CHD - RVZ is the lossless format for Wii",
    "xbox": "xemu can't read CHD - keep Xbox games as ISO",
    "xbox360": "Xenia can't read CHD - keep Xbox 360 games as ISO",
    "wiiu": "Cemu can't read CHD",
    "ps3": "RPCS3 can't read CHD",
}


@dataclass
class Detection:
    media: str          # "cd" | "dvd"
    system: str = ""    # catalog id, "" if unknown
    reason: str = ""
    warning: str = ""
    confident: bool = True

    @property
    def summary(self) -> str:
        name = SYSTEM_NAMES.get(self.system, "")
        head = f"{name} → {self.media.upper()}" if name else self.media.upper()
        return f"{head} ({self.reason})" + (f" ⚠ {self.warning}" if self.warning else "")


# ---------------------------------------------------------------- ISO 9660 helpers

def _read(fh, offset: int, n: int) -> bytes:
    fh.seek(offset)
    return fh.read(n)


def _pvd(fh) -> bytes | None:
    """Primary volume descriptor, or None if this isn't a plain 2048-byte ISO 9660 image."""
    d = _read(fh, 16 * SECTOR, SECTOR)
    return d if len(d) == SECTOR and d[1:6] == b"CD001" else None


def _find_in_root(fh, pvd: bytes, name: str) -> tuple[int, int] | None:
    """(LBA, size) of a file in the root directory."""
    root = pvd[156:190]
    lba, size = struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0]
    data = _read(fh, lba * SECTOR, min(size, 64 * SECTOR))
    pos, want = 0, name.upper()
    while pos < len(data):
        ln = data[pos]
        if ln == 0:  # records never cross sectors: skip to the next one
            pos = (pos // SECTOR + 1) * SECTOR
            continue
        rec = data[pos:pos + ln]
        nlen = rec[32] if len(rec) > 32 else 0
        fname = rec[33:33 + nlen].decode("ascii", "replace").split(";")[0].upper()
        if fname == want:
            return struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0]
        pos += ln
    return None


def _system_cnf(fh, pvd: bytes) -> str | None:
    hit = _find_in_root(fh, pvd, "SYSTEM.CNF")
    if not hit:
        return None
    lba, size = hit
    return _read(fh, lba * SECTOR, min(size, 4096)).decode("ascii", "replace")


XBOX_MAGIC = b"MICROSOFT*XBOX*MEDIA"
# XDVDFS header sits at sector 32 of the game partition; full Redump images put that partition at an offset.
XBOX_OFFSETS = (0x10000, 0x18310000, 0xFDA0000, 0x2090000)   # plain/XISO, XGD1, XGD2, XGD3


def from_contents(path: Path) -> Detection | None:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = _read(fh, 0, 0x100)
            if len(head) >= 0x20:
                if head[0x1C:0x20] == b"\xc2\x33\x9f\x3d":
                    return Detection(DVD, "ngc", "GameCube disc header")
                if head[0x18:0x1C] == b"\x5d\x1c\x9e\xa3":
                    return Detection(DVD, "wii", "Wii disc header")
                if head.startswith(b"SEGA SEGASATURN"):
                    return Detection(CD, "saturn", "Saturn boot header")
                if head.startswith(b"SEGADISCSYSTEM") or head[0x10:0x1E] == b"SEGADISCSYSTEM":
                    return Detection(CD, "segacd", "Sega CD boot header")
                if head.startswith(b"SEGA SEGAKATANA"):
                    return Detection(CD, "dc", "Dreamcast boot header")
            for off in XBOX_OFFSETS:
                if off + 20 <= size and _read(fh, off, 20) == XBOX_MAGIC:
                    # XGD1 = original Xbox, XGD2/XGD3 = Xbox 360; a trimmed XISO (0x10000) could be either.
                    sysid = {0x18310000: "xbox", 0xFDA0000: "xbox360", 0x2090000: "xbox360"}.get(off, "")
                    kind = {0x18310000: "XGD1", 0xFDA0000: "XGD2", 0x2090000: "XGD3"}.get(off, "XISO")
                    return Detection(DVD, sysid, f"Xbox media header ({kind})")
            pvd = _pvd(fh)
            if not pvd:
                return None
            sys_id = pvd[8:40].decode("ascii", "replace").strip()
            vol_bytes = struct.unpack_from("<I", pvd, 80)[0] * SECTOR
            if sys_id.startswith("PSP GAME") or _find_in_root(fh, pvd, "UMD_DATA.BIN"):
                return Detection(DVD, "psp", "disc says PSP GAME")
            if sys_id.startswith("PLAYSTATION"):
                cnf = _system_cnf(fh, pvd) or ""
                if "BOOT2" in cnf.upper():
                    media = DVD if max(vol_bytes, size) > CD_MAX else ""
                    return Detection(media or DVD, "ps2", "SYSTEM.CNF boots with BOOT2",
                                     confident=bool(media))
                if "BOOT" in cnf.upper():
                    return Detection(CD, "psx", "SYSTEM.CNF boots with BOOT",
                                     warning="PS1 .iso files lack raw sectors/audio; a .cue/.bin dump is "
                                             "preferred")
                return Detection(CD if size <= CD_MAX else DVD, "", "PlayStation volume without SYSTEM.CNF",
                                 confident=False)
    except OSError:
        return None
    return None


# ---------------------------------------------------------------- DAT lookup (PS2 CD vs DVD)

def from_dat(path: Path, index) -> str | None:
    """Redump lists PS2 DVD games as .iso and CD games as .bin/.cue."""
    if index is None:
        return None
    g = index.game_named(path.stem)
    if not g:
        return None
    exts = {Path(r.name).suffix.lower() for r in g.roms}
    if ".iso" in exts:
        return DVD
    if exts & {".bin", ".cue"}:
        return CD
    return None


# ---------------------------------------------------------------- main entry

def detect(path: Path, folder_system: str = "", dat_index=None) -> Detection:
    """Best guess for a disc image. folder_system: catalog id of the library folder, if known."""
    ext = path.suffix.lower()
    if not folder_system:
        s = guess_system(path.parent.name)
        folder_system = s.id if s else ""

    if ext == ".gdi":
        return Detection(CD, "dc", "GD-ROM descriptor (.gdi)")
    if ext in (".cue", ".toc", ".nrg", ".ccd"):
        d = Detection(CD, folder_system if SYSTEM_MEDIA.get(folder_system, CD) == CD or folder_system == "ps2"
                      else "", f"{ext} files only describe CDs")
        return _warn(d)

    d = from_contents(path)
    if d and d.system == "ps2" and not d.confident:
        dat_media = from_dat(path, dat_index)
        if dat_media:
            d = Detection(dat_media, "ps2", f"BOOT2 + Redump lists it as {'an .iso (DVD)' if dat_media == DVD else '.bin/.cue (CD)'}")
        else:
            d.reason += "; small image not in DAT - DVD is the safe choice for PCSX2"
    if d:
        if not d.system and folder_system:
            d.system = folder_system
        return _warn(d)

    if folder_system == "ps2":
        dat_media = from_dat(path, dat_index)
        if dat_media:
            return _warn(Detection(dat_media, "ps2", "ps2 folder + Redump DAT entry"))
    if folder_system in SYSTEM_MEDIA:
        return _warn(Detection(SYSTEM_MEDIA[folder_system], folder_system, f"in the {path.parent.name}/ folder"))
    try:
        big = path.stat().st_size > CD_MAX
    except OSError:
        big = True
    if big:
        return _warn(Detection(DVD, folder_system, "larger than any CD"))
    return _warn(Detection(DVD, folder_system, "unrecognised small image - DVD guessed; force CD if it's a "
                                               "data CD", confident=False))


def _warn(d: Detection) -> Detection:
    if d.system in NO_CHD_SUPPORT and not d.warning:
        d.warning = NO_CHD_SUPPORT[d.system]
    return d


# ---------------------------------------------------------------- existing CHDs: right type?

CD_MAX_FRAMES = 445_500   # 99 minutes; nothing pressed on CD is longer


@dataclass
class MediaCheck:
    actual: str          # cd | gd | dvd
    expected: str        # cd | gd | dvd
    reason: str
    fixable: bool = False
    fix_note: str = ""

    @property
    def ok(self) -> bool:
        fam = {"gd": "cd"}  # GD-ROM is CD-family for the purposes of "CD vs DVD"
        if self.expected == "gd" or self.actual == "gd":
            return self.expected == self.actual or (self.expected == "cd" and self.actual == "gd")
        return fam.get(self.actual, self.actual) == fam.get(self.expected, self.expected)

    @property
    def label(self) -> str:
        if self.ok:
            return self.actual.upper()
        return f"{self.actual.upper()} → should be {self.expected.upper()}"

    def to_dict(self) -> dict:
        return {"actual": self.actual, "expected": self.expected, "reason": self.reason,
                "fixable": self.fixable, "fix_note": self.fix_note}


def check_chd(info: dict, system: str = "", game=None) -> MediaCheck | None:
    """Is this CHD the right type? info: ChdInfo.as_dict(); game: matched DAT Game (or None)."""
    actual = info.get("kind", "")
    if actual not in ("cd", "gd", "dvd"):
        return None
    frames = int(info.get("frames") or 0)
    types = tuple(info.get("track_types") or ())

    expected, reason = "", ""
    if actual == "cd" and frames > CD_MAX_FRAMES:
        expected = DVD
        reason = f"{frames * 2048 / 1024 ** 3:.1f} GB of data — more than any CD can hold"
    if not expected and game is not None:
        exts = {Path(r.name).suffix.lower() for r in game.roms}
        if ".iso" in exts:
            expected, reason = DVD, "Redump lists this game as an .iso (DVD)"
        elif exts & {".bin", ".cue", ".gdi", ".raw"}:
            expected = "gd" if system == "dc" else CD
            reason = f"Redump lists this game as {'a GD-ROM' if system == 'dc' else '.bin/.cue (CD)'}"
    if not expected and system:
        sys_media = "gd" if system == "dc" else SYSTEM_MEDIA.get(system, "")
        if sys_media:
            expected, reason = sys_media, f"{SYSTEM_NAMES.get(system, system)} games are {sys_media.upper()}s"
    if not expected:
        return None

    mc = MediaCheck(actual, expected, reason)
    if mc.ok:
        return mc
    if actual == "cd" and expected == DVD:
        if types == ("MODE1",):
            mc.fixable = True
            mc.fix_note = "single 2048-byte data track (made from an .iso) — converts to DVD losslessly"
        else:
            mc.fix_note = f"can't convert automatically: tracks are {', '.join(types) or 'unknown'}"
    elif actual == DVD and expected in (CD, "gd"):
        if expected == "gd":
            mc.fix_note = "can't convert a DVD CHD to a GD-ROM; re-create it from the original .gdi/.cue"
        else:
            mc.fixable = True
            mc.fix_note = ("becomes a single-track data CD (same bytes); a raw .cue/.bin dump would be needed "
                           "for a Redump-exact CD")
    elif actual == "cd" and expected == "gd":
        mc.fix_note = "Dreamcast discs need a GD-ROM CHD; re-create it from the original .gdi/.cue"
    return mc
