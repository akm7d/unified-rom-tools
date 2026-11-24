"""Streaming CRC32/MD5/SHA-1 hashing for plain files and archive members.

Copier/iNES-style headers are detected and a second, headerless hash set is
computed in the same pass, so headered and headerless DATs both match.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import zipfile
import zlib
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import BinaryIO, Callable

CHUNK = 4 * 1024 * 1024


class Cancelled(Exception):
    pass


@dataclass(slots=True)
class HashSet:
    size: int
    crc: str
    md5: str
    sha1: str


@dataclass(slots=True)
class Entry:
    """One hashed piece of data: a file, an archive member, or a CHD track."""
    name: str
    full: HashSet
    headerless: HashSet | None = None    # the same data the way DATs list it (no copier header, .z64 byte order …)
    header_len: int = 0
    norm: str = ""                       # what was done for that, in words

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Entry":
        return Entry(d["name"], HashSet(**d["full"]),
                     HashSet(**d["headerless"]) if d.get("headerless") else None, d.get("header_len", 0), d.get("norm", ""))

    def variants(self):
        yield self.full
        if self.headerless:
            yield self.headerless


HASH_VERSION = 2       # 2: headers and byte order are recognised by content, not by extension; nested zips are opened


def detect_header(name: str, first: bytes, total: int | None) -> int:
    """Length of the copier / emulator header in front of the ROM, going by the data itself."""
    if first[:4] == b"NES\x1a":
        return 16
    if first[:4] == b"FDS\x1a":
        return 16
    if first[:4] == b"LYNX":
        return 64
    if first[1:10] == b"ATARI7800":
        return 128
    if total is not None and total % 1024 == 512 and 32768 < total <= 16 << 20:
        return 512              # a copier header (SNES, PC Engine, Mega Drive …): no cartridge has such a size
    return 0


def _normaliser(name: str, first: bytes, total: int | None):
    """(what it does in words, header bytes to drop, block size, function for each block) or None."""
    from .identify import n64_order, n64_swap, smd_decode
    if total is not None and total % 16384 == 512 and first[8:10] == b"\xaa\xbb" and \
            smd_decode(first[512:512 + 16384])[0x100:0x110].lstrip().startswith(b"SEGA"):
        return "decoded from the .smd copier format", 512, 16384, \
            lambda b: b"".join(smd_decode(b[i:i + 16384]) for i in range(0, len(b), 16384))
    hlen = detect_header(name, first, total)
    if hlen:
        return f"without its {hlen}-byte header", hlen, 1, None
    order = n64_order(first)
    if order in ("v64", "n64"):
        return f"in .z64 byte order (the file is .{order})", 0, 4, lambda b: n64_swap(b, order)
    return None


class _Hasher:
    __slots__ = ("crc", "md5", "sha1", "size")

    def __init__(self):
        self.crc = 0
        self.md5 = hashlib.md5()
        self.sha1 = hashlib.sha1()
        self.size = 0

    def update(self, b):
        if b:
            self.crc = zlib.crc32(b, self.crc)
            self.md5.update(b)
            self.sha1.update(b)
            self.size += len(b)

    def result(self) -> HashSet:
        return HashSet(self.size, f"{self.crc & 0xFFFFFFFF:08x}", self.md5.hexdigest(), self.sha1.hexdigest())


def hash_stream(fh: BinaryIO, name: str, total: int | None = None,
                cancel: threading.Event | None = None,
                progress: Callable[[int], None] | None = None, prefix_zeros: int = 0) -> Entry:
    """prefix_zeros: hash this many zero bytes first (a CD pregap the image leaves out but Redump includes)."""
    full = _Hasher()
    if prefix_zeros:
        full.update(bytes(prefix_zeros))
    first = fh.read(CHUNK)
    norm = _normaliser(name, first, total)
    what, drop, block, func = norm or ("", 0, 1, None)
    hlen = drop
    nohdr = _Hasher() if norm else None
    rest = b""                             # bytes of an unfinished block
    buf = first
    while buf:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        full.update(buf)
        if nohdr is not None:
            part = rest + buf[drop:]
            drop = 0
            cut = len(part) - len(part) % block
            rest = part[cut:]
            nohdr.update(func(part[:cut]) if func and cut else part[:cut])
        if progress:
            progress(len(buf))
        buf = fh.read(CHUNK)
    if nohdr is not None and rest:
        nohdr.update(rest)
    return Entry(name, full.result(), nohdr.result() if nohdr else None, hlen, what)


def hash_file(path: Path, cancel=None, progress=None, name: str | None = None, prefix_zeros: int = 0) -> Entry:
    size = path.stat().st_size
    with open(path, "rb", buffering=0) as fh:
        return hash_stream(fh, name or path.name, size, cancel, progress, prefix_zeros)


# ---------------------------------------------------------------- archives

ARCHIVE_EXTS = {".zip", ".7z", ".rar"}
from .compat import find_tool  # noqa: E402

SEVENZIP = find_tool("7z", "7zz", "7za")


NESTED_MAX = 512 << 20      # an archive inside an archive is read into memory, up to this size


def hash_zip(path, cancel=None, progress=None, depth: int = 0) -> list[Entry]:
    """Every file in a zip; a zip inside it is opened too (downloads are often packed twice)."""
    out = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as fh:
                inner = depth < 2 and info.file_size <= NESTED_MAX and fh.peek(4)[:4] == b"PK\x03\x04"
                if inner:
                    import io
                    data = io.BytesIO(fh.read())
                    try:
                        out += [Entry(f"{info.filename}/{e.name}", e.full, e.headerless, e.header_len, e.norm)
                                for e in hash_zip(data, cancel, progress, depth + 1)]
                        continue
                    except (zipfile.BadZipFile, NotImplementedError):
                        data.seek(0)
                        out.append(hash_stream(data, info.filename, info.file_size, cancel, progress))
                        continue
                out.append(hash_stream(fh, info.filename, info.file_size, cancel, progress))
    return out


class ArchiveError(RuntimeError):
    pass


def _7z_entries(path: Path) -> list[dict]:
    """What 7-Zip lists, in the order it unpacks: {"Path", "Size", "CRC", "Folder", "Encrypted", …}."""
    # -sccUTF-8: names as UTF-8 whatever the system's code page is; no standard input: it must never wait for a password
    res = subprocess.run([SEVENZIP, "l", "-slt", "-ba", "-sccUTF-8", "--", str(path)], capture_output=True,
                         encoding="utf-8", errors="surrogateescape", check=False, stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        said = (res.stderr.strip() or res.stdout.strip()).encode("utf-8", "replace").decode("utf-8")
        raise ArchiveError("the archive is protected with a password" if "password" in said.lower() or "break signaled" in said.lower() else
                           f"7z list failed: {said}")
    items, cur = [], {}
    for line in res.stdout.splitlines() + [""]:
        if not line.strip():
            if cur.get("Path") and cur.get("Folder", "-") != "+" and "D" not in cur.get("Attributes", ""):
                items.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" = ")
        cur[k.strip()] = v
    return items


def _7z_list(path: Path) -> list[tuple[str, int]]:
    return [(e["Path"], int(e.get("Size") or 0)) for e in _7z_entries(path)]


def member_command(path: Path, member: str, *more: str) -> list[str]:
    """7-Zip command that writes one file of an archive to its output: taken literally, whatever its name looks like
    (-spd: no wildcards; after "--" neither a switch nor a list file)."""
    return [SEVENZIP, "e", "-so", "-bd", "-spd", "-sccUTF-8", *more, "--", str(path), member]


class _Slice:
    """The next ``size`` bytes of a stream, as a file of its own."""

    def __init__(self, fh: BinaryIO, size: int):
        self.fh, self.left = fh, size

    def read(self, n: int) -> bytes:
        d = self.fh.read(min(n, self.left))
        self.left -= len(d)
        return d


def hash_7z(path: Path, cancel=None, progress=None) -> list[Entry]:
    if not SEVENZIP:
        raise ArchiveError("7z is not installed (pacman -S 7zip)")
    items = _7z_entries(path)
    if any(e.get("Encrypted") == "+" for e in items):
        raise ArchiveError("the archive is protected with a password")
    # all in one go (a solid archive would otherwise be unpacked from the start for every file in it), checked against the
    # checksums in its list - one by one if those don't agree with the order
    out = _hash_in_one_go(path, items, cancel, progress)
    return out if out is not None else [_hash_member(path, e, cancel, progress) for e in items]


def _hash_in_one_go(path: Path, items: list[dict], cancel, progress) -> list[Entry] | None:
    if len(items) < 2 or any(not e.get("CRC") and int(e.get("Size") or 0) for e in items):
        return None
    proc = subprocess.Popen([SEVENZIP, "e", "-so", "-bd", "-sccUTF-8", "--", str(path)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    out = []
    try:
        for e in items:
            size = int(e.get("Size") or 0)
            entry = hash_stream(_Slice(proc.stdout, size), e["Path"], size, cancel, progress)
            if entry.full.size != size or size and entry.full.crc != e["CRC"].lower():
                return None
            out.append(entry)
        return out if not proc.stdout.read(1) else None
    finally:
        proc.kill()
        proc.wait()


def _hash_member(path: Path, e: dict, cancel, progress) -> Entry:
    size = int(e.get("Size") or 0)
    proc = subprocess.Popen(member_command(path, e["Path"]), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL)
    try:
        entry = hash_stream(proc.stdout, e["Path"], size, cancel, progress)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if entry.full.size != size or e.get("CRC") and size and entry.full.crc != e["CRC"].lower():
        raise ArchiveError(f"7z couldn't unpack {e['Path']!r} (damaged archive?)")
    return entry


def hash_archive(path: Path, cancel=None, progress=None) -> list[Entry]:
    with open(path, "rb") as fh:
        is_zip = fh.read(4)[:2] == b"PK" or path.suffix.lower() == ".zip"
    if is_zip:
        try:
            return hash_zip(path, cancel, progress)
        except (zipfile.BadZipFile, NotImplementedError):
            if not SEVENZIP:
                raise
    return hash_7z(path, cancel, progress)
