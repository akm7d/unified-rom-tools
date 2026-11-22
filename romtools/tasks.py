"""Python-based tasks for ROM Optimizer (run by converter.Runner like chdman jobs).

Every task is lossless and verified before anything is replaced:
  * new files are written under a temporary name next to the target, checked, then renamed;
  * a source file is only moved to the trash after its replacement was verified (and only if asked);
  * nothing is ever overwritten.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import threading
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .hashing import Cancelled


class TaskSkipped(Exception):
    """Nothing to do for this file (already right / not applicable). Shown as 'Skipped' with the reason."""


@dataclass
class TaskContext:
    log: Callable[[str], None]
    progress: Callable[..., None]            # progress(fraction, phase="")
    cancel: threading.Event
    trash: Callable[[Path], bool]
    trash_sources: bool = False

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


def _tmp_beside(path: Path, suffix: str) -> Path:
    return path.with_name(f".romtools-{uuid.uuid4().hex[:8]}{suffix}")


def _finish_new_file(ctx: TaskContext, tmp: Path, final: Path, source: Path | None) -> str:
    """Rename a verified temp file into place; optionally trash the source. Never overwrites."""
    if final.exists() and final != source:
        raise RuntimeError(f"{final.name} already exists")
    if source is not None and final == source:
        # in-place replacement (same name): the original must go to the trash first
        if not ctx.trash(source):
            raise RuntimeError("couldn't move the original to the trash, so it was left as it was")
        os.rename(tmp, final)
        return "original moved to the trash"
    os.rename(tmp, final)
    if source is not None and ctx.trash_sources:
        return "original moved to the trash" if ctx.trash(source) else "couldn't trash the original"
    return ""


def sha1_file(path: Path, ctx: TaskContext | None = None, span: tuple[float, float] = (0, 1), phase: str = "") -> str:
    h = hashlib.sha1()
    size = max(path.stat().st_size, 1)
    done = 0
    with open(path, "rb", buffering=0) as fh:
        while chunk := fh.read(8 << 20):
            if ctx:
                ctx.check()
            h.update(chunk)
            done += len(chunk)
            if ctx:
                ctx.progress(span[0] + (span[1] - span[0]) * done / size, phase)
    return h.hexdigest()


# ====================================================================== CHD type fix

def fix_chd(path: Path, target: str, ram: bool, ssd_dir: Path) -> Callable[[TaskContext], str]:
    def run(ctx: TaskContext) -> str:
        from .chdfix import fix
        r = fix(path, target, ram, ssd_dir, ctx.cancel, log=ctx.log,
                progress=lambda f, stage: ctx.progress(f, stage))
        if not r.ok:
            raise RuntimeError(r.message)
        return r.message
    return run


# ====================================================================== multi-disc playlists

DISC_EXTS = (".chd", ".cue", ".gdi", ".iso", ".pbp", ".rvz", ".ccd", ".mds")
_DISC = re.compile(r"\s*\((?:Disc|Disk|CD|DVD)\s*(\d+)(?:\s*of\s*\d+)?\)", re.I)


def find_playlists(folder: Path, recursive: bool = True) -> list[tuple[Path, list[Path]]]:
    """[(m3u path, [disc files in order])] for every multi-disc game. Playlist name = game name without (Disc N)."""
    out = []
    folders = [folder] + ([d for d in folder.rglob("*") if d.is_dir() and not d.name.startswith(".")]
                          if recursive else [])
    for d in folders:
        groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
        try:
            files = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in DISC_EXTS]
        except OSError:
            continue
        cue_tracks = set()
        for f in files:
            if f.suffix.lower() in (".cue", ".gdi"):
                from .scanner import _cue_parts, _gdi_parts
                cue_tracks.update(p.name for p in (_cue_parts(f) if f.suffix.lower() == ".cue" else _gdi_parts(f)))
        for f in files:
            if f.name in cue_tracks:
                continue
            m = _DISC.search(f.stem)
            if not m:
                continue
            base = (f.stem[:m.start()] + f.stem[m.end():]).strip()
            base = re.sub(r"\s{2,}", " ", base)
            groups.setdefault((base, f.suffix.lower()), []).append((int(m.group(1)), f))
        for (base, _ext), discs in groups.items():
            if len(discs) < 2:
                continue
            discs.sort()
            out.append((d / f"{base}.m3u", [p for _n, p in discs]))
    return sorted(out, key=lambda t: str(t[0]).lower())


def write_playlist(m3u: Path, discs: list[Path]) -> Callable[[TaskContext], str]:
    def run(ctx: TaskContext) -> str:
        text = "".join(os.path.relpath(d, m3u.parent) + "\n" for d in discs)
        if m3u.exists():
            if m3u.read_text(errors="replace").replace("\r", "") == text:
                raise TaskSkipped("playlist already up to date")
            raise TaskSkipped(f"{m3u.name} already exists with different contents (left as it is)")
        tmp = _tmp_beside(m3u, ".m3u")
        tmp.write_text(text, encoding="utf-8")
        os.rename(tmp, m3u)
        ctx.log(f"wrote {m3u.name}: " + ", ".join(d.name for d in discs))
        return f"{len(discs)} discs"
    return run


# ====================================================================== NDS / 3DS trim and untrim

def _nds_layout(head: bytes) -> tuple[int, int]:
    """(used size from the header, full cartridge size)."""
    used = struct.unpack_from("<I", head, 0x80)[0]
    if head[0x12] & 0x02 and len(head) >= 0x214:          # DSi-enhanced / DSi-only: TWL area is included
        used = max(used, struct.unpack_from("<I", head, 0x210)[0])
    capacity = 0x20000 << head[0x14] if head[0x14] < 16 else 0
    return used, capacity


def _3ds_layout(head: bytes) -> tuple[int, int]:
    if head[0x100:0x104] != b"NCSD":
        raise RuntimeError("not a 3DS cartridge image (no NCSD header)")
    media = 0x200
    image = struct.unpack_from("<I", head, 0x104)[0] * media
    used = 0
    for i in range(8):
        off, size = struct.unpack_from("<II", head, 0x120 + i * 8)
        used = max(used, (off + size) * media)
    return used, image


def _layout(path: Path) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(0x4000)
    if path.suffix.lower() in (".3ds", ".cci"):
        return _3ds_layout(head)
    return _nds_layout(head)


def trim_info(path: Path) -> tuple[int, int, int]:
    """(current size, trimmed size, full size). Raises if the file isn't trimmable losslessly."""
    used, full = _layout(path)
    size = path.stat().st_size
    if not used or used > size:
        raise RuntimeError("header looks wrong (used size beyond the file)")
    return size, used, full


def _tail_is(path: Path, start: int, byte: int) -> bool:
    pat = bytes([byte]) * (1 << 20)
    with open(path, "rb") as fh:
        fh.seek(start)
        while chunk := fh.read(1 << 20):
            if chunk != pat[:len(chunk)]:
                return False
    return True


def trim(path: Path, out: Path) -> Callable[[TaskContext], str]:
    """Cut the unused padding. Only when the padding is uniform, so untrim restores the exact dump."""
    def run(ctx: TaskContext) -> str:
        size, used, full = trim_info(path)
        if size <= used:
            raise TaskSkipped("already trimmed")
        pad = None
        for b in (0xFF, 0x00):
            if _tail_is(path, used, b):
                pad = b
                break
        if pad is None:
            raise TaskSkipped("the area after the game isn't plain padding, so trimming wouldn't be reversible")
        if size != full:
            raise TaskSkipped(f"file size ({size:,}) isn't the cartridge size ({full:,}); left untouched")
        tmp = _tmp_beside(out, path.suffix)
        try:
            with open(path, "rb") as fi, open(tmp, "wb") as fo:
                left = used
                while left:
                    ctx.check()
                    chunk = fi.read(min(left, 8 << 20))
                    fo.write(chunk)
                    left -= len(chunk)
                    ctx.progress(0.5 * (1 - left / used), "Trimming")
            # prove it's reversible: untrimmed temp must hash like the original
            want = sha1_file(path, ctx, (0.5, 0.8), "Checking")
            h = hashlib.sha1()
            with open(tmp, "rb") as fh:
                while chunk := fh.read(8 << 20):
                    h.update(chunk)
            h.update(bytes([pad]) * (full - used))
            ctx.progress(0.9, "Checking")
            if h.hexdigest() != want:
                raise RuntimeError("trim check failed - nothing changed")
            note = _finish_new_file(ctx, tmp, out, path)
        finally:
            tmp.unlink(missing_ok=True)
        saved = (size - used) / 1024 ** 2
        ctx.log(f"trimmed {size:,} → {used:,} bytes (padding 0x{pad:02X}); untrim restores the exact dump")
        return f"saved {saved:.1f} MiB" + (f"; {note}" if note else "")
    return run


def untrim(path: Path, out: Path, dat_index=None) -> Callable[[TaskContext], str]:
    """Pad back to the full cartridge size. With a DAT, the padding byte that gives the No-Intro dump is chosen."""
    def run(ctx: TaskContext) -> str:
        size, used, full = trim_info(path)
        if size >= full:
            raise TaskSkipped("already full size")
        pads = [0xFF, 0x00]
        chosen, note_dat = 0xFF, ""
        if dat_index is not None:
            base = hashlib.sha1()
            crc = 0
            with open(path, "rb") as fh:
                data_all = fh.read()
            base.update(data_all)
            for b in pads:
                h = base.copy()
                tail = bytes([b]) * (full - size)
                h.update(tail)
                c = zlib.crc32(tail, zlib.crc32(data_all)) & 0xFFFFFFFF
                hits = dat_index.lookup(full, f"{c:08x}", None, h.hexdigest())
                if hits:
                    chosen, note_dat = b, f"matches No-Intro: {hits[0][0].name}"
                    break
            del data_all
        tmp = _tmp_beside(out, path.suffix)
        try:
            with open(path, "rb") as fi, open(tmp, "wb") as fo:
                while chunk := fi.read(8 << 20):
                    ctx.check()
                    fo.write(chunk)
                left = full - size
                block = bytes([chosen]) * (8 << 20)
                while left:
                    n = min(left, len(block))
                    fo.write(block[:n])
                    left -= n
                    ctx.progress(1 - left / max(full - size, 1), "Untrimming")
            note = _finish_new_file(ctx, tmp, out, path)
        finally:
            tmp.unlink(missing_ok=True)
        return ", ".join(x for x in (f"padded to {full:,} bytes", note_dat or "no DAT match (padding 0xFF)", note)
                         if x)
    return run


# ====================================================================== external tools (RVZ via nodtool, NSZ via nsz)

def _run_tool(argv: list[str], ctx: TaskContext, phase: str) -> list[str]:
    from .chdman_tool import Process
    lines: list[str] = []

    def line(s: str):
        lines.append(s)
        ctx.log(s)

    p = Process(argv, line, lambda f: ctx.progress(f, phase))
    watcher = threading.Thread(target=lambda: (ctx.cancel.wait(), p.cancel()), daemon=True)
    watcher.start()
    ctx.progress(0.0, phase)
    rc = p.run()
    ctx.check()
    if rc != 0:
        tail = next((ln for ln in reversed(lines) if ln.strip()), f"exit code {rc}")
        raise RuntimeError(f"{Path(argv[0]).stem}: {tail}")
    return lines


def _nod_sha1(path: Path, ctx: TaskContext) -> str:
    from .compat import find_tool
    out = _run_tool([find_tool("nodtool"), "verify", str(path)], ctx, "Verifying")
    for ln in out:
        m = re.match(r"\s*SHA-?1\s*:\s*([0-9a-fA-F]{40})", ln)
        if m:
            return m.group(1).lower()
    raise RuntimeError("nodtool didn't report a SHA-1")


def convert_disc_nod(src: Path, out: Path) -> Callable[[TaskContext], str]:
    """GameCube/Wii disc image <-> RVZ (or any format nodtool writes), verified by SHA-1 of the disc."""
    def run(ctx: TaskContext) -> str:
        from .compat import find_tool
        exe = find_tool("nodtool")
        if not exe:
            raise RuntimeError("nodtool not found (it's bundled in the packaged builds)")
        if out.exists():
            raise TaskSkipped(f"{out.name} already exists")
        if src.suffix.lower() == ".wbfs" and out.suffix.lower() == ".rvz":
            ctx.log("note: WBFS files were already scrubbed when made, so the RVZ matches the WBFS, not the "
                    "original disc")
        tmp = _tmp_beside(out, out.suffix)
        try:
            src_sha = (sha1_file(src, ctx, (0.0, 0.25), "Hashing the original")
                       if src.suffix.lower() in (".iso", ".gcm") else _nod_sha1(src, ctx))
            _run_tool([exe, "convert", str(src), str(tmp)], ctx, f"Converting to {out.suffix[1:].upper()}")
            new_sha = (sha1_file(tmp, ctx, (0.8, 1.0), "Checking") if out.suffix.lower() in (".iso", ".gcm")
                       else _nod_sha1(tmp, ctx))
            if new_sha != src_sha:
                raise RuntimeError("the converted disc doesn't match the original - nothing changed")
            note = _finish_new_file(ctx, tmp, out, src)
        finally:
            tmp.unlink(missing_ok=True)
        ratio = out.stat().st_size / max(src.stat().st_size, 1) if src.exists() else 0
        return (f"{ratio:.0%} of the original, " if ratio else "") + "verified (same disc SHA-1)" + \
            (f"; {note}" if note else "")
    return run


def find_switch_keys(extra: list[Path] | None = None) -> Path | None:
    cands = [Path.home() / ".switch" / "prod.keys", Path.home() / ".switch" / "keys.txt"]
    for base in extra or []:
        try:
            cands += sorted(base.rglob("prod.keys"))[:3] + sorted(base.rglob("keys.txt"))[:3]
        except OSError:
            pass
    return next((c for c in cands if c.is_file()), None)


def convert_nsz(src: Path, out: Path, keys: Path | None) -> Callable[[TaskContext], str]:
    """NSP/XCI -> NSZ/XCZ (compress) or back (decompress). nsz verifies the hashes of every NCA."""
    def run(ctx: TaskContext) -> str:
        import shutil
        import tempfile
        from .compat import find_tool
        exe = find_tool("nsz")
        if not exe:
            raise RuntimeError("nsz not found (it's bundled in the packaged builds)")
        if not keys or not keys.is_file():
            raise RuntimeError("your Switch prod.keys file is needed - set it in the NSZ options")
        if out.exists():
            raise TaskSkipped(f"{out.name} already exists")
        compress = src.suffix.lower() in (".nsp", ".xci")
        work = Path(tempfile.mkdtemp(prefix=".romtools-nsz-", dir=out.parent))
        try:
            argv = [exe, "-C" if compress else "-D", "-V", "-o", str(work), "--keys", str(keys)]
            if compress:
                argv += ["-K"]          # keep everything, so decompressing gives back the identical NSP
            _run_tool(argv + [str(src)], ctx, "Compressing (NSZ)" if compress else "Decompressing")
            made = [p for p in work.iterdir() if p.is_file()]
            if len(made) != 1:
                raise RuntimeError(f"nsz produced {len(made)} files")
            os.rename(made[0], work / ("result" + out.suffix))
            note = _finish_new_file(ctx, work / ("result" + out.suffix), out, src)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        ratio = out.stat().st_size / max(src.stat().st_size, 1) if src.exists() else 0
        return (f"{ratio:.0%} of the original, " if ratio else "") + "verified by nsz" + (f"; {note}" if note else "")
    return run


# ====================================================================== Switch RomFS / ExeFS (nstool)

def extract_switch_fs(out: Path, keys: Path | None, main: tuple[Path, str], base: tuple[Path, str] | None,
                      tickets: list[tuple[Path, str]], parts: dict[str, int]) -> Callable[[TaskContext], str]:
    """Extract sections of a Switch NCA into folders (``exefs/``, ``romfs/``) with nstool.

    ``main`` is (package, NCA name) of the program (or DLC data) NCA; with ``base`` (the base game's program NCA)
    the update's RomFS patch is applied, so ``romfs/`` is the updated game. The NCAs are copied out of the
    package into a work folder next to the output first (nstool reads NCA files), and removed afterwards."""
    def run(ctx: TaskContext) -> str:
        import shutil
        from .compat import find_tool
        from . import packages as pk
        exe = find_tool("nstool")
        if not exe:
            raise RuntimeError("nstool not found (it's bundled in the builds; from source run ./install.sh)")
        if not keys or not Path(keys).is_file():
            raise RuntimeError("your Switch prod.keys file is needed - set it in the Switch keys option")
        if out.exists():
            raise TaskSkipped(f"{out.name} already exists")
        work = out.parent / f".romtools-switch-{uuid.uuid4().hex[:8]}"
        dest = work / "out"
        try:
            (work / "nca").mkdir(parents=True)
            todo = [main] + ([base] if base else [])
            sizes = []
            for cont, name in todo:
                c = pk._Container(cont)
                try:
                    sizes.append(c.files[name][1])
                finally:
                    c.close()
            total = sum(sizes) or 1
            done_before = 0
            local: list[Path] = []
            for (cont, name), size in zip(todo, sizes):
                target = work / "nca" / f"{len(local)}-{name}"
                ctx.log(f"copying {name} out of {cont.name} ({size / 2**30:.2f} GiB)")
                pk.copy_member(cont, name, target,
                               lambda d, _t, b=done_before: ctx.progress(0.4 * (b + d) / total, "Copying NCA"),
                               ctx.cancel)
                done_before += size
                local.append(target)
            tiks = []
            for cont, name in tickets:
                t = work / "nca" / name
                if not t.exists():
                    pk.copy_member(cont, name, t)
                tiks += ["--tik", str(t)]
            ctx.check()
            base_args = ["--basenca", str(local[1])] if base else []
            dest.mkdir(parents=True, exist_ok=True)
            for i, (part, idx) in enumerate(parts.items()):
                # "/N/" with the trailing slash: the form nstool's own --partN shortcuts use for a partition folder
                argv = [exe, "-k", str(keys), *tiks, *base_args, "-x", f"/{idx}/", str(dest / part), str(local[0])]
                lo, hi = 0.4 + 0.6 * i / len(parts), 0.4 + 0.6 * (i + 1) / len(parts)
                stop = threading.Event()

                def watch(lo=lo, hi=hi, part=part):
                    expect = sizes[0] * (0.05 if part == "exefs" else 0.95) or 1
                    while not stop.wait(1.0):
                        got = sum(f.stat().st_size for f in (dest / part).rglob("*") if f.is_file()) \
                            if (dest / part).exists() else 0
                        ctx.progress(lo + (hi - lo) * min(0.99, got / expect), f"Extracting {part}")
                threading.Thread(target=watch, daemon=True).start()
                try:
                    _run_tool(argv, ctx, f"Extracting {part}")
                finally:
                    stop.set()
                if not (dest / part).is_dir() or not any((dest / part).rglob("*")):
                    # show what nstool sees inside the NCA, so the log says why
                    try:
                        _run_tool([exe, "-k", str(keys), *tiks, *base_args, "--fstree", str(local[0])], ctx,
                                  "Listing the NCA")
                    except Exception as exc:  # noqa: BLE001
                        ctx.log(f"(listing failed: {exc})")
                    raise RuntimeError(f"nstool produced no {part} - see the NCA listing above in the log")
            if "exefs" in parts and not ((dest / "exefs" / "main").exists() or (dest / "exefs" / "main.npdm").exists()):
                raise RuntimeError("the ExeFS has no main / main.npdm - extraction looks incomplete")
            os.rename(dest, out)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        files = sum(1 for f in out.rglob("*") if f.is_file())
        size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
        return f"{', '.join(parts)}: {files} files, {size / 2**30:.2f} GiB" + (" (update applied)" if base else "")
    return run
