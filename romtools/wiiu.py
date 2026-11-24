"""Identifying Wii U games from their own data - no keys, no DAT, nothing extracted.

  .wua            Cemu's archive (ZArchive): one folder per title, "0005000010143500_v0/{code,content,meta}". The folder
                  list isn't compressed; meta/meta.xml (name, region, product code) is a few zstd blocks
  code/content/meta folders      the same meta.xml, unpacked
  title.tmd + .app files         title ID and version only: the rest is encrypted

Disc images (.wud / .wux) are encrypted too; a plain .wud is checked with Redump's DAT like other discs.
"""

from __future__ import annotations

import hashlib
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

MAGIC, VERSION_1 = 0x169F52D6, 0x61BF3A01
FOOTER, BLOCK, PER_RECORD = 144, 0x10000, 16
MAX_TABLE = 256 << 20                   # no real archive comes near this
MAX_XML = 4 << 20

_REGIONS = ((1, "Japan"), (2, "USA"), (4, "Europe"), (8, "Australia"), (0x10, "China"), (0x20, "Korea"), (0x40, "Taiwan"))


class WiiUError(Exception):
    pass


class NoZstd(WiiUError):
    """This Python has no zstd (built in since 3.14): names inside .wua files can't be read."""


def _unpack(data: bytes) -> bytes:
    try:
        from compression import zstd
    except ImportError:
        try:
            import zstandard
        except ImportError as exc:
            raise NoZstd("reading names inside .wua files needs Python 3.14 or the zstandard module") from exc
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=BLOCK)
    d = zstd.ZstdDecompressor()
    return d.decompress(data, BLOCK + 1)


class ZArchive:
    """Read-only access to a .wua file. Every offset is checked before it's used."""

    def __init__(self, path: Path):
        self.f = open(path, "rb")
        try:
            self._open(Path(path).stat().st_size)
        except BaseException:
            self.f.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.f.close()

    def _at(self, off: int, n: int) -> bytes:
        self.f.seek(off)
        d = self.f.read(n)
        if len(d) != n:
            raise WiiUError("the archive ends too early")
        return d

    def _open(self, size: int) -> None:
        if size < FOOTER:
            raise WiiUError("not a Wii U archive (.wua)")
        ft = self._at(size - FOOTER, FOOTER)
        total, ver, magic = struct.unpack_from(">QII", ft, 128)
        if magic != MAGIC or ver != VERSION_1:
            raise WiiUError("not a Wii U archive (.wua), or an incomplete one")
        if total != size:
            raise WiiUError("the archive isn't complete (its size isn't the one it says)")
        sections = [struct.unpack_from(">QQ", ft, 16 * i) for i in range(6)]
        if any(o + n > size - FOOTER for o, n in sections) or any(n > MAX_TABLE for _o, n in sections[1:4]):
            raise WiiUError("damaged archive: a table lies outside it")
        self.data_at, self.data_size = sections[0]
        self.records, self.names, self.tree = (self._at(o, n) for o, n in sections[1:4])
        self.sha = ft[96:128]

    def _name(self, o: int) -> str:
        n = self.names[o]
        if n & 0x80:
            n, o = (n & 0x7F) | self.names[o + 1] << 7, o + 1
        return self.names[o + 1:o + 1 + n].decode("utf-8", "replace")

    def _entry(self, i: int) -> tuple[int, int, int, int]:
        if not 0 <= 16 * i + 16 <= len(self.tree):
            raise WiiUError("damaged archive: folder list")
        return struct.unpack_from(">IIII", self.tree, 16 * i)

    def list(self, i: int = 0) -> dict[str, int]:
        """Names in a folder -> entry numbers (0 is the root)."""
        e = self._entry(i)
        if e[0] >> 31 and i:
            raise WiiUError("not a folder")
        if e[2] > 1_000_000:
            raise WiiUError("damaged archive: folder list")
        return {self._name(self._entry(k)[0] & 0x7FFFFFFF): k for k in range(e[1], e[1] + e[2])}

    def find(self, *parts: str, start: int = 0) -> int | None:
        for p in parts:
            names = {n.lower(): k for n, k in self.list(start).items()}
            if p.lower() not in names:
                return None
            start = names[p.lower()]
        return start

    def _block(self, b: int) -> bytes:
        rec = self.records[40 * (b // PER_RECORD):40 * (b // PER_RECORD) + 40]
        if len(rec) < 40:
            raise WiiUError("damaged archive: block table")
        sizes = struct.unpack_from(">16H", rec, 8)
        off = struct.unpack_from(">Q", rec)[0] + sum(s + 1 for s in sizes[:b % PER_RECORD])
        n = sizes[b % PER_RECORD] + 1
        if off + n > self.data_size:
            raise WiiUError("damaged archive: a block lies outside it")
        d = self._at(self.data_at + off, n)
        if n == BLOCK:
            return d
        try:
            d = _unpack(d)
        except NoZstd:
            raise
        except Exception as exc:  # noqa: BLE001 - whatever this zstd module raises
            raise WiiUError(f"a block doesn't unpack: {exc}") from exc
        if len(d) != BLOCK:
            raise WiiUError("a block unpacks to the wrong size")
        return d

    def read(self, i: int, limit: int = MAX_XML) -> bytes:
        e = self._entry(i)
        if not e[0] >> 31:
            raise WiiUError("not a file")
        off, size = e[1] | ((e[3] >> 16) << 32), e[2] | ((e[3] & 0xFFFF) << 32)
        if size > limit:
            raise WiiUError("file larger than expected")
        out = b""
        while len(out) < size:
            b, d = divmod(off + len(out), BLOCK)
            out += self._block(b)[d:]
        return out[:size]


def verify(path: Path, progress: Callable[[int, int], None] = lambda done, total: None, cancel=None) -> tuple[str, str]:
    """("intact" | "damaged", explanation): the archive's own SHA-256 over everything in it."""
    try:
        with ZArchive(path) as z:
            want = z.sha
    except WiiUError as exc:
        return "damaged", str(exc)
    size = Path(path).stat().st_size
    h, left = hashlib.sha256(), size - FOOTER
    with open(path, "rb") as f:
        while left > 0:
            if cancel is not None and cancel.is_set():
                from .hashing import Cancelled
                raise Cancelled()
            d = f.read(min(8 << 20, left))
            if not d:
                return "damaged", "the archive ends too early"
            h.update(d)
            left -= len(d)
            progress(size - FOOTER - left, size)
        ft = f.read(FOOTER)
    h.update(ft[:96] + bytes(32) + ft[128:])
    if h.digest() != want:
        return "damaged", "its data doesn't match the SHA-256 stored in it"
    return "intact", "all of it matches the SHA-256 stored in it"


# ---------------------------------------------------------------- titles

def kind_of(title_id: str) -> str:
    from .packages import DLC, GAME, UPDATE
    return {"0005000E": UPDATE, "0005000C": DLC}.get(title_id[:8].upper(), GAME)


def base_of(title_id: str) -> str:
    return "00050000" + title_id[8:].upper()


def region_text(mask: int) -> str:
    if mask & 7 == 7:
        return "World"
    return ", ".join(n for b, n in _REGIONS if mask & b)


def parse_meta(data: bytes) -> dict:
    """What's needed from a meta.xml: title_id, version, title, region, product."""
    from .packages import clean_title
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:      # real ones have none: nothing an entity could do here is wanted
        raise WiiUError("unexpected meta.xml")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise WiiUError(f"meta.xml can't be read: {exc}") from exc

    def get(tag: str) -> str:
        return (root.findtext(tag) or "").strip()
    tid = get("title_id").upper()
    if not re.fullmatch(r"[0-9A-F]{16}", tid):
        raise WiiUError("meta.xml has no title ID")
    name = next((get(t) for t in ("longname_en", "longname_ja", "shortname_en", "longname_fr", "longname_de",
                                  "longname_es", "longname_it") if get(t)), "")
    lines = [clean_title(x) for x in name.splitlines() if x.strip()]
    return {"title_id": tid, "version": int(get("title_version") or 0) if get("title_version").isdigit() else 0,
            "title": " - ".join(lines), "region": region_text(int(get("region") or "0", 16)
                                                              if re.fullmatch(r"[0-9A-Fa-f]{1,8}", get("region")) else 0),
            "product": get("product_code") if get("product_code").startswith("WUP-") else ""}


def tmd_title(d: Path) -> tuple[str, int] | None:
    """(title ID, version) from a title.tmd, if it's a Wii U one."""
    try:
        with open(d / "title.tmd", "rb") as f:
            h = f.read(0x1E0)
    except OSError:
        return None
    if len(h) < 0x1E0 or h[:4] != b"\0\x01\0\x04" or h[0x18C:0x18E] != b"\0\x05":
        return None
    return h[0x18C:0x194].hex().upper(), struct.unpack_from(">H", h, 0x1DC)[0]


def is_extracted(d: Path) -> bool:
    """A Wii U game as folders: code/content/meta, or title.tmd with its .app files."""
    try:
        if (d / "meta" / "meta.xml").is_file() and (d / "code").is_dir():
            return True
        return tmd_title(d) is not None and any(d.glob("*.app"))
    except OSError:
        return False


def read(path: Path):
    """PackageInfo of a .wua file or an extracted game's folder."""
    from .packages import DLC, GAME, UPDATE, Content, PackageError, PackageInfo
    path = Path(path)
    titles: list[dict] = []
    note = ""
    try:
        if path.is_dir():
            xml = path / "meta" / "meta.xml"
            if xml.is_file():
                with open(xml, "rb") as f:
                    titles = [parse_meta(f.read(MAX_XML))]
            else:
                t = tmd_title(path)
                if t is None:
                    raise WiiUError("no meta.xml or Wii U title.tmd in it")
                titles, note = [{"title_id": t[0], "version": t[1]}], "encrypted: only its ID and version can be read"
        else:
            with ZArchive(path) as z:
                for name, entry in z.list().items():
                    m = re.fullmatch(r"([0-9a-fA-F]{16})_v(\d+)", name)
                    if not m:
                        continue
                    t = {"title_id": m[1].upper(), "version": int(m[2])}
                    try:
                        meta = z.find("meta", "meta.xml", start=entry)
                        if meta is not None:
                            t = {**parse_meta(z.read(meta)), **t}          # the folder's ID and version count
                    except NoZstd as exc:
                        note = str(exc)
                    titles.append(t)
            if not titles:
                raise WiiUError("no Wii U titles in the archive")
    except WiiUError as exc:
        raise PackageError(str(exc)) from exc
    except (OSError, struct.error, IndexError, ValueError) as exc:
        raise PackageError(f"couldn't be read: {exc}") from exc
    order = {GAME: 0, UPDATE: 1, DLC: 2}
    titles.sort(key=lambda t: (order[kind_of(t["title_id"])], t["title_id"]))
    main = titles[0]
    contents = [Content(kind_of(t["title_id"]), t["title_id"], t["version"], f"v{t['version']}") for t in titles]
    kinds = [c.kind for c in contents]
    info = PackageInfo("wiiu", str(path), title=next((t["title"] for t in titles if t.get("title")), ""),
                       title_id=main["title_id"], base_id=base_of(main["title_id"]), kind=kinds[0],
                       version=main["version"], version_str=f"v{main['version']}", contents=contents,
                       region=next((t["region"] for t in titles if t.get("region")), ""),
                       product=next((t["product"] for t in titles if t.get("product")), ""), note=note)
    if kinds[0] == GAME and len(kinds) > 1:
        n = kinds.count(DLC)
        info.kind = " + ".join([GAME] + ([UPDATE] if UPDATE in kinds else []) + (["DLC"] if n == 1 else [f"{n} DLC"] if n else []))
    if not info.title:
        info.title = re.sub(r"\s*[\[(][^\])]*[\])]", "", path.name if path.is_dir() else path.stem).strip()
        info.note = info.note or "name taken from the file name"
    return info
