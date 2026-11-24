"""Checks the BIOS / firmware files you already have against known good hashes.

The list comes from the libretro database (System.dat, which names the files RetroArch's cores look for, per system). It is
downloaded when first used; no BIOS is included, downloaded, renamed or changed.
"""

from __future__ import annotations

import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR
from .hashing import hash_file

URL = "https://raw.githubusercontent.com/libretro/libretro-database/master/dat/System.dat"
LIST = DATA_DIR / "bios" / "System.dat"

GOOD, MISNAMED, WRONG = "Good", "Good, other name", "Wrong data"


@dataclass
class Bios:
    system: str
    name: str                   # as the emulator core expects it, may include a folder
    size: int
    md5: str
    sha1: str

    @property
    def file(self) -> str:
        return self.name.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass
class Found:
    state: str
    path: Path
    bios: Bios


@dataclass
class SystemReport:
    system: str
    known: list[Bios]
    found: list[Found] = field(default_factory=list)

    @property
    def good(self) -> list[Found]:
        return [f for f in self.found if f.state in (GOOD, MISNAMED)]


def download(timeout: int = 60) -> Path:
    req = urllib.request.Request(URL, headers={"User-Agent": "romtools/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(8 << 20)
    if not parse(data.decode("utf-8", "replace")):
        raise RuntimeError("the server didn't return the BIOS list")
    LIST.parent.mkdir(parents=True, exist_ok=True)
    tmp = LIST.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, LIST)
    return LIST


def list_age_days() -> int | None:
    try:
        return int((time.time() - LIST.stat().st_mtime) // 86400)
    except OSError:
        return None


_ROM = re.compile(r'rom\s*\(\s*name\s+(?:"([^"]+)"|(\S+))(.*?)\)\s*$')
_VAL = re.compile(r"\b(size|crc|md5|sha1)\s+([0-9A-Fa-f]+)")


def parse(text: str) -> list[Bios]:
    """The "System" DAT: one game whose ROMs are divided into systems by comments."""
    out, system, inside = [], "", False
    for line in text.splitlines():
        ln = line.strip()
        if ln.startswith("game ("):
            inside = True
        m = re.match(r'comment\s+"([^"]+)"', ln)
        if m and inside:
            system = m[1]
            continue
        m = _ROM.match(ln)
        if m and inside:
            v = dict(_VAL.findall(m[3]))
            if v.get("md5") or v.get("sha1"):
                out.append(Bios(system, m[1] or m[2], int(v.get("size", 0)), v.get("md5", "").lower(),
                                v.get("sha1", "").lower()))
    return out


def known() -> list[Bios]:
    return parse(LIST.read_text(encoding="utf-8", errors="replace"))


def usual_folders(library: Path | None = None) -> list[Path]:
    """Folders where emulators keep their BIOS files, as far as they exist here."""
    h = Path.home()
    cands = [h / ".config/retroarch/system", h / ".var/app/org.libretro.RetroArch/config/retroarch/system",
             h / ".config/PCSX2/bios", h / ".var/app/net.pcsx2.PCSX2/config/PCSX2/bios",
             h / ".local/share/duckstation/bios", h / ".config/duckstation/bios",
             h / ".var/app/org.duckstation.DuckStation/config/duckstation/bios",
             h / ".local/share/flycast/data", h / ".config/mednafen/firmware", h / ".mednafen/firmware",
             h / "Documents/PCSX2/bios", h / "Documents/DuckStation/bios", h / "AppData/Roaming/RetroArch/system",
             h / "RetroArch/system"]
    if library:
        cands += [library / "bios", library.parent / "bios", library / "BIOS", library.parent / "BIOS"]
    out: list[Path] = []
    for c in cands:
        try:
            if c.is_dir() and not any(c.samefile(o) for o in out):
                out.append(c)
        except OSError:
            continue
    return out


def check(folders: list[Path], bios: list[Bios], progress=lambda msg: None, cancel=None) -> list[SystemReport]:
    """Compare the files in these folders (a few levels deep) with the list."""
    by_md5: dict[str, list[Bios]] = {}
    by_sha1: dict[str, list[Bios]] = {}
    by_name: dict[str, list[Bios]] = {}
    sizes = {b.size for b in bios}
    reports: dict[str, SystemReport] = {}
    for b in bios:
        reports.setdefault(b.system, SystemReport(b.system, [])).known.append(b)
        if b.md5:
            by_md5.setdefault(b.md5, []).append(b)
        if b.sha1:
            by_sha1.setdefault(b.sha1, []).append(b)
        by_name.setdefault(b.file.lower(), []).append(b)
    seen: set[Path] = set()
    for folder in folders:
        for p in _files(folder):
            if cancel is not None and cancel.is_set():
                return list(reports.values())
            try:
                real, size = p.resolve(), p.stat().st_size
            except OSError:
                continue
            named = by_name.get(p.name.lower(), [])
            if real in seen or size > 64 << 20 or not (size in sizes or named):
                continue
            seen.add(real)
            progress(f"Checking {p.name}")
            try:
                h = hash_file(p).full
            except OSError:
                continue
            hits = by_sha1.get(h.sha1) or by_md5.get(h.md5) or []
            if hits:
                exact = [b for b in hits if b.file.lower() == p.name.lower()]
                for b in exact or hits[:1]:
                    reports[b.system].found.append(Found(GOOD if exact else MISNAMED, p, b))
            else:
                for b in named[:1]:
                    reports[b.system].found.append(Found(WRONG, p, b))
    return sorted(reports.values(), key=lambda r: (not r.found, r.system.lower()))


def _files(folder: Path, depth: int = 0):
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        return
    for e in entries:
        if e.is_file():
            yield Path(e.path)
        elif depth < 4 and e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
            yield from _files(Path(e.path), depth + 1)
