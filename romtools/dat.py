"""DAT file parsing (Logiqx XML and ClrMamePro) and hash indexing."""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

DESCRIPTOR_EXTS = (".cue", ".gdi", ".ccd", ".sub")  # not data, ignored when matching


@dataclass(slots=True)
class Rom:
    name: str
    size: int | None
    crc: str | None
    md5: str | None
    sha1: str | None
    status: str = ""

    @property
    def is_descriptor(self) -> bool:
        return self.name.lower().endswith(DESCRIPTOR_EXTS)


@dataclass(slots=True)
class Game:
    name: str
    description: str = ""
    roms: list[Rom] = field(default_factory=list)
    cloneof: str = ""
    dat: str = ""

    @property
    def data_roms(self) -> list[Rom]:
        return [r for r in self.roms if not r.is_descriptor]


@dataclass
class Dat:
    path: str
    name: str = ""
    version: str = ""
    games: list[Game] = field(default_factory=list)


def _norm(h: str | None) -> str | None:
    if not h:
        return None
    h = h.strip().lower()
    return h or None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _read_bytes(path: Path) -> bytes:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".dat", ".xml"))]
            if not names:
                raise ValueError(f"{path.name}: zip contains no .dat/.xml")
            return zf.read(names[0])
    return path.read_bytes()


def quick_count(path: str | Path) -> int:
    """Approximate game count without a full parse (for UI display)."""
    raw = _read_bytes(Path(path))
    return raw.count(b"<game ") + raw.count(b"<machine ") + len(re.findall(rb"(?m)^\s*game\s*\(", raw))


def parse_dat(path: str | Path) -> Dat:
    path = Path(path)
    raw = _read_bytes(path)
    head = raw[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    dat = Dat(str(path))
    if head.startswith(b"<"):
        _parse_xml(raw, dat)
    else:
        _parse_cmp(raw.decode("utf-8", errors="replace"), dat)
    if not dat.name:
        dat.name = path.stem
    for g in dat.games:
        g.dat = dat.name
    return dat


def _parse_xml(raw: bytes, dat: Dat) -> None:
    for _ev, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        tag = el.tag
        if tag == "header":
            dat.name = (el.findtext("name") or "").strip()
            dat.version = (el.findtext("version") or "").strip()
            el.clear()
        elif tag in ("game", "machine"):
            g = Game(el.get("name", ""), el.findtext("description") or "", cloneof=el.get("cloneof", ""))
            for r in el.iter("rom"):
                g.roms.append(Rom(r.get("name", ""), _int(r.get("size")), _norm(r.get("crc")),
                                  _norm(r.get("md5")), _norm(r.get("sha1")), r.get("status", "")))
            for d in el.iter("disk"):  # MAME-style CHD entries
                g.roms.append(Rom(d.get("name", "") + ".chd", None, None, _norm(d.get("md5")),
                                  _norm(d.get("sha1")), "disk"))
            dat.games.append(g)
            el.clear()


_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(\()|(\))|([^\s()"]+)')


def _parse_cmp(text: str, dat: Dat) -> None:
    """ClrMamePro format: nested `key ( ... )` blocks of key/value pairs."""
    tokens = []
    for m in _TOKEN.finditer(text):
        if m.group(1) is not None:
            tokens.append(("s", m.group(1)))
        elif m.group(2):
            tokens.append(("(", None))
        elif m.group(3):
            tokens.append((")", None))
        else:
            tokens.append(("s", m.group(4)))
    pos = 0

    def block():
        nonlocal pos
        items = []  # list of (key, value-or-list)
        while pos < len(tokens):
            kind, val = tokens[pos]
            if kind == ")":
                pos += 1
                return items
            if kind == "s":
                key = val
                pos += 1
                if pos < len(tokens) and tokens[pos][0] == "(":
                    pos += 1
                    items.append((key, block()))
                elif pos < len(tokens) and tokens[pos][0] == "s":
                    items.append((key, tokens[pos][1]))
                    pos += 1
                else:
                    items.append((key, ""))
            else:
                pos += 1
        return items

    top = block()
    for key, val in top:
        if not isinstance(val, list):
            continue
        d = {}
        if key == "clrmamepro":
            d = {k: v for k, v in val if not isinstance(v, list)}
            dat.name = d.get("name", "")
            dat.version = d.get("version", "")
            continue
        if key not in ("game", "machine", "resource"):
            continue
        g = Game("")
        for k, v in val:
            if k == "name" and not isinstance(v, list):
                g.name = v
            elif k == "description" and not isinstance(v, list):
                g.description = v
            elif k == "cloneof" and not isinstance(v, list):
                g.cloneof = v
            elif k in ("rom", "disk") and isinstance(v, list):
                rd = {}
                for rk, rv in v:
                    if not isinstance(rv, list):
                        rd.setdefault(rk, rv)
                name = rd.get("name", "")
                if k == "disk":
                    name += ".chd"
                g.roms.append(Rom(name, _int(rd.get("size")), _norm(rd.get("crc")), _norm(rd.get("md5")),
                                  _norm(rd.get("sha1")), "disk" if k == "disk" else rd.get("flags", "")))
        dat.games.append(g)


class DatIndex:
    """Fast lookup of DAT entries by hash, merged across any number of DATs."""

    def __init__(self, dats: list[Dat] | None = None) -> None:
        self.dats: list[Dat] = []
        self.by_sha1: dict[str, list[tuple[Game, Rom]]] = {}
        self.by_md5: dict[str, list[tuple[Game, Rom]]] = {}
        self.by_crc: dict[tuple[str, int | None], list[tuple[Game, Rom]]] = {}
        self.by_name: dict[str, list[Game]] = {}
        self._sizes: dict[str, list[int]] = {}
        for d in dats or []:
            self.add(d)

    def add(self, dat: Dat) -> None:
        self.dats.append(dat)
        for g in dat.games:
            self.by_name.setdefault(g.name.lower(), []).append(g)
            for r in g.roms:
                if r.sha1:
                    self.by_sha1.setdefault(r.sha1, []).append((g, r))
                if r.md5:
                    self.by_md5.setdefault(r.md5, []).append((g, r))
                if r.crc:
                    self.by_crc.setdefault((r.crc, r.size), []).append((g, r))

    @property
    def game_count(self) -> int:
        return sum(len(d.games) for d in self.dats)

    def lookup(self, size: int, crc: str, md5: str | None, sha1: str | None) -> list[tuple[Game, Rom]]:
        """Strongest hash wins; CRC+size only for DATs that list no SHA-1."""
        if sha1 and sha1 in self.by_sha1:
            return self.by_sha1[sha1]
        if md5 and md5 in self.by_md5:
            return [(g, r) for g, r in self.by_md5[md5] if not r.sha1 or r.sha1 == sha1]
        hits = self.by_crc.get((crc, size), [])
        return [(g, r) for g, r in hits if not r.sha1 and not r.md5]

    def game_named(self, stem: str, dats: set[str] | None = None) -> Game | None:
        """The game with this name. ``dats``: only from these DATs (by their name)."""
        return next((g for g in self.by_name.get(stem.lower(), []) if dats is None or g.dat in dats), None)

    def sizes(self, dat: str) -> list[int]:
        """Every ROM size a DAT lists, sorted."""
        if dat not in self._sizes:
            self._sizes[dat] = sorted({r.size for d in self.dats if d.name == dat for g in d.games
                                       for r in g.data_roms if r.size})
        return self._sizes[dat]
