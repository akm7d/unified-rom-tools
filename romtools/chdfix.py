"""Convert a CHD to the right media type (CD <-> DVD), safely.

The original is not touched until the replacement is proven identical:

  1. extract the disc image to temp storage (RAM in RAM mode, else the SSD temp folder)
  2. SHA-1 the image
  3. create the new CHD next to the original under a hidden temporary name
  4. chdman verify the new CHD
  5. read the new CHD back and compare its data SHA-1 with step 2
  6. move the original to the trash (restorable), then give the new CHD the original name

Any failure before step 6 deletes the temporary files and leaves the original as it was.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import chd
from .chdman_tool import Process, chdman_bin
from .compat import ram_mode_supported, trash as compat_trash
from .hashing import Cancelled, Entry

Log = Callable[[str], None]
Prog = Callable[[str, float], None]

STAGES = [("extract", 0.30), ("create", 0.45), ("verify", 0.10), ("compare", 0.15)]


@dataclass
class FixResult:
    path: Path
    ok: bool
    message: str
    entries: list[Entry] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def temp_root(ram_mode: bool, ssd_dir: Path) -> Path:
    ram_mode = ram_mode and ram_mode_supported()
    return Path("/dev/shm/romtools-fix") if ram_mode else ssd_dir / "fix"


def _overall(stage: str, f: float) -> float:
    done = 0.0
    for name, w in STAGES:
        if name == stage:
            return done + w * max(0.0, min(1.0, f))
        done += w
    return done


def _run(argv: list[str], stage: str, cancel: threading.Event, log: Log, prog: Prog) -> None:
    p = Process(argv, log, lambda f: prog(stage, f))
    watcher = threading.Thread(target=lambda: (cancel.wait(), p.cancel()), daemon=True)
    watcher.start()
    rc = p.run()
    if cancel.is_set():
        raise Cancelled()
    if rc != 0:
        raise RuntimeError(f"chdman {argv[1]} failed (exit {rc})")


def _sha1(path: Path, cancel: threading.Event) -> str:
    h = hashlib.sha1()
    with open(path, "rb", buffering=0) as fh:
        while chunk := fh.read(8 << 20):
            if cancel.is_set():
                raise Cancelled()
            h.update(chunk)
    return h.hexdigest()


def _trash(path: Path) -> bool:
    return compat_trash(path)


def fix(path: Path, target: str, ram_mode: bool, ssd_dir: Path, cancel: threading.Event,
        log: Log = lambda s: None, progress: Callable[[float, str], None] = lambda f, s: None) -> FixResult:
    """target: "dvd" or "cd". Returns FixResult; the original is only replaced when ok is True."""
    ram_mode = ram_mode and ram_mode_supported()
    exe = chdman_bin()
    if not exe:
        return FixResult(path, False, "chdman not found")
    try:
        ci = chd.info(path)
    except Exception as exc:  # noqa: BLE001
        return FixResult(path, False, f"can't read CHD: {exc}")

    if target == "dvd":
        if ci.kind != "cd" or ci.track_types != ("MODE1",):
            return FixResult(path, False, "only single-track 2048-byte CD CHDs can be converted to DVD")
    elif target == "cd":
        if ci.kind != "dvd":
            return FixResult(path, False, "only DVD CHDs can be converted to CD")
    else:
        return FixResult(path, False, f"unknown target {target}")

    size = ci.frames * 2048 if ci.kind == "cd" else ci.logical_size
    root = temp_root(ram_mode, ssd_dir)
    root.mkdir(parents=True, exist_ok=True)
    margin = 256 << 20
    if shutil.disk_usage(root).free < size + margin or (ram_mode and chd.mem_available() < size + (1 << 30)):
        where = "RAM" if ram_mode else str(root)
        return FixResult(path, False, f"not enough free space in {where} for the {size / 1024 ** 3:.1f} GB disc "
                                      f"image" + (" — switch to SSD mode or close programs" if ram_mode else ""))
    if shutil.disk_usage(path.parent).free < path.stat().st_size + margin:
        return FixResult(path, False, "not enough free space next to the original for the new CHD")

    def prog(stage: str, f: float):
        progress(_overall(stage, f), stage)

    work = Path(tempfile.mkdtemp(prefix=f"chdfix-{os.getpid()}-", dir=root))
    tmp_chd = path.with_name(f".romtools-fix-{uuid.uuid4().hex[:8]}.chd")
    iso = work / "disc.iso"
    try:
        log(f"== Fixing {path.name}: {ci.kind.upper()} → {target.upper()} (temp: {'RAM' if ram_mode else root})")
        # 1. extract
        if ci.kind == "cd":
            _run([exe, "extractcd", "-i", str(path), "-o", str(work / "disc.cue"), "-ob", str(iso), "-f"],
                 "extract", cancel, log, prog)
        else:
            _run([exe, "extractdvd", "-i", str(path), "-o", str(iso), "-f"], "extract", cancel, log, prog)
        if not iso.exists() or iso.stat().st_size != size:
            raise RuntimeError(f"extracted image has unexpected size ({iso.stat().st_size if iso.exists() else 0} "
                               f"bytes, expected {size})")
        # 2. fingerprint
        log("== Hashing the extracted disc image")
        before = _sha1(iso, cancel)
        log(f"   SHA-1 {before}")
        # 3. create
        cmd = "createdvd" if target == "dvd" else "createcd"
        _run([exe, cmd, "-i", str(iso), "-o", str(tmp_chd), "-f"], "create", cancel, log, prog)
        iso.unlink()  # free RAM / disk before reading the new CHD back
        # 4. verify
        _run([exe, "verify", "-i", str(tmp_chd)], "verify", cancel, log, prog)
        # 5. compare contents
        log("== Reading the new CHD back and comparing")
        if ram_mode_supported():   # tiny RAM footprint, no disk writes
            new_info, entries = chd.stream_hash(tmp_chd, chd.RAM_DIR, cancel,
                                                lambda _s, f: prog("compare", f))
        else:
            new_info, entries = chd.extract_and_hash(tmp_chd, root, cancel,
                                                     lambda _s, f: prog("compare", f))
        after = entries[0].full.sha1 if len(entries) == 1 else ""
        log(f"   SHA-1 {after}")
        if after != before:
            raise RuntimeError("the new CHD's data differs from the original — nothing was changed")
        if new_info.kind != target:
            raise RuntimeError(f"new CHD is {new_info.kind}, expected {target}")
        # 6. swap
        if cancel.is_set():
            raise Cancelled()
        log("== Identical. Moving the original to the trash and putting the new CHD in its place")
        if not _trash(path):
            raise RuntimeError("couldn't move the original to the trash, so it was left in place")
        os.rename(tmp_chd, path)
        progress(1.0, "done")
        log(f"== Done: {path.name} is now a {target.upper()} CHD (original is in the trash)")
        entries[0].name = "ISO.iso" if target == "dvd" else "BIN.bin"
        return FixResult(path, True, f"converted to {target.upper()}; original moved to trash", entries,
                         new_info.as_dict())
    except Cancelled:
        log("== Cancelled — original unchanged")
        return FixResult(path, False, "cancelled — original unchanged")
    except Exception as exc:  # noqa: BLE001
        log(f"== Failed: {exc}")
        return FixResult(path, False, f"{exc}" if "original" in str(exc) else f"{exc} — original unchanged")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp_chd.exists():
            try:
                tmp_chd.unlink()
            except OSError:
                pass
