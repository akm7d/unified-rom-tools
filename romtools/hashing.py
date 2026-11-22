"""Streaming CRC32/MD5/SHA-1 hashing for plain files and archive members.

Copier/iNES-style headers are detected and a second, headerless hash set is
computed in the same pass, so headered and headerless DATs both match.
"""

from __future__ import annotations

import hashlib
import shutil
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
    headerless: HashSet | None = None
    header_len: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Entry":
        return Entry(d["name"], HashSet(**d["full"]),
                     HashSet(**d["headerless"]) if d.get("headerless") else None, d.get("header_len", 0))

    def variants(self):
        yield self.full
        if self.headerless:
            yield self.headerless


HEADER_EXTS = {".nes", ".fds", ".lnx", ".lyx", ".a78", ".sfc", ".smc", ".fig", ".swc", ".unf", ".unif"}


def detect_header(name: str, first: bytes, total: int | None) -> int:
    ext = Path(name).suffix.lower()
    if ext not in HEADER_EXTS:
        return 0
    if first[:4] == b"NES\x1a":
        return 16
    if first[:4] == b"FDS\x1a":
        return 16
    if first[:4] == b"LYNX":
        return 64
    if first[1:10] == b"ATARI7800":
        return 128
    if ext in (".sfc", ".smc", ".fig", ".swc") and total is not None and total % 1024 == 512:
        return 512
    return 0


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
    hlen = detect_header(name, first, total)
    nohdr = _Hasher() if hlen else None
    buf = first
    while buf:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        full.update(buf)
        if nohdr is not None:
            nohdr.update(buf[hlen:] if full.size == len(buf) + prefix_zeros else buf)
        if progress:
            progress(len(buf))
        buf = fh.read(CHUNK)
    return Entry(name, full.result(), nohdr.result() if nohdr else None, hlen)


def hash_file(path: Path, cancel=None, progress=None, name: str | None = None, prefix_zeros: int = 0) -> Entry:
    size = path.stat().st_size
    with open(path, "rb", buffering=0) as fh:
        return hash_stream(fh, name or path.name, size, cancel, progress, prefix_zeros)


# ---------------------------------------------------------------- archives

ARCHIVE_EXTS = {".zip", ".7z", ".rar"}
SEVENZIP = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


def hash_zip(path: Path, cancel=None, progress=None) -> list[Entry]:
    out = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as fh:
                out.append(hash_stream(fh, info.filename, info.file_size, cancel, progress))
    return out


def _7z_list(path: Path) -> list[tuple[str, int]]:
    res = subprocess.run([SEVENZIP, "l", "-slt", "-ba", str(path)], capture_output=True, text=True,
                         errors="replace", check=False)
    if res.returncode != 0:
        raise RuntimeError(f"7z list failed: {res.stderr.strip() or res.stdout.strip()}")
    items, cur = [], {}
    for line in res.stdout.splitlines() + [""]:
        if not line.strip():
            if cur.get("Path") and cur.get("Folder", "-") != "+" and "D" not in cur.get("Attributes", ""):
                items.append((cur["Path"], int(cur.get("Size") or 0)))
            cur = {}
            continue
        k, _, v = line.partition(" = ")
        cur[k.strip()] = v
    return items


def hash_7z(path: Path, cancel=None, progress=None) -> list[Entry]:
    if not SEVENZIP:
        raise RuntimeError("7z is not installed (pacman -S 7zip)")
    out = []
    for member, size in _7z_list(path):
        proc = subprocess.Popen([SEVENZIP, "e", "-so", "-bd", str(path), member],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            out.append(hash_stream(proc.stdout, member, size, cancel, progress))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if proc.returncode not in (0, None, -9):
            raise RuntimeError(f"7z failed extracting {member}")
    return out


def hash_archive(path: Path, cancel=None, progress=None) -> list[Entry]:
    if path.suffix.lower() == ".zip":
        try:
            return hash_zip(path, cancel, progress)
        except (zipfile.BadZipFile, NotImplementedError):
            if not SEVENZIP:
                raise
    return hash_7z(path, cancel, progress)
