"""Is a console package intact? Reads all of it and compares it with the hashes and signatures it carries. Nothing is
written or extracted.

  PS4 PKG          digests in the header, and the signature of every block of game data (ps4pkg.verify)
  Switch NSP / XCI every content file (NCA) is named after its own SHA-256 - no keys needed
  Wii U .wua       the archive's own SHA-256 over everything in it
  Switch NSZ / XCZ the compressed ones are checked by nsz, which unpacks them in memory (needs prod.keys)

This proves that nothing changed since the package was made, not that a game runs.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

INTACT, DAMAGED, UNKNOWN = "intact", "damaged", "unknown"
CHUNK = 8 << 20


@dataclass
class Integrity:
    state: str
    note: str
    when: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d) -> "Integrity | None":
        try:
            return Integrity(**d) if d else None
        except TypeError:
            return None

    @property
    def date(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.when))


def check(path: Path, keys: Path | None = None, progress: Callable[[float, str], None] = lambda f, what: None,
          cancel=None) -> Integrity:
    path = Path(path)
    last = [0.0]

    def prog(done, total, what="Reading the package"):
        now = time.monotonic()
        if now - last[0] > 0.3:
            last[0] = now
            progress(done / max(total, 1), f"{what} - {done / 1e9:.2f} of {total / 1e9:.2f} GB")
    from .packages import package_ext
    ext = package_ext(path)
    if ext == ".pkg":
        from . import ps4pkg
        state, note = ps4pkg.verify(path, prog, cancel)
    elif ext == ".wua":
        from . import wiiu
        state, note = wiiu.verify(path, prog, cancel)
    else:
        state, note = _switch(path, keys, prog, cancel, progress)
    return Integrity(state, note, time.time())


def _switch(path: Path, keys: Path | None, prog, cancel, progress) -> tuple[str, str]:
    from . import packages as pk
    from .hashing import Cancelled
    try:
        c = pk._Container(path)
    except (pk.PackageError, OSError, ValueError, IndexError) as exc:
        return DAMAGED, f"its file list can't be read: {exc}"
    try:
        size = path.stat().st_size
        for name, (off, n) in c.files.items():
            if off + n > size:
                return DAMAGED, f"the package is incomplete: {name} would end {(off + n - size) / 1e6:,.1f} MB after " \
                                f"the end of the file"
        ncas = [n for n in c.files if n.lower().endswith(".nca") and re.fullmatch(r"[0-9a-f]{32}(\.cnmt)?",
                                                                                  n.lower()[:-4])]
        packed = [n for n in c.files if n.lower().endswith(".ncz")]
        odd = [n for n in c.files if n.lower().endswith(".nca") and n not in ncas]
        total, done, bad, card = sum(c.files[n][1] for n in ncas), 0, [], []
        k = pk._Keys(keys, None)
        for n in ncas:
            h, pos, want = hashlib.sha256(), 0, c.files[n][1]
            as_card = _game_card_header(c, n, k)        # the same file as it was on a game card
            h2 = hashlib.sha256() if as_card else None
            while pos < want:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                d = c.read(n, pos, min(CHUNK, want - pos))
                if not d:
                    return DAMAGED, f"the package ends in the middle of {n}"
                h.update(d)
                if h2 is not None:
                    h2.update(as_card + d[len(as_card):] if pos == 0 else d)
                pos += len(d)
                done += len(d)
                prog(done, total + (1 if packed else 0))
            if h.hexdigest()[:32] != n[:32].lower():
                (card if h2 is not None and h2.hexdigest()[:32] == n[:32].lower() else bad).append(n)
    finally:
        c.close()
    rewritten = 0
    if bad:
        if not k.get("header_key"):
            if len(bad) == len(ncas):
                return UNKNOWN, "every content file differs from the hash it's named after: rewritten by a converter, not " \
                                "damage in one place (your prod.keys are needed to check what's inside)"
            return DAMAGED, _named(bad, ncas) + ": damaged, or rewritten by a converter (with your prod.keys that can be " \
                                                "told apart)"
        # the hash in the name is gone for good when a converter rewrote the header (title key removed, decrypted …):
        # what's inside still has its own hash trees
        left = []
        for n in bad:
            state, why = _inside(path, n, k, prog, cancel)
            if state == DAMAGED:
                return DAMAGED, f"{n}: {why}"
            if state == UNKNOWN:
                left.append((n, why))
            else:
                rewritten += 1
        if left:
            return UNKNOWN, f"{_named(bad, ncas)}: rewritten by a converter. What can be checked inside is intact, but " \
                            f"{left[0][1]}"
    missing = _missing(path, keys)
    if missing:
        return DAMAGED, f"incomplete: {len(missing)} content file(s) its own list names aren't in it ({missing[0]}…)"
    if packed:
        state, note = _nsz(path, keys, packed, progress, cancel)
        if state != INTACT:
            return state, note
    if not ncas and not packed:
        return UNKNOWN, "no content files with a hash in it"
    if odd:
        return UNKNOWN, f"{len(odd)} content file(s) aren't named after their hash, so they can't be checked"
    return INTACT, f"all {len(ncas) + len(packed)} content files match their SHA-256" + (
        " (a game-card dump turned into an NSP: one header byte differs, the data doesn't)" if card else
        f" ({rewritten} of them rewritten by a converter: checked with the hash trees inside instead)" if rewritten else "")


def _named(bad: list[str], ncas: list[str]) -> str:
    return f"{len(bad)} of {len(ncas)} content files don't match the hash they're named after ({', '.join(bad[:2])}" + (
        " …)" if len(bad) > 2 else ")")


def _inside(path: Path, name: str, keys, prog, cancel) -> tuple[str, str]:
    """Check a content file (NCA) with the hashes inside it: its header's hashes of the section headers, and each
    section's hash tree down to every block of data."""
    import struct
    from . import packages as pk
    from .hashing import Cancelled

    def digest(b: bytes) -> bytes:
        return hashlib.sha256(b).digest()
    try:
        c = pk._Container(path)
    except (pk.PackageError, OSError) as exc:
        return DAMAGED, str(exc)
    skipped = ""
    try:
        try:
            nca = pk._Nca(c, name, keys)
        except pk.PackageError as exc:
            return UNKNOWN, f"its header can't be read: {exc}"
        size = c.files[name][1]
        if struct.unpack_from("<Q", nca.hd, 0x208)[0] != size:
            return DAMAGED, "its size isn't the one in its header"
        total = sum(nca.section(i)[1] - nca.section(i)[0] for i in range(4) if any(nca.hd[0x280 + 32 * i:0x2A0 + 32 * i]))
        base = 0
        for i in range(4):
            want = nca.hd[0x280 + 32 * i:0x2A0 + 32 * i]
            if not any(want):
                continue
            start, end, fsh = nca.section(i)
            if digest(fsh) != want:
                return DAMAGED, f"the header of section {i} doesn't match its hash"
            if any(fsh[0x100:0x140]) or fsh[4] not in (1, 3):
                skipped = "its update data only makes sense together with the game's, so that part can't be checked"
                base += end - start
                continue
            # levels: (offset, size, block size), each hashed by the one before, the first by the master hash
            if fsh[3] == 2:
                block, count = struct.unpack_from("<II", fsh, 0x28)
                regions = [struct.unpack_from("<QQ", fsh, 0x30 + 16 * n) for n in range(min(count, 5))]
                master, pad = fsh[8:0x28], False
                levels = [(regions[0][0], regions[0][1], regions[0][1] or 1)] + [(o, n, block) for o, n in regions[1:]]
            elif fsh[3] == 3 and fsh[8:12] == b"IVFC":
                levels = []
                for n in range(6):
                    o, sz, lg = struct.unpack_from("<QQI", fsh, 0x18 + 0x18 * n)
                    levels.append((o, sz, 1 << min(lg, 24)))
                master, pad = fsh[0xC8:0xE8], True
            else:
                skipped = "one of its sections uses a kind of hash this can't check"
                base += end - start
                continue
            if any(o + sz > end - start or bs <= 0 for o, sz, bs in levels):
                return DAMAGED, f"section {i}'s hash tree is outside the section"
            hashes = master
            for depth, (off, sz, bs) in enumerate(levels):
                last = depth == len(levels) - 1
                if not last and sz > 1 << 30:
                    return UNKNOWN, "one of its hash tables is too large"
                kept, pos, at, wrong = [], 0, 0, []
                step = max(bs, CHUNK - CHUNK % bs)
                while pos < sz:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    d = nca.read_section(i, off + pos, min(step, sz - pos))
                    if not d:
                        return DAMAGED, f"section {i} ends too early"
                    for q in range(0, len(d), bs):
                        part = d[q:q + bs]
                        if pad and len(part) < bs:
                            part = part.ljust(bs, b"\0")
                        if digest(part) != hashes[at:at + 32]:
                            if not last:
                                return DAMAGED, f"a hash table in it is damaged (section {i})"
                            wrong.append((pos + q) // bs)
                        at += 32
                    if not last:
                        kept.append(d)
                    pos += len(d)
                    if last:
                        prog(base + pos, max(total, 1), "Checking the hash trees inside")
                if wrong:
                    names = _files_at(nca, i, off, wrong, bs) if pad else []
                    return DAMAGED, f"{len(wrong):,} of {(sz + bs - 1) // bs:,} blocks of its data don't match their hashes" + (
                        f", in {len(names):,} files ({', '.join(names[:3])}{' …' if len(names) > 3 else ''})" if names else "")\
                        + (": a built-in mod, or damage" if fsh[4] == 1 or len(wrong) > 50 else ": damaged")
                hashes = b"".join(kept)
            base += end - start
    except (struct.error, ValueError, IndexError) as exc:
        return DAMAGED, f"its structure can't be read: {exc}"
    finally:
        c.close()
    return (UNKNOWN, skipped) if skipped else (INTACT, "")


def _game_card_header(c, name: str, keys) -> bytes | None:
    """The start of a content file with the "distribution" byte a game card has. Converters from game-card dumps to NSP
    change it, and with it the file's hash - nothing else. None if it can't be read (no keys) or already says so."""
    from . import packages as pk
    if not keys.get("header_key") or c.files[name][1] < 0xC00:
        return None
    Cipher, algorithms, modes = pk._aes()
    raw = c.read(name, 0, 0x400)
    xts = Cipher(algorithms.AES(keys.get("header_key")), modes.XTS((1).to_bytes(16, "big")))
    d = xts.decryptor()
    hd = bytearray(d.update(raw[0x200:0x400]) + d.finalize())
    if hd[0:4] not in (b"NCA3", b"NCA2") or hd[4] != 0:
        return None
    hd[4] = 1
    e = xts.encryptor()
    return raw[:0x200] + e.update(bytes(hd)) + e.finalize()


def _missing(path: Path, keys: Path | None) -> list[str]:
    """Content files listed in the package's own content meta that it doesn't hold (not delta fragments)."""
    from . import packages as pk
    k = pk._Keys(keys, None)
    if not k.get("header_key"):
        return []
    out: list[str] = []
    try:
        c = pk._Container(path)
    except (pk.PackageError, OSError):
        return []
    try:
        have = {n.lower().split(".")[0] for n in c.files}
        for n in [n for n in c.files if n.endswith(".cnmt.nca")]:
            try:
                for blob in pk._Nca(c, n, k).pfs0_files().values():
                    _t, _v, _typ, recs = pk._cnmt_records(blob)
                    out += [i for i, kind in recs if kind != 6 and i not in have]      # 6 = delta fragment (optional)
            except (pk.PackageError, ValueError, IndexError, KeyError, OSError):
                continue
    finally:
        c.close()
    return out


def _nsz(path: Path, keys: Path | None, packed: list[str], progress, cancel) -> tuple[str, str]:
    import threading
    from .compat import find_tool
    from .tasks import TaskContext, _run_tool
    exe = find_tool("nsz")
    if not exe:
        return UNKNOWN, "nsz not found, needed to check compressed content"
    if not keys or not Path(keys).is_file():
        return UNKNOWN, "compressed content needs your prod.keys to be checked (Settings)"
    lines: list[str] = []
    progress(0.5, "Unpacking the compressed content in memory and checking it (nsz)")
    ctx = TaskContext(lines.append, lambda f, what="": None, cancel or threading.Event(), lambda p: False)
    try:
        _run_tool([exe, "-V", "--keys", str(keys), str(path)], ctx, "Verifying", span=None)
    except RuntimeError as exc:
        text = "\n".join(lines[-40:]).lower()
        if "mismatch" in text or "corrupt" in text or "mismatch" in str(exc).lower():
            return DAMAGED, "its compressed content doesn't unpack to the data its hash names (checked with nsz)"
        return UNKNOWN, f"nsz couldn't check its compressed content: {exc}"
    text = "\n".join(lines)
    left = [n for n in packed if not re.search(r"\[VERIFIED\]\s+" + re.escape(n), text)]
    if re.search(r"\[(CORRUPTED|MISMATCH|BAD)", text):
        return DAMAGED, "its compressed content doesn't match its hash (checked with nsz)"
    if left:
        return UNKNOWN, f"nsz didn't confirm {left[0]}"
    return INTACT, ""


def _files_at(nca, section: int, romfs: int, blocks: list[int], size: int) -> list[str]:
    """Names of the RomFS files these blocks belong to."""
    import bisect
    import struct
    try:
        h = struct.unpack("<10Q", nca.read_section(section, romfs, 0x50))
        if h[8] > 256 << 20:
            return []
        meta, p, out = nca.read_section(section, romfs + h[7], h[8]), 0, []
        while p + 0x20 <= len(meta):
            _par, _sib, off, n, _h, nl = struct.unpack("<IIQQII", meta[p:p + 0x20])
            first, last = (h[9] + off) // size, (h[9] + off + max(n, 1) - 1) // size
            k = bisect.bisect_left(blocks, first)
            if n and k < len(blocks) and blocks[k] <= last:
                out.append(meta[p + 0x20:p + 0x20 + nl].decode("utf-8", "replace"))
            p += 0x20 + ((nl + 3) & ~3)
        return out
    except (struct.error, ValueError, IndexError):
        return []
