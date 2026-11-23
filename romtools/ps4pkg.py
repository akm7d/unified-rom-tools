"""Independent reader for the encrypted file system inside PS4 packages (fPKG), used to check extractions.

Layers: PKG -> outer PFS (AES-XTS encrypted, signed) -> pfs_image.dat (PFSC: 64 KB zlib blocks) -> inner PFS -> files.

The file-system key of a fake / homebrew package is derived from its content ID and the packer's passcode (normally
32 zeros), so no console or Sony key material is needed and none is included. Official retail packages use a key
this derivation can't produce: they're reported as such. Nothing is ever written; every size and offset read from
a package is checked before it's used.

It shares no code with the bundled extractor, so a mistake in one doesn't hide in the other.
"""

from __future__ import annotations

import hashlib
import hmac
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DEFAULT_PASSCODE = "0" * 32
PFS_MAGIC = 20130315
PFSC_MAGIC = b"PFSC"
SECTOR = 0x1000
XTS_FIRST = 16                 # the first 64 KB (the superblock) isn't encrypted
NEW_CRYPT = 1 << 61            # pfs_flags bit: one more HMAC step in the key derivation
D32, S32 = 0xA8, 0x2C8         # inode sizes: plain / signed
T_FILE, T_DIR = 2, 3
MAX_INODES = 4_000_000


class Ps4PkgError(Exception):
    pass


class RetailPackage(Ps4PkgError):
    """The passcode doesn't open it: an official package, or a fake one made with another passcode."""


def _compute_key(content_id: str, passcode: str, index: int) -> bytes:
    return hashlib.sha256(hashlib.sha256(struct.pack(">I", index)).digest()
                          + hashlib.sha256(content_id.encode("ascii").ljust(48, b"\0")).digest()
                          + passcode.encode("ascii")).digest()


@dataclass(frozen=True)
class PkgFile:
    path: str                  # relative, "/"-separated
    size: int
    block: int                 # first block in the inner PFS


class _Pfs:
    """Inodes and directories of one PFS image, read through ``read(offset, size)``."""

    def __init__(self, read, what: str):
        self.read, self.what = read, what
        h = read(0, 0x400)
        version, magic = struct.unpack_from("<qq", h, 0)
        if magic != PFS_MAGIC:
            raise Ps4PkgError(f"{what}: not a PFS image")
        self.mode = struct.unpack_from("<H", h, 0x1C)[0]
        self.bs = struct.unpack_from("<I", h, 0x20)[0]
        self.n_inodes, _nd, self.inode_blocks, self.root = struct.unpack_from("<qqqq", h, 0x30)
        if self.bs not in (0x1000, 0x4000, 0x8000, 0x10000, 0x20000) or not 0 < self.n_inodes <= MAX_INODES:
            raise Ps4PkgError(f"{what}: damaged PFS header")
        if self.mode & 2:
            raise Ps4PkgError(f"{what}: 64-bit inodes aren't supported")
        self.isize = S32 if self.mode & 1 else D32
        self.per_block = self.bs // self.isize

    def inode(self, n: int) -> dict:
        if not 0 <= n < self.n_inodes:
            raise Ps4PkgError(f"{self.what}: inode {n} out of range")
        b = self.read((1 + n // self.per_block) * self.bs + (n % self.per_block) * self.isize, self.isize)
        mode, _nlink, flags = struct.unpack_from("<HHI", b, 0)
        size, csize = struct.unpack_from("<qq", b, 8)
        blocks = struct.unpack_from("<I", b, 0x60)[0]
        step, at = (36, 0x64 + 32) if self.isize == S32 else (4, 0x64)
        direct = [struct.unpack_from("<i", b, at + step * i)[0] for i in range(12)]
        # data is stored in one run: the following entries are -1 ("continues") or simply count up
        for i, d in enumerate(direct[1:min(12, blocks)], 1):
            if d not in (-1, 0, direct[0] + i):
                raise Ps4PkgError(f"{self.what}: inode {n} isn't stored in one piece (not supported)")
        if size < 0 or blocks < 0 or direct[0] < 0:
            raise Ps4PkgError(f"{self.what}: damaged inode {n}")
        return {"mode": mode, "flags": flags, "size": size, "csize": csize, "blocks": blocks, "block": direct[0]}

    def entries(self, n: int) -> Iterator[tuple[str, int, int]]:
        """(name, inode, type) of a directory."""
        ino = self.inode(n)
        if not stat.S_ISDIR(ino["mode"]):
            raise Ps4PkgError(f"{self.what}: inode {n} isn't a directory")
        for k in range(max(1, ino["blocks"])):
            data, p = self.read((ino["block"] + k) * self.bs, self.bs), 0
            while p + 16 <= len(data):
                i, typ, nlen, esize = struct.unpack_from("<iiii", data, p)
                if esize == 0:
                    break
                if not 16 <= esize <= len(data) - p or not 0 <= nlen <= esize - 16:
                    raise Ps4PkgError(f"{self.what}: damaged directory")
                if typ in (T_FILE, T_DIR):
                    yield data[p + 16:p + 16 + nlen].decode("utf-8", "replace"), i, typ
                p += esize

    def user_root(self) -> int:
        for name, i, typ in self.entries(self.root):
            if name == "uroot" and typ == T_DIR:
                return i
        raise Ps4PkgError(f"{self.what}: no user root directory")


class PkgReader:
    """``with PkgReader(path) as r: for f in r.files(): for chunk in r.chunks(f): ...``"""

    def __init__(self, path: Path, passcode: str = DEFAULT_PASSCODE):
        if len(passcode) != 32 or not passcode.isascii():
            raise Ps4PkgError("a passcode has exactly 32 characters")
        self.path = Path(path)
        self.f = open(path, "rb")
        try:
            self._open(passcode)
        except BaseException:
            self.f.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()

    def close(self):
        self.f.close()

    # -- package ------------------------------------------------------------
    def _open(self, passcode: str) -> None:
        h = self.f.read(0x500)
        if h[:4] != b"\x7fCNT" or len(h) < 0x500:
            raise Ps4PkgError("not a PS4 package")
        self.content_id = h[0x40:0x64].split(b"\0")[0].decode("ascii", "replace")
        self.pfs_flags = struct.unpack_from(">Q", h, 0x408)[0]
        self.image_offset, self.image_size = struct.unpack_from(">QQ", h, 0x410)
        self.image_digest = h[0x440:0x460]
        self.file_size = self.path.stat().st_size
        self._table(h)
        self.has_files = self.image_size > 0
        self.new_crypt = bool(self.pfs_flags & NEW_CRYPT)
        if not self.has_files:
            return                  # e.g. a DLC that is only an unlock licence
        if self.image_offset + self.image_size > self.file_size:
            raise Ps4PkgError("the package is incomplete (smaller than its header says)")
        self._keys(passcode)
        self.outer = _Pfs(self._read_outer, "outer file system")
        img = next((i for name, i, typ in self.outer.entries(self.outer.user_root())
                    if name == "pfs_image.dat" and typ == T_FILE), None)
        if img is None:
            raise Ps4PkgError("no pfs_image.dat inside")
        ino = self.outer.inode(img)
        self.pfsc_at, self.pfsc_size = ino["block"] * self.outer.bs, ino["blocks"] * self.outer.bs
        self._pfsc()
        self.inner = _Pfs(self._read_inner, "inner file system")

    def _table(self, h: bytes) -> None:
        n, off = struct.unpack_from(">I", h, 0x10)[0], struct.unpack_from(">I", h, 0x18)[0]
        self.entries: dict[int, tuple[int, int, bool]] = {}      # id -> (offset, size, encrypted)
        if n > 100000:
            raise Ps4PkgError("damaged package header")
        self.f.seek(off)
        t = self.f.read(32 * n)
        for k in range(len(t) // 32):
            eid, _fn, flags1, _f2, o, s = struct.unpack_from(">IIIIII", t, 32 * k)
            self.entries[eid] = (o, s, bool(flags1 & 0x80000000))

    def entry(self, eid: int) -> bytes | None:
        """A plain (unencrypted) package entry, e.g. 0x1000 = param.sfo, 0x1200 = icon0.png."""
        if eid not in self.entries or self.entries[eid][2]:
            return None
        o, s, _enc = self.entries[eid]
        self.f.seek(o)
        return self.f.read(s)

    def _keys(self, passcode: str) -> None:
        self.f.seek(self.image_offset)
        sb = self.f.read(0x400)
        if struct.unpack_from("<q", sb, 8)[0] != PFS_MAGIC:
            raise Ps4PkgError("no PFS image where the header says it is")
        seed = sb[0x370:0x380]
        ekpfs = _compute_key(self.content_id, passcode, 1)
        if self.new_crypt:
            ekpfs = hmac.new(ekpfs, seed, hashlib.sha256).digest()
        k = hmac.new(ekpfs, struct.pack("<I", 1) + seed, hashlib.sha256).digest()
        self._xts = k[16:] + k[:16]                # data key, tweak key
        # prove the key is right before trusting anything: the first inode must be a directory
        mode, nlink = struct.unpack_from("<HH", self._read_outer(0x10000, 0x1000), 0)
        if not stat.S_ISDIR(mode) or not 0 < nlink < 1000:
            raise RetailPackage("this package doesn't open with the default passcode: it's an official (retail) "
                                "package, or a fake one made with another passcode")

    # -- layers ------------------------------------------------------------
    def _read_outer(self, off: int, n: int) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        if off < 0 or n < 0 or off + n > self.image_size:
            raise Ps4PkgError("read outside the package's file system")
        first, last = off // SECTOR, (off + n + SECTOR - 1) // SECTOR
        self.f.seek(self.image_offset + first * SECTOR)
        raw = self.f.read((last - first) * SECTOR)
        out = bytearray()
        key = algorithms.AES(self._xts)
        for k in range(last - first):
            s = raw[k * SECTOR:(k + 1) * SECTOR]
            if first + k >= XTS_FIRST and len(s) == SECTOR:
                s = Cipher(key, modes.XTS((first + k).to_bytes(16, "little"))).decryptor().update(s)
            out += s
        d = off - first * SECTOR
        return bytes(out[d:d + n])

    def _pfsc(self) -> None:
        h = self._read_outer(self.pfsc_at, 0x30)
        if h[:4] != PFSC_MAGIC:
            raise Ps4PkgError("pfs_image.dat isn't in the expected (PFSC) format")
        self.cbs, offs, _data, length = struct.unpack_from("<QQQQ", h, 0x10)
        if self.cbs not in (0x1000, 0x4000, 0x8000, 0x10000, 0x20000) or length <= 0:
            raise Ps4PkgError("damaged PFSC header")
        n = (length + self.cbs - 1) // self.cbs
        if n > 1 << 24 or offs + 8 * (n + 1) > self.pfsc_size:
            raise Ps4PkgError("damaged PFSC block table")
        self.inner_size = length
        self.map = struct.unpack(f"<{n + 1}Q", self._read_outer(self.pfsc_at + offs, 8 * (n + 1)))
        if any(b < a for a, b in zip(self.map, self.map[1:])) or self.map[-1] > self.pfsc_size:
            raise Ps4PkgError("damaged PFSC block table")
        self._last: tuple[int, bytes] = (-1, b"")

    def _inner_block(self, i: int) -> bytes:
        if i == self._last[0]:
            return self._last[1]
        if not 0 <= i < len(self.map) - 1:
            raise Ps4PkgError("read outside the inner file system")
        a, size = self.map[i], self.map[i + 1] - self.map[i]
        if size > self.cbs:
            data = bytes(self.cbs)                           # stored as "all zeros"
        else:
            data = self._read_outer(self.pfsc_at + a, size)
            if size < self.cbs:
                try:
                    data = zlib.decompressobj().decompress(data, self.cbs)
                except zlib.error as exc:
                    raise Ps4PkgError(f"block {i} doesn't decompress: {exc}") from exc
                if len(data) != self.cbs:
                    raise Ps4PkgError(f"block {i} decompresses to the wrong size")
        self._last = (i, data)
        return data

    def _read_inner(self, off: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            i, d = divmod(off, self.cbs)
            part = self._inner_block(i)[d:d + n]
            out += part
            off += len(part)
            n -= len(part)
        return bytes(out)

    # -- files ------------------------------------------------------------
    def files(self) -> list[PkgFile]:
        """Every game file in the package (not the package's own sce_sys entries)."""
        if not self.has_files:
            return []
        out: list[PkgFile] = []
        seen: set[int] = set()

        def walk(n: int, prefix: str, depth: int):
            if n in seen or depth > 64:
                raise Ps4PkgError("inner file system: directory loop")
            seen.add(n)
            for name, i, typ in self.inner.entries(n):
                if name in ("", ".", "..") or "/" in name or "\\" in name or "\0" in name:
                    raise Ps4PkgError(f"unsafe file name in the package: {name!r}")
                if typ == T_DIR:
                    walk(i, prefix + name + "/", depth + 1)
                else:
                    ino = self.inner.inode(i)
                    if (ino["block"] * self.inner.bs + ino["size"]) > len(self.map) * self.cbs:
                        raise Ps4PkgError(f"{prefix + name}: outside the file system")
                    out.append(PkgFile(prefix + name, ino["size"], ino["block"]))
        walk(self.inner.user_root(), "", 0)
        return out

    def chunks(self, f: PkgFile) -> Iterator[bytes]:
        off, left = f.block * self.inner.bs, f.size
        while left > 0:
            n = min(left, self.cbs)
            yield self._read_inner(off, n)
            off += n
            left -= n


@dataclass
class Check:
    files: int = 0
    bytes: int = 0
    missing: list[str] = None            # in the package, not on disk
    different: list[str] = None          # content differs
    extra: list[str] = None              # on disk, not in the package (sce_sys entries are expected, not counted)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.different)


def check_extraction(pkg: Path, folder: Path, progress=lambda done, total, name: None, cancel=None,
                     passcode: str = DEFAULT_PASSCODE) -> Check:
    """Compare an extracted folder with the package, byte for byte, reading the package with this module."""
    c = Check(missing=[], different=[], extra=[])
    with PkgReader(pkg, passcode) as r:
        want = r.files()
        total, done = sum(f.size for f in want), 0
        for f in want:
            p = folder / f.path
            if not p.is_file():
                c.missing.append(f.path)
                done += f.size
                continue
            same = p.stat().st_size == f.size
            if same:
                with open(p, "rb") as fh:
                    for chunk in r.chunks(f):
                        if cancel is not None and cancel.is_set():
                            raise Ps4PkgError("cancelled")
                        if fh.read(len(chunk)) != chunk:
                            same = False
                            break
                        done += len(chunk)
                        progress(done, total, f.path)
            if not same:
                c.different.append(f.path)
            c.files += 1
            c.bytes += f.size
        sfo = r.entry(0x1000)
        disk_sfo = folder / "sce_sys" / "param.sfo"
        if sfo is not None and (not disk_sfo.is_file() or disk_sfo.read_bytes() != sfo):
            (c.different if disk_sfo.is_file() else c.missing).append("sce_sys/param.sfo")
        names = {f.path for f in want}
        c.extra = sorted(str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*") if p.is_file()
                         and str(p.relative_to(folder)).replace("\\", "/") not in names
                         and not str(p.relative_to(folder)).replace("\\", "/").startswith("sce_sys/"))
    return c
