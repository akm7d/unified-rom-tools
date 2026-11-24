"""ROM patching: IPS (and EBP), BPS, UPS, PPF 1/2/3 and xdelta (VCDIFF).

The original ROM is never modified; every function writes a new output file.
BPS and UPS carry CRC32s of the expected original and of the result, so they are checked; for IPS, PPF and
xdelta the original is identified against your DATs instead (patches are made for one exact dump).
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import textfile
from .compat import find_tool

PATCH_EXTS = (".ips", ".ebp", ".bps", ".ups", ".ppf", ".xdelta", ".xdelta3", ".vcdiff", ".delta")


class PatchError(Exception):
    pass


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        from .hashing import Cancelled
        raise Cancelled("stopped")


def crc32(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def file_crc32(path: Path) -> str:
    c = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(8 << 20):
            c = zlib.crc32(chunk, c)
    return f"{c & 0xFFFFFFFF:08x}"


# ---------------------------------------------------------------- inspection

@dataclass
class PatchInfo:
    path: Path
    format: str                      # IPS | BPS | UPS | PPF1 | PPF2 | PPF3 | xdelta
    source_size: int | None = None
    target_size: int | None = None
    source_crc: str | None = None    # BPS / UPS
    target_crc: str | None = None    # BPS / UPS
    description: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def checks_source(self) -> bool:
        return self.source_crc is not None


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    """beat-style varint used by BPS and UPS."""
    value, shift = 0, 1
    while True:
        if pos >= len(data):
            raise PatchError("truncated patch")
        x = data[pos]
        pos += 1
        value += (x & 0x7F) * shift
        if x & 0x80:
            return value, pos
        shift <<= 7
        value += shift


def inspect(path: Path) -> PatchInfo:
    with open(path, "rb") as fh:
        head = fh.read(64)
    if head.startswith(b"PATCH"):
        info = PatchInfo(path, "IPS")
        if path.suffix.lower() == ".ebp":
            info.notes.append("EBP (IPS with metadata)")
        return info
    if head.startswith((b"BPS1", b"UPS1")):
        data = path.read_bytes()
        if len(data) < 16:
            raise PatchError("patch file too small")
        pos = 4
        src, pos = _varint(data, pos)
        dst, pos = _varint(data, pos)
        fmt = data[:3].decode()
        info = PatchInfo(path, fmt, src, dst,
                         f"{struct.unpack('<I', data[-12:-8])[0]:08x}", f"{struct.unpack('<I', data[-8:-4])[0]:08x}")
        if fmt == "BPS":
            msize, pos = _varint(data, pos)
            info.description = data[pos:pos + msize].decode("utf-8", "replace").strip()
        if crc32(data[:-4]) != f"{struct.unpack('<I', data[-4:])[0]:08x}":
            raise PatchError("the patch file itself is damaged (its own checksum doesn't match)")
        return info
    if head.startswith((b"PPF10", b"PPF20", b"PPF30")):
        info = PatchInfo(path, "PPF" + chr(head[3]))
        info.description = head[6:56].decode("latin-1", "replace").strip(" \x00")
        if head[3:4] == b"2":
            info.source_size = struct.unpack("<I", head[56:60])[0]
        return info
    if head[:3] == b"\xd6\xc3\xc4":
        return PatchInfo(path, "xdelta")
    raise PatchError("unknown patch format (supported: IPS, EBP, BPS, UPS, PPF, xdelta)")


# ---------------------------------------------------------------- appliers (bytes in, bytes out)

def _ips_ends(patch: bytes, pos: int) -> bool:
    """Is the "EOF" at ``pos`` the end of the patch? The same three bytes are also the offset of a change at 0x454F46 - then
    valid records follow, up to the real end."""
    def end_like(p: int) -> bool:           # nothing more, the optional size to cut to, or EBP's description
        return len(patch) - p - 3 in (0, 3) or patch[p + 3:p + 4] == b"{"
    if end_like(pos):
        return True
    p = pos
    while p + 5 <= len(patch):              # read on as if it were a record
        size = int.from_bytes(patch[p + 3:p + 5], "big")
        p += 5 + (size or 3)
        if p + 3 <= len(patch) and patch[p:p + 3] == b"EOF" and end_like(p):
            return False
    return True


def apply_ips(src: bytes, patch: bytes) -> bytes:
    if not patch.startswith(b"PATCH"):
        raise PatchError("not an IPS patch")
    out = bytearray(src)
    pos = 5
    while True:
        if pos + 3 > len(patch):
            raise PatchError("IPS patch ends early")
        if patch[pos:pos + 3] == b"EOF" and _ips_ends(patch, pos):
            pos += 3
            break
        off = int.from_bytes(patch[pos:pos + 3], "big")
        size = int.from_bytes(patch[pos + 3:pos + 5], "big")
        pos += 5
        if size == 0:                                     # run-length record
            count = int.from_bytes(patch[pos:pos + 2], "big")
            chunk = patch[pos + 2:pos + 3] * count
            pos += 3
        else:
            chunk = patch[pos:pos + size]
            pos += size
        if pos > len(patch):
            raise PatchError("IPS patch ends early")
        end = off + len(chunk)
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[off:end] = chunk
    if len(patch) - pos == 3:                             # optional truncation size
        out = out[:int.from_bytes(patch[pos:pos + 3], "big")]
    return bytes(out)


def apply_bps(src: bytes, patch: bytes, cancel=None) -> bytes:
    if not patch.startswith(b"BPS1"):
        raise PatchError("not a BPS patch")
    pos = 4
    src_size, pos = _varint(patch, pos)
    dst_size, pos = _varint(patch, pos)
    msize, pos = _varint(patch, pos)
    pos += msize
    if len(src) != src_size:
        raise PatchError(f"the original is {len(src):,} bytes, the patch expects {src_size:,}")
    out = bytearray(dst_size)
    o = src_rel = dst_rel = 0
    end = len(patch) - 12
    n = 0
    while pos < end:
        n += 1
        if not n & 0x3FF:
            _check(cancel)
        data, pos = _varint(patch, pos)
        cmd, length = data & 3, (data >> 2) + 1
        if cmd == 0:                                      # SourceRead
            out[o:o + length] = src[o:o + length]
            o += length
        elif cmd == 1:                                    # TargetRead
            out[o:o + length] = patch[pos:pos + length]
            pos += length
            o += length
        else:
            d, pos = _varint(patch, pos)
            delta = -(d >> 1) if d & 1 else d >> 1
            if cmd == 2:                                  # SourceCopy
                src_rel += delta
                out[o:o + length] = src[src_rel:src_rel + length]
                src_rel += length
                o += length
            else:                                         # TargetCopy (may overlap: byte by byte)
                dst_rel += delta
                for k in range(length):
                    if not k & 0xFFFFF:
                        _check(cancel)
                    out[o] = out[dst_rel]
                    o += 1
                    dst_rel += 1
    return bytes(out)


def apply_ups(src: bytes, patch: bytes) -> bytes:
    if not patch.startswith(b"UPS1"):
        raise PatchError("not a UPS patch")
    pos = 4
    a_size, pos = _varint(patch, pos)
    b_size, pos = _varint(patch, pos)
    want_src = f"{struct.unpack('<I', patch[-12:-8])[0]:08x}"
    want_dst = f"{struct.unpack('<I', patch[-8:-4])[0]:08x}"
    have = crc32(src)
    if have == want_dst and have != want_src:
        raise PatchError("this ROM already has the patch applied")
    out = bytearray(src[:b_size] if len(src) >= b_size else src + b"\x00" * (b_size - len(src)))
    o = 0
    end = len(patch) - 12
    while pos < end:
        skip, pos = _varint(patch, pos)
        o += skip
        while pos < end:
            x = patch[pos]
            pos += 1
            if x == 0:
                o += 1
                break
            if o < len(out):
                out[o] ^= x
            o += 1
    return bytes(out)


def _ppf_records_end(data: bytes) -> int:
    i = data.find(b"@BEGIN_FILE_ID.DIZ")
    return i if i >= 0 else len(data)


def _copy(src: Path, dst: Path, cancel=None) -> None:
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while chunk := fi.read(8 << 20):
            _check(cancel)
            fo.write(chunk)


def apply_ppf(src_path: Path, patch_path: Path, out_path: Path, ignore_check: bool = False, cancel=None) -> list[str]:
    """PPF patches disc images (often hundreds of MB): copy the original, then write the changes in place."""
    data = patch_path.read_bytes()
    ver = data[3:4]
    notes = []
    if ver == b"1":
        pos, wide = 56, False
    elif ver == b"2":
        expect = struct.unpack("<I", data[56:60])[0]
        if src_path.stat().st_size != expect and not ignore_check:
            raise PatchError(f"the original is {src_path.stat().st_size:,} bytes, the patch expects {expect:,}")
        block = data[60:1084]
        _ppf_blockcheck(src_path, 0x9320, block, ignore_check)
        pos, wide = 1084, False
    elif ver == b"3":
        imagetype, blockcheck, undo = data[56], data[57], data[58]
        pos = 60
        if blockcheck:
            _ppf_blockcheck(src_path, 0x80A0 if imagetype == 1 else 0x9320, data[60:1084], ignore_check)
            pos = 1084
        wide = True
        if undo:
            notes.append("patch contains undo data")
    else:
        raise PatchError("unknown PPF version")
    end = _ppf_records_end(data)
    _copy(src_path, out_path, cancel)
    with open(out_path, "r+b") as fh:
        while pos < end:
            if wide:
                off = struct.unpack("<Q", data[pos:pos + 8])[0]
                pos += 8
            else:
                off = struct.unpack("<I", data[pos:pos + 4])[0]
                pos += 4
            n = data[pos]
            pos += 1
            fh.seek(off)
            fh.write(data[pos:pos + n])
            pos += n
            if ver == b"3" and data[58]:
                pos += n                                   # skip undo bytes
    return notes


def _ppf_blockcheck(src_path: Path, offset: int, block: bytes, ignore: bool) -> None:
    with open(src_path, "rb") as fh:
        fh.seek(offset)
        have = fh.read(1024)
    if have != block and not ignore:
        raise PatchError("this disc image isn't the one the PPF patch was made for (block check failed)")


def apply_xdelta(src_path: Path, patch_path: Path, out_path: Path, cancel=None) -> None:
    from .chdman_tool import Process
    exe = find_tool("xdelta3")
    if not exe:
        raise PatchError("xdelta3 not found (it's bundled in the packaged builds; on Linux: pacman -S xdelta3)")
    lines: list[str] = []
    rc = Process([exe, "-d", "-f", "-s", str(src_path), str(patch_path), str(out_path)], lines.append).run_until(cancel)
    _check(cancel)
    if rc != 0:
        said = [ln.strip() for ln in lines[1:] if ln.strip()]
        msg = said[-1] if said else f"exit code {rc}"
        if "checksum" in msg.lower() or "source" in msg.lower():
            msg += " — this usually means the original isn't the version the patch was made for"
        raise PatchError(f"xdelta3: {msg}")


# ---------------------------------------------------------------- headers

def copier_header_len(name: str, data: bytes) -> int:
    from .hashing import detect_header
    return detect_header(name, data[:16], len(data))


# ---------------------------------------------------------------- one-shot apply for a plain file

@dataclass
class ApplyResult:
    output: Path
    notes: list[str]
    target_crc: str
    verified: bool | None            # True: matches the patch's target CRC; None: format has none


def apply_file(src: Path, patches: list[Path], out: Path, header: str = "auto", cancel=None) -> ApplyResult:
    """Apply one or more patches in order to a plain ROM/disc-image file, writing `out`.
    header: auto | keep | strip  (copier headers on SNES/NES/Lynx/7800 ROMs)."""
    notes: list[str] = []
    work = src
    tmp_files: list[Path] = []
    verified = None
    try:
        for i, p in enumerate(patches):
            _check(cancel)
            info = inspect(p)
            dest = out if i == len(patches) - 1 else out.with_name(f".romtools-patch-{i}{out.suffix}")
            if dest != out:
                tmp_files.append(dest)
            if info.format in ("IPS", "BPS", "UPS"):
                data = work.read_bytes()
                hlen = copier_header_len(src.name, data)
                variants = [("as is", data)]
                if hlen:
                    stripped = ("without its copier header", data[hlen:])
                    variants = [stripped, variants[0]] if header == "strip" else \
                        [variants[0]] if header == "keep" else [variants[0], stripped]
                if info.checks_source:
                    match = next(((lbl, d) for lbl, d in variants if crc32(d) == info.source_crc), None)
                    if match is None:
                        if crc32(data) == info.target_crc:
                            raise PatchError(f"{p.name}: this ROM already has the patch applied")
                        raise PatchError(f"{p.name}: this isn't the ROM the patch was made for (CRC32 {crc32(data)}, "
                                         f"patch expects {info.source_crc})")
                    label, data = match
                    if label != "as is":
                        notes.append(f"{p.name}: applied to the ROM {label}, as the patch expects")
                else:
                    label, data = variants[0]
                    if hlen and header == "strip":
                        notes.append(f"{p.name}: copier header removed first")
                fn = {"IPS": apply_ips, "UPS": apply_ups,
                      "BPS": lambda a, b: apply_bps(a, b, cancel)}[info.format]
                result = fn(data, p.read_bytes())
                _check(cancel)
                dest.write_bytes(result)
                if info.target_crc:
                    verified = crc32(result) == info.target_crc
                    if not verified:
                        raise PatchError(f"{p.name}: the result doesn't match the patch's expected CRC32")
            elif info.format.startswith("PPF"):
                notes += apply_ppf(work, p, dest, cancel=cancel)
            elif info.format == "xdelta":
                apply_xdelta(work, p, dest, cancel)
            work = dest
        return ApplyResult(out, notes, file_crc32(out), verified)
    except Exception:
        if out.exists():
            out.unlink()
        raise
    finally:
        for t in tmp_files:
            t.unlink(missing_ok=True)


# ---------------------------------------------------------------- creating patches (BPS)

def create_bps(original: bytes, modified: bytes, metadata: str = "") -> bytes:
    """A simple, valid BPS (SourceRead where bytes are unchanged, TargetRead elsewhere)."""
    def enc(n: int) -> bytes:
        out = bytearray()
        while True:
            x = n & 0x7F
            n >>= 7
            if n == 0:
                out.append(0x80 | x)
                return bytes(out)
            out.append(x)
            n -= 1

    body = bytearray(b"BPS1") + enc(len(original)) + enc(len(modified))
    meta = metadata.encode()
    body += enc(len(meta)) + meta
    i, n = 0, len(modified)
    while i < n:
        same = i < len(original) and original[i] == modified[i]
        j = i
        while j < n and ((j < len(original) and original[j] == modified[j]) == same):
            j += 1
        length = j - i
        body += enc(((length - 1) << 2) | (0 if same else 1))
        if not same:
            body += modified[i:j]
        i = j
    body += struct.pack("<I", zlib.crc32(original) & 0xFFFFFFFF)
    body += struct.pack("<I", zlib.crc32(modified) & 0xFFFFFFFF)
    body += struct.pack("<I", zlib.crc32(bytes(body)) & 0xFFFFFFFF)
    return bytes(body)


# ---------------------------------------------------------------- whole-ROM pipeline (archives, .cue sets, CHDs)

@dataclass
class BaseInfo:
    """What the user picked, and what the patch will actually be applied to."""
    path: Path
    kind: str                         # file | archive | cueset | chd
    target_name: str = ""             # file the patch applies to (archive member / data track / disc image)
    size: int = 0
    crc: str = ""
    sha1: str = ""
    dat_game: str = ""                # identified against all DATs ("" if unknown)
    dat_name: str = ""
    chd_kind: str = ""
    notes: list[str] = field(default_factory=list)


def output_suffix(src: Path) -> str:
    """Extension of the patched result: CHDs stay CHDs, .cue sets stay .cue, archives become the ROM inside."""
    return src.suffix.lower() if src.suffix.lower() in (".chd", ".cue") else src.suffix


def default_output_name(src: Path, patches: list[Path]) -> str:
    tag = patches[0].stem if patches else "patched"
    stem = src.stem
    for prefix in (stem, stem.split(" (")[0]):          # "Game (USA) - Fix" / "Game [T-En]" -> "Fix" / "T-En"
        if prefix and tag.lower().startswith(prefix.lower()):
            tag = tag[len(prefix):].strip(" -_.") or tag
            break
    if tag.startswith("[") and tag.endswith("]") and tag.count("[") == 1:
        tag = tag[1:-1]
    return f"{stem} [{tag}]"


def _identify(path: Path, cancel=None):
    """(game, dat) for a file, looked up across all DATs (with and without a copier header)."""
    from .config import get_config
    from .hashing import hash_file
    from .library import all_dats
    dats = all_dats(get_config())
    if not dats:
        return "", ""
    idx = _index_cache().get(dats)
    e = hash_file(path, cancel)
    for v in (e.full, e.headerless):
        if v is not None:
            hits = idx.lookup(v.size, v.crc, v.md5, v.sha1)
            if hits:
                return hits[0][0].name, hits[0][0].dat
    return "", ""


_identify.cache = None  # set lazily (IndexCache import is GUI-free but costs time)


def _index_cache():
    if _identify.cache is None:
        from .datmanager import IndexCache
        _identify.cache = IndexCache()
    return _identify.cache


def _hash_path(path: Path, cancel=None) -> tuple[int, str, str]:
    import hashlib
    h, c, n = hashlib.sha1(), 0, 0
    with open(path, "rb") as fh:
        while chunk := fh.read(8 << 20):
            _check(cancel)
            h.update(chunk)
            c = zlib.crc32(chunk, c)
            n += len(chunk)
    return n, f"{c & 0xFFFFFFFF:08x}", h.hexdigest()


def _extract_archive_member(src: Path, work: Path) -> Path:
    from .hashing import SEVENZIP, _7z_list, member_command
    import zipfile
    if src.suffix.lower() == ".zip" and zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            if len(members) != 1:
                raise PatchError(f"{src.name} holds {len(members)} files; patch a single-game archive, or extract "
                                 f"the ROM first")
            out = work / Path(members[0].filename).name
            with zf.open(members[0]) as fi, open(out, "wb") as fo:
                shutil.copyfileobj(fi, fo, 1 << 20)
            return out
    if not SEVENZIP:
        raise PatchError("7-Zip is needed to read this archive")
    listed = _7z_list(src)
    names, sizes = [n for n, _s in listed], [n for _n, n in listed]
    if len(names) != 1:
        raise PatchError(f"{src.name} holds {len(names)} files; patch a single-game archive")
    out = work / (Path(names[0].replace("\\", "/")).name or "rom")
    with open(out, "wb") as fo:
        r = subprocess.run(member_command(src, names[0]), stdout=fo, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    if r.returncode != 0 or out.stat().st_size != sizes[0]:
        raise PatchError(f"couldn't extract {names[0]} from {src.name}")
    return out


def _cue_files(cue: Path) -> list[str]:
    return re.findall(r'^\s*FILE\s+"?(.+?)"?\s+\S+\s*$', textfile.read_text(cue), re.I | re.M)


def _pick_track(tracks: list[Path], patches: list[Path]) -> Path:
    """The track a disc patch applies to: by the size/CRC a BPS/UPS patch expects, else the first data track."""
    info = inspect(patches[0])
    if info.source_size:
        for t in tracks:
            if t.stat().st_size == info.source_size and (not info.source_crc or file_crc32(t) == info.source_crc):
                return t
    return tracks[0]


def prepare(src: Path, patches: list[Path], work: Path, progress=lambda s, f=0.0: None,
            cancel=None) -> tuple[BaseInfo, Path, dict]:
    """Unpack whatever was picked into `work`; return (info, file to patch, context for finishing)."""
    from . import chd
    ext = src.suffix.lower()
    ctx: dict = {}
    if ext == ".chd":
        ci = chd.info(src)
        if ci.kind not in ("cd", "dvd"):
            raise PatchError(f"{ci.kind.upper()} CHDs can't be patched here (only CD and DVD)")
        progress("Unpacking the CHD", 0.0)
        from .chdman_tool import Process, chdman_bin
        exe = chdman_bin()
        if ci.kind == "cd":
            argv = [exe, "extractcd", "-i", str(src), "-o", str(work / "disc.cue"), "-f"]
            if ci.tracks > 1:
                argv.append("-sb")
        else:
            argv = [exe, "extractdvd", "-i", str(src), "-o", str(work / "disc.iso"), "-f"]
        rc = Process(argv, on_progress=lambda f: progress("Unpacking the CHD", f)).run_until(cancel)
        _check(cancel)
        if rc != 0:
            raise PatchError("chdman couldn't unpack the CHD")
        if ci.kind == "cd":
            tracks = sorted((p for p in work.iterdir() if p.suffix.lower() == ".bin"),
                            key=lambda p: chd._natural(p.name))
            target = _pick_track(tracks, patches)
            ctx.update(cue=work / "disc.cue", tracks=tracks)
        else:
            target = work / "disc.iso"
        ctx["chd_kind"] = ci.kind
        info = BaseInfo(src, "chd", target.name, chd_kind=ci.kind)
    elif ext == ".cue":
        files = [src.parent / f for f in _cue_files(src)]
        missing = [f.name for f in files if not f.exists()]
        if missing:
            raise PatchError(f"the .cue refers to missing files: {', '.join(missing)}")
        target = _pick_track(files, patches)
        ctx.update(cue=src, tracks=files)
        info = BaseInfo(src, "cueset", target.name)
    elif ext in (".zip", ".7z", ".rar"):
        progress("Extracting the ROM from the archive")
        target = _extract_archive_member(src, work)
        info = BaseInfo(src, "archive", target.name)
    else:
        target = src
        info = BaseInfo(src, "file", src.name)
    progress("Identifying the original")
    info.size, info.crc, info.sha1 = _hash_path(target, cancel)
    info.dat_game, info.dat_name = _identify(target, cancel)
    return info, target, ctx


def rename_tracks(cue_text: str, folder: Path, names: dict[Path, str]) -> str:
    """A .cue's text with its tracks renamed - however they're written in it (quoted or not, with a folder …)."""
    def line(m: re.Match) -> str:
        new = names.get(folder / m[2])
        return f'{m[1]}"{new}"{m[3]}' if new else m[0]
    return re.sub(r'^([ \t]*FILE[ \t]+)"?(.+?)"?([ \t]+\S+[ \t]*\r?)$', line, cue_text, flags=re.I | re.M)


def _place(made: Path, out: Path, cancel=None) -> None:
    """Move a finished file to its real name; across drives it's copied beside it first, so a stop leaves nothing."""
    import os
    import uuid
    try:
        os.rename(made, out)
        return
    except OSError:
        pass
    tmp = out.with_name(f".romtools-{uuid.uuid4().hex[:8]}{out.suffix}")
    try:
        _copy(made, tmp, cancel)
        if out.exists():
            raise PatchError(f"{out.name} already exists")
        os.rename(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def finish(info: BaseInfo, patched: Path, ctx: dict, out: Path, work: Path, progress=lambda s, f=0.0: None,
           cancel=None) -> list[str]:
    """Turn the patched file into the final output (plain file, new .cue set, or a new CHD). Returns track SHA-1s."""
    from .chdman_tool import Process, chdman_bin
    if info.kind in ("file", "archive"):
        sha = [_hash_path(patched, cancel)[2]]
        _place(patched, out, cancel)
        return sha
    if info.kind == "cueset":
        tracks: list[Path] = ctx["tracks"]
        sheet = textfile.read(ctx["cue"])
        many = len(tracks) > 1
        sha, written, names = [], [], {}
        try:
            for i, t in enumerate(tracks, 1):
                new_name = f"{out.stem} (Track {i}){t.suffix}" if many else f"{out.stem}{t.suffix}"
                dest = out.parent / new_name
                if dest.exists():
                    raise PatchError(f"{dest.name} already exists")
                written.append(dest)
                _copy(patched if t.name == info.target_name else t, dest, cancel)
                names[t] = new_name
                sha.append(_hash_path(dest, cancel)[2])
            out.write_bytes(sheet.with_text(rename_tracks(sheet.text, ctx["cue"].parent, names)).encode())
        except BaseException:
            for f in written:
                f.unlink(missing_ok=True)
            raise
        return sha
    # CHD: put the patched track back and build a new CHD of the same type (it gets its name once it's verified)
    new = out.with_name(f".romtools-{work.name}.chd")
    try:
        if ctx["chd_kind"] == "cd":
            orig_track = work / info.target_name
            orig_track.unlink()
            shutil.move(str(patched), orig_track)
            sha = [_hash_path(t, cancel)[2] for t in ctx["tracks"]]
            argv = [chdman_bin(), "createcd", "-i", str(ctx["cue"]), "-o", str(new), "-f"]
        else:
            iso = work / "disc.iso"
            iso.unlink()
            shutil.move(str(patched), iso)
            sha = [_hash_path(iso, cancel)[2]]
            argv = [chdman_bin(), "createdvd", "-i", str(iso), "-o", str(new), "-f"]
        rc = Process(argv, on_progress=lambda f: progress("Creating the new CHD", f)).run_until(cancel)
        _check(cancel)
        if rc != 0:
            raise PatchError("chdman couldn't create the new CHD")
        rc = Process([chdman_bin(), "verify", "-i", str(new)],
                     on_progress=lambda f: progress("Verifying the new CHD", f)).run_until(cancel)
        _check(cancel)
        if rc != 0:
            raise PatchError("the new CHD failed chdman verify")
        _place(new, out, cancel)
    finally:
        new.unlink(missing_ok=True)
    return sha


def patch_rom(src: Path, patches: list[Path], out: Path, tmp_root: Path, header: str = "auto", tag: str = "",
              progress=lambda s, f=0.0: None, cancel=None) -> tuple[BaseInfo, ApplyResult]:
    """Full job: unpack, patch, rebuild, tag. Never touches `src`; `out` must not exist yet, and gets its name once it's
    complete. ``cancel`` (an Event) stops it at any point, leaving nothing behind."""
    import tempfile
    if out.exists():
        raise PatchError(f"{out.name} already exists")
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="patch-", dir=tmp_root))
    try:
        info, target, ctx = prepare(src, patches, work, progress, cancel)
        progress("Applying the patch", 0.0)
        patched = work / ("patched" + target.suffix)
        result = apply_file(target, patches, patched, header, cancel)
        out.parent.mkdir(parents=True, exist_ok=True)
        sha = finish(info, patched, ctx, out, work, progress, cancel)
        result.output = out
        if tag:
            from .config import get_config  # noqa: F401  (config dirs)
            from .tags import MarkStore
            MarkStore().set("sha1:" + ",".join(sha[:8]), tag, out.name)
        return info, result
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- auto-matching a folder of patches to library ROMs

@dataclass
class Candidate:
    path: Path
    name: str
    hashes: list[tuple[int, str]]          # (size, crc32) of each data part (ROM, CHD track, archive member)


@dataclass
class PatchMatch:
    patch: Path
    info: PatchInfo | None
    rom: Path | None = None
    how: str = ""                          # "checksum" | "patch names the original" | "name (check)" | ""
    sure: bool = False
    alternatives: list[Path] = field(default_factory=list)
    error: str = ""


def library_candidates(cfg, extra_folders: list[Path] = ()) -> list[Candidate]:
    """Every ROM in the library (and extra folders), with hashes from remembered scans where available."""
    from .cache import HashCache
    from .library import OTHER, split
    from .scanner import _items_in, enumerate_system
    root = Path(cfg["library_root"])
    items = []
    if root.is_dir():
        systems, _o, _l = split(cfg, root)
        for s in systems:
            items += enumerate_system(s, root / s)
        items += _items_in(OTHER, root, skip_dirs=set(systems))
    for f in extra_folders:
        items += _items_in(OTHER, f)
    cache = HashCache()
    out, seen = [], set()
    for it in items:
        if it.kind == "folder" or it.path in seen:
            continue
        seen.add(it.path)
        hashes = []
        try:
            st = it.path.stat()
            mtime = st.st_mtime + sum(x.stat().st_mtime for x in it.parts)
            size = st.st_size + sum(x.stat().st_size for x in it.parts)
            hit = cache.get(it.path, size, mtime)
            if hit:
                for e in hit[2]:
                    hashes.append((e.full.size, e.full.crc))
                    if e.headerless:
                        hashes.append((e.headerless.size, e.headerless.crc))
        except OSError:
            continue
        out.append(Candidate(it.path, it.path.stem, hashes))
    return out


_NOISE = re.compile(r"\[[^\]]*\]|\b(v|rev|ver)\s?\d+(\.\d+)*\b|\b(english|translation|translated|patch|patched|"
                    r"hack|fan|eng|undub|t-?en|redux|fix(ed)?|final|beta|by)\b", re.I)


def _tokens(name: str) -> set[str]:
    name = _NOISE.sub(" ", name.lower())
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return {t for t in name.split() if len(t) > 1 or t.isdigit()}


def _xdelta_source_name(path: Path) -> str:
    """xdelta3 usually stores the original's file name in the VCDIFF application header."""
    data = path.read_bytes()[:4096]
    if data[:3] != b"\xd6\xc3\xc4":
        return ""
    ind, pos = data[4], 5

    def varint(p):
        v = 0
        while True:
            b = data[p]
            p += 1
            v = (v << 7) | (b & 0x7F)
            if not b & 0x80:
                return v, p

    if ind & 0x01:
        pos += 1
    if ind & 0x02:
        n, pos = varint(pos)
        pos += n
    if not ind & 0x04:
        return ""
    n, pos = varint(pos)
    app = data[pos:pos + n].decode("utf-8", "replace").strip("\x00")
    parts = app.split("/")
    # xdelta3's application header is "<target name>/<kind>/<source name>/<kind>"
    if len(parts) >= 3 and parts[2].strip():
        return Path(parts[2].replace("\\", "/")).name
    return ""


def match_patches(patches: list[Path], candidates: list[Candidate]) -> list[PatchMatch]:
    by_hash: dict[tuple[int, str], list[Candidate]] = {}
    for c in candidates:
        for h in c.hashes:
            by_hash.setdefault(h, []).append(c)
    by_name = {c.path.name.lower(): c for c in candidates}
    by_stem = {c.path.stem.lower(): c for c in candidates}
    tok = [(c, _tokens(c.name)) for c in candidates]
    results = []
    for p in patches:
        m = PatchMatch(p, None)
        try:
            m.info = inspect(p)
        except PatchError as exc:
            m.error = str(exc)
            results.append(m)
            continue
        i = m.info
        if i.source_crc and i.source_size is not None:
            hits = by_hash.get((i.source_size, i.source_crc), [])
            if not hits:   # not scanned yet: hash just the files of the right size (with or without a header)
                from .hashing import hash_file
                sizes = {i.source_size, i.source_size + 16, i.source_size + 512}
                for c in candidates:
                    if c.hashes or c.path.suffix.lower() in (".chd", ".zip", ".7z", ".rar", ".cue"):
                        continue
                    try:
                        if c.path.stat().st_size not in sizes:
                            continue
                        e = hash_file(c.path)
                    except OSError:
                        continue
                    if (e.full.size, e.full.crc) == (i.source_size, i.source_crc) or (
                            e.headerless and (e.headerless.size, e.headerless.crc) == (i.source_size, i.source_crc)):
                        hits.append(c)
            if hits:
                m.rom, m.how, m.sure = hits[0].path, "checksum", True
                m.alternatives = [h.path for h in hits[1:]]
                results.append(m)
                continue
        if i.format == "xdelta":
            src_name = _xdelta_source_name(p)
            c = by_name.get(src_name.lower()) or by_stem.get(Path(src_name).stem.lower()) if src_name else None
            if c:
                m.rom, m.how = c.path, "patch names the original"
                results.append(m)
                continue
        want = _tokens(p.stem)
        scored = []
        for c, t in tok:
            if not t or not want:
                continue
            inter = len(want & t)
            score = inter / len(want | t) + 0.5 * inter / len(want)
            if i.source_size and any(s == i.source_size for s, _c in c.hashes):
                score += 0.3
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.6:
            m.rom, m.how = scored[0][1].path, "name (check)"
            m.alternatives = [c.path for s, c in scored[1:6] if s >= 0.4]
        elif i.source_crc:
            m.error = (f"no ROM in your library has the checksum this patch expects ({i.source_crc}); "
                       "scan the library first if you haven't")
        else:
            m.error = "no ROM with a similar name"
        results.append(m)
    return results
