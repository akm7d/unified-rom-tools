"""Reading and extracting the encrypted file system inside PS4 packages (fake / homebrew packages, fPKG).

Layers: PKG -> outer PFS (AES-XTS encrypted, signed) -> pfs_image.dat (PFSC: 64 KB zlib blocks) -> inner PFS -> files.

The keys of a fake / homebrew package are derived from its content ID and the packer's passcode (normally 32 zeros),
so no console or Sony key material is needed and none is included. Official retail packages use keys this derivation
can't produce: they're reported as such.

Nothing read from a package is trusted: every size and offset is checked before it's used, and every 64 KB block of
game data is verified against the signature the packer stored in the package (HMAC-SHA256) before it's used, and every
system entry against the package's digest table - so a damaged package, or a mistake in this reader, is an error
instead of wrong data.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import stat
import struct
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

DEFAULT_PASSCODE = "0" * 32
PFS_MAGIC = 20130315
PFSC_MAGIC = b"PFSC"
SECTOR = 0x1000
BLOCK = 0x10000
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


_PIECE = re.compile(r"^(.*)_(\d{1,2})$")


def pieces(path: Path) -> list[Path]:
    """All pieces of a split package ("…_0.pkg", "…_1.pkg", …), in order, whichever of them is given; [] when it isn't
    split. Only the first piece has a package header, which says how big the whole package is."""
    path = Path(path)
    m = _PIECE.match(path.stem)
    if not m or path.suffix.lower() != ".pkg":
        return []
    first = path.with_name(f"{m[1]}_0{path.suffix}")
    try:
        with open(first, "rb") as fh:
            h = fh.read(0x438)
        size = first.stat().st_size
    except OSError:
        return []
    if h[:4] != b"\x7fCNT" or len(h) < 0x438 or struct.unpack_from(">Q", h, 0x430)[0] <= size:
        return []                           # no header, or complete by itself: a name that just ends in a number
    out = []
    while (p := path.with_name(f"{m[1]}_{len(out)}{path.suffix}")).is_file():
        out.append(p)
    return out if path in out and len(out) > 1 else []


class _Joined:
    """Several files read as one."""

    def __init__(self, paths: list[Path]):
        self.files = []
        try:
            for p in paths:
                self.files.append((open(p, "rb"), p.stat().st_size))
        except OSError:
            self.close()
            raise
        self.size = sum(n for _f, n in self.files)
        self.pos = 0

    def seek(self, pos: int) -> int:
        self.pos = pos
        return pos

    def read(self, n: int) -> bytes:
        out, at = b"", 0
        for f, size in self.files:
            if n > 0 and at <= self.pos < at + size:
                f.seek(self.pos - at)
                d = f.read(min(n, at + size - self.pos))
                out += d
                self.pos += len(d)
                n -= len(d)
            at += size
        return out

    def close(self):
        for f, _n in self.files:
            f.close()


@dataclass(frozen=True)
class PkgFile:
    path: str                  # relative, "/"-separated
    size: int
    block: int                 # first block in the inner PFS


@dataclass(frozen=True)
class _Entry:
    eid: int
    name_at: int
    offset: int
    size: int
    encrypted: bool
    key_index: int
    raw: bytes
    digest: bytes = b""          # SHA-256 the package's digest table gives for it


MAX_ENTRY = 256 << 20
INTERNAL_ENTRIES = {0x1, 0x10, 0x20, 0x80, 0x100, 0x200}      # digests, keys, name table: no use outside the package
ENTRY_NAMES = {0x400: "license.dat", 0x401: "license.info", 0x402: "nptitle.dat", 0x403: "npbind.dat",
               0x404: "selfinfo.dat", 0x406: "imageinfo.dat", 0x407: "target-deltainfo.dat",
               0x408: "origin-deltainfo.dat", 0x409: "psreserved.dat"}


def safe_path(name: str) -> str:
    """A relative path from inside a package, refused if it could land anywhere but under the output folder."""
    parts = name.split("/")
    if not name or any(p in ("", ".", "..") or "\\" in p or ":" in p or any(ord(c) < 32 for c in p) for p in parts):
        raise Ps4PkgError(f"unsafe file name in the package: {name!r}")
    return name


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
        parts = pieces(self.path)
        self.f = _Joined(parts) if parts else open(path, "rb")
        self.file_size = self.f.size if parts else self.path.stat().st_size
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
        self.finalized = bool(h[4] & 0x80)         # set by Sony's own packaging, not by the fake / homebrew tools
        self.content_id = h[0x40:0x64].split(b"\0")[0].decode("ascii", "replace")
        self.pfs_flags = struct.unpack_from(">Q", h, 0x408)[0]
        self.image_offset, self.image_size = struct.unpack_from(">QQ", h, 0x410)
        self.image_digest = h[0x440:0x460]
        self._table(h)
        self.has_files = self.image_size > 0
        self.new_crypt = bool(self.pfs_flags & NEW_CRYPT)
        if not self.has_files:
            return                  # e.g. a DLC that is only an unlock licence
        if self.image_offset + self.image_size > self.file_size:
            raise Ps4PkgError("the package is incomplete (smaller than its header says)")
        self._keys(passcode)
        self._signed: dict[int, bytes] = {}          # outer block -> the signature it must have
        self._cache: tuple[int, bytes] = (-1, b"")
        self.outer = _Pfs(self._read_outer, "outer file system")
        if self.outer.bs != BLOCK:
            raise Ps4PkgError("outer file system: unexpected block size")
        img = next((i for name, i, typ in self.outer.entries(self.outer.user_root())
                    if name == "pfs_image.dat" and typ == T_FILE), None)
        if img is None:
            raise Ps4PkgError("no pfs_image.dat inside")
        ino = self.outer.inode(img)
        self.pfsc_at, self.pfsc_size = ino["block"] * self.outer.bs, ino["blocks"] * self.outer.bs
        self._signatures(img, ino)
        self._pfsc()
        self.inner = _Pfs(self._read_inner, "inner file system")

    def _table(self, h: bytes) -> None:
        n, off = struct.unpack_from(">I", h, 0x10)[0], struct.unpack_from(">I", h, 0x18)[0]
        self.entries: dict[int, _Entry] = {}
        if n > 100000:
            raise Ps4PkgError("damaged package header")
        self.f.seek(off)
        t = self.f.read(32 * n)
        for k in range(len(t) // 32):
            raw = t[32 * k:32 * k + 32]
            eid, name_at, flags1, flags2, o, s = struct.unpack_from(">IIIIII", raw, 0)
            if o + s > self.file_size:
                raise Ps4PkgError("the package is incomplete (smaller than its header says)")
            self.entries[eid] = _Entry(eid, name_at, o, s, bool(flags1 & 0x80000000), (flags2 >> 12) & 0xF, raw)
        # the digest table (entry 1) has one SHA-256 per entry, in table order; its own hash is in the header
        if 0x1 not in self.entries:
            raise Ps4PkgError("the package has no digest table (not supported)")
        d = self.entries[0x1]
        self.f.seek(d.offset)
        digests = self.f.read(min(d.size, 32 * n))
        if hashlib.sha256(digests).digest() != h[0x140:0x160] or len(digests) < 32 * n:
            raise Ps4PkgError("the package is damaged: its digest table doesn't match the header")
        for k in range(len(t) // 32):
            eid = struct.unpack_from(">I", t, 32 * k)[0]
            self.entries[eid] = replace(self.entries[eid], digest=digests[32 * k:32 * k + 32])

    def _raw(self, e: "_Entry", padded: bool = False) -> bytes:
        if e.size > MAX_ENTRY:
            raise Ps4PkgError("damaged package entry")
        self.f.seek(e.offset)
        data = self.f.read((e.size + 15) & ~15)       # encrypted entries are hashed with their padding
        if e.eid != 0x1 and e.digest not in (hashlib.sha256(data[:e.size]).digest(), hashlib.sha256(data).digest()):
            raise Ps4PkgError(f"the package is damaged: system entry {e.eid:#x} doesn't match its digest")
        return data if padded else data[:e.size]

    def entry(self, eid: int) -> bytes | None:
        """A system entry, e.g. 0x1000 = param.sfo, 0x1200 = icon0.png (decrypted if it's stored encrypted)."""
        e = self.entries.get(eid)
        if e is None:
            return None
        if not e.encrypted:
            return self._raw(e)
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        k = hashlib.sha256(e.raw + _compute_key(self.content_id, self._passcode, e.key_index)).digest()
        data = self._raw(e, padded=True)
        return Cipher(algorithms.AES(k[16:]), modes.CBC(k[:16])).decryptor().update(data)[:e.size]

    def system_files(self) -> list[tuple[str, int]]:
        """(path under sce_sys/, entry id) of the package's system files (not its internal key and digest tables)."""
        names = self._raw(self.entries[0x200]) if 0x200 in self.entries else b""
        out = []
        for e in self.entries.values():
            if e.eid in INTERNAL_ENTRIES:
                continue
            name = ENTRY_NAMES.get(e.eid, "")
            if not name and 0 < e.name_at < len(names):
                name = names[e.name_at:].split(b"\0", 1)[0].decode("utf-8", "replace")
            if name:
                out.append((safe_path(name), e.eid))
        return out

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
        self._sign_key = hmac.new(ekpfs, struct.pack("<I", 2) + seed, hashlib.sha256).digest()
        self._passcode = passcode
        # prove the key is right before trusting anything: the first inode must be a directory
        mode, nlink = struct.unpack_from("<HH", self._read_outer(0x10000, 0x1000), 0)
        if not stat.S_ISDIR(mode) or not 0 < nlink < 1000:
            if self.finalized:
                raise RetailPackage("this is an official (retail) package: its data is encrypted with Sony's keys")
            raise RetailPackage("this package doesn't open with the default passcode: it's an official (retail) "
                                "package, or a fake one made with another passcode")

    # -- layers ------------------------------------------------------------
    def _decrypt(self, first: int, count: int) -> bytes:
        """``count`` 4 KB sectors of the image, decrypted."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        self.f.seek(self.image_offset + first * SECTOR)
        raw = self.f.read(min(count * SECTOR, self.image_size - first * SECTOR))
        out, key = bytearray(), algorithms.AES(self._xts)
        for k in range(count):
            s = raw[k * SECTOR:(k + 1) * SECTOR]
            if first + k >= XTS_FIRST and len(s) == SECTOR:
                s = Cipher(key, modes.XTS((first + k).to_bytes(16, "little"))).decryptor().update(s)
            out += s
        return bytes(out)

    def _mac(self, data: bytes) -> bytes:
        return hmac.new(self._sign_key, data, hashlib.sha256).digest()

    def _read_outer(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0 or off + n > self.image_size:
            raise Ps4PkgError("read outside the package's file system")
        out = bytearray()
        while n > 0:
            b, d = divmod(off, BLOCK)
            want = getattr(self, "_signed", {}).get(b)
            if want is None:                          # not game data (headers, tables): just the sectors needed
                first, last = off // SECTOR, min(off + n, (b + 1) * BLOCK)
                last = (last + SECTOR - 1) // SECTOR
                part = self._decrypt(first, last - first)[off - first * SECTOR:][:min(n, BLOCK - d)]
            else:
                if self._cache[0] != b:
                    data = self._decrypt(b * (BLOCK // SECTOR), BLOCK // SECTOR)
                    if not hmac.compare_digest(self._mac(data), want):
                        raise Ps4PkgError(f"the package is damaged: block {b} doesn't match its signature")
                    self._cache = (b, data)
                part = self._cache[1][d:d + n]
            if not part:
                raise Ps4PkgError("the package ends too early")
            out += part
            off += len(part)
            n -= len(part)
        return bytes(out)

    def _signatures(self, img: int, ino: dict) -> None:
        """The signature of every block of pfs_image.dat (in its inode and the signed tables it points to)."""
        o, total = self.outer, ino["blocks"]
        if o.isize != S32:
            raise Ps4PkgError("the package's file system isn't signed (not supported)")
        raw = o.read((1 + img // o.per_block) * BLOCK + (img % o.per_block) * S32, S32)[0x64:]
        found: dict[int, bytes] = {}

        def entry(table: bytes, k: int) -> tuple[bytes, int]:
            return table[36 * k:36 * k + 32], struct.unpack_from("<i", table, 36 * k + 32)[0]

        def add(sig: bytes, block: int) -> None:
            if block != ino["block"] + len(found):
                raise Ps4PkgError("the package's game data isn't stored in one piece (not supported)")
            found[block] = sig

        def table(sig: bytes, block: int, depth: int) -> None:
            data = o.read(block * BLOCK, BLOCK)
            if not hmac.compare_digest(self._mac(data), sig):
                raise Ps4PkgError("the package is damaged: a signature table doesn't match its own signature")
            for k in range(BLOCK // 36):
                if len(found) >= total:
                    return
                (table(*entry(data, k), depth - 1) if depth else add(*entry(data, k)))
        for k in range(min(12, total)):
            add(*entry(raw, k))
        for level in range(5):
            if len(found) < total:
                table(*entry(raw, 12 + level), level)
        if len(found) != total:
            raise Ps4PkgError("the package's signature tables are incomplete")
        self._signed = found

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
                if "/" in name:
                    raise Ps4PkgError(f"unsafe file name in the package: {name!r}")
                safe_path(name)
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


@dataclass(frozen=True)
class _Planned:
    path: str                  # where it goes
    reader: "PkgReader"
    file: "PkgFile | None"     # a game file …
    eid: int = 0               # … or a system entry
    size: int = 0


def layers(readers: list["PkgReader"]) -> list[_Planned]:
    """What a game folder holds once these packages are put on top of each other (the game first, then its update):
    file by file, the later one wins - the way the console and emulators see an update. Windows and Samba ignore case,
    so names that differ only in that are the same file, spelled as in the first package."""
    out: dict[str, _Planned] = {}
    spelled: dict[str, str] = {}               # lower-case folder -> its spelling

    def spell(path: str) -> str:
        done: list[str] = []
        *dirs, name = path.split("/")
        for d in dirs:
            key = "/".join([*done, d]).lower()
            done.append(spelled.setdefault(key, d))
        return "/".join([*done, name])
    for r in readers:
        files = r.files()
        have = {f.path.lower() for f in files}
        mine: set[str] = set()
        todo = [(f.path, f, 0, f.size) for f in files]
        todo += [(f"sce_sys/{n}", None, eid, 0) for n, eid in r.system_files() if f"sce_sys/{n}".lower() not in have]
        for path, f, eid, size in todo:
            key = path.lower()
            if key in mine:         # Windows and Samba ignore case: one would overwrite the other
                raise Ps4PkgError(f"two files in the package have the same name: {path}")
            mine.add(key)
            out[key] = _Planned(out[key].path if key in out else spell(path), r, f, eid, size)
    for key in out:
        parent = key
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in out:
                raise Ps4PkgError(f"{out[parent].path} is a file in one package and a folder in the other")
    return list(out.values())


def extract(pkgs: "Path | list[Path]", folder: Path, progress=lambda done, total, what: None, cancel=None,
            passcode: str = DEFAULT_PASSCODE) -> tuple[int, int]:
    """Extract every game file and system file into ``folder`` (new or empty). Several packages (the game, then its
    update) are merged: see layers(). Data is signature-checked as it's read, and every written file is read back and
    compared. Returns (files, bytes)."""
    def stop():
        if cancel is not None and cancel.is_set():
            from .hashing import Cancelled
            raise Cancelled()
    readers: list[PkgReader] = []
    try:
        for p in pkgs if isinstance(pkgs, (list, tuple)) else [pkgs]:
            readers.append(PkgReader(p, passcode))
        plan = layers(readers)
        size = sum(x.size for x in plan)
        total, done = 2 * size, 0          # written once, read back once
        sums: dict[str, bytes] = {}
        for x in plan:
            stop()
            out = folder / x.path
            out.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            with open(out, "xb") as fh:
                for chunk in x.reader.chunks(x.file) if x.file is not None else [x.reader.entry(x.eid)]:
                    stop()
                    fh.write(chunk)
                    h.update(chunk)
                    if x.file is not None:
                        done += len(chunk)
                    progress(done, total, "Extracting")
            sums[x.path] = h.digest()
        for name, want in sums.items():
            h = hashlib.sha256()
            with open(folder / name, "rb") as fh:
                while chunk := fh.read(4 << 20):
                    stop()
                    h.update(chunk)
                    done += len(chunk)
                    progress(min(done, total), total, "Checking what was written")
            if h.digest() != want:
                raise Ps4PkgError(f"{name} reads back differently from what was written (disk or network problem)")
        return len(sums), size
    finally:
        for r in readers:
            r.close()
