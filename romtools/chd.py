"""CHD support via MAME's chdman.

Redump DATs describe the original tracks (.bin/.iso), not the CHD, so a CHD
is verified by extracting it to a temporary local folder, hashing every
track, and deleting the temporary files.  The CHD itself is only read.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .chdman_tool import chdman_bin
from .hashing import Cancelled, Entry, hash_file


# 2 = GD-ROMs extracted as split .cue/.bin with chdman's virtual pregaps restored (Redump layout).
#     Layout 1 extracted GD-ROMs as .gdi, which drops or misplaces the 2-second pregap before track 2.
TRACK_LAYOUT = 2
SECTOR_RAW = 2352

CD_TAGS = {"CHT2", "CHTR", "CHCD"}
GD_TAGS = {"CHGT", "CHGD"}
DVD_TAGS = {"DVD "}
HD_TAGS = {"GDDD", "IDNT"}


@dataclass
class ChdInfo:
    kind: str          # cd | gd | dvd | hd | raw
    logical_size: int
    sha1: str
    data_sha1: str
    version: int
    tracks: int = 1
    # TRACK_LAYOUT bumps when the way tracks are rebuilt changes, so remembered hashes made the old way get redone.
    track_types: tuple = ()   # e.g. ("MODE1",) for a CD made from an .iso, ("MODE2_RAW", "AUDIO", ...)
    frames: int = 0           # total CD frames (sectors); 0 for non-CD

    def as_dict(self) -> dict:
        return {"kind": self.kind, "sha1": self.sha1, "data_sha1": self.data_sha1, "version": self.version,
                "tracks": self.tracks, "track_types": list(self.track_types), "frames": self.frames,
                "logical_size": self.logical_size, "layout": TRACK_LAYOUT}


def info(path: Path) -> ChdInfo:
    exe = chdman_bin()
    if not exe:
        raise RuntimeError("chdman not found (install the 'mame-tools' package)")
    res = subprocess.run([exe, "info", "-i", str(path)], capture_output=True, text=True,
                         errors="replace", check=False)
    if res.returncode != 0:
        raise RuntimeError(_last_line(res.stderr or res.stdout) or "chdman info failed")
    out = res.stdout
    tags = set(re.findall(r"Tag='(.{4})'", out))
    kind = "raw"
    if tags & GD_TAGS:
        kind = "gd"
    elif tags & CD_TAGS:
        kind = "cd"
    elif tags & DVD_TAGS:
        kind = "dvd"
    elif tags & HD_TAGS:
        kind = "hd"

    def grab(label):
        m = re.search(rf"^{label}:\s*(.+)$", out, re.M)
        return m.group(1).strip() if m else ""

    tracks = re.findall(r"TRACK:(\d+) TYPE:(\S+).*?FRAMES:(\d+)", out)
    return ChdInfo(kind, int(grab("Logical size").split()[0].replace(",", "") or 0),
                   grab("SHA1").lower(), grab("Data SHA1").lower(), int(grab("File Version") or 0),
                   max(1, len(tracks)), tuple(t[1] for t in tracks), sum(int(t[2]) for t in tracks))


def _last_line(s: str) -> str:
    lines = [l.strip() for l in re.split(r"[\r\n]+", s or "") if l.strip()]
    return lines[-1] if lines else ""


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _run(cmd: list[str], cancel: threading.Event | None, progress: Callable[[float], None] | None) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tail = b""
    pct = re.compile(rb"(\d+(?:\.\d+)?)% complete")
    try:
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled()
            chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
            if not chunk:
                break
            tail = (tail + chunk)[-4096:]
            if progress:
                hits = pct.findall(chunk)
                if hits:
                    progress(float(hits[-1]) / 100.0)
    finally:
        proc.wait()
    if proc.returncode != 0:
        msg = _last_line(tail.decode(errors="replace"))
        raise RuntimeError(f"chdman: {msg or 'exit code %d' % proc.returncode}")


def _extract_cmd(ci: ChdInfo, path: Path, work: Path) -> list[str]:
    i, o = str(path), str(work / "t")
    CHDMAN = chdman_bin()
    if ci.kind == "cd":
        return [CHDMAN, "extractcd", "-i", i, "-o", o + ".cue", "-sb", "-f"]
    if ci.kind == "gd":
        # split .cue/.bin, not .gdi: the cue keeps Redump's track boundaries (see _cue_pregaps)
        return [CHDMAN, "extractcd", "-i", i, "-o", o + ".cue", "-sb", "-f"]
    if ci.kind == "dvd":
        return [CHDMAN, "extractdvd", "-i", i, "-o", o + ".iso", "-f"]
    if ci.kind == "hd":
        return [CHDMAN, "extracthd", "-i", i, "-o", o + ".img", "-f"]
    return [CHDMAN, "extractraw", "-i", i, "-o", o + ".bin", "-f"]


def _cue_pregaps(cue: Path) -> dict[str, int]:
    """{bin file name: pregap frames} for tracks whose cue says PREGAP, i.e. a gap chdman didn't write
    into the file. Redump's track files contain that gap (as silence), so it's hashed back in as zeros."""
    out: dict[str, int] = {}
    try:
        text = cue.read_text(errors="replace")
    except OSError:
        return out
    current = ""
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'FILE\s+"(.+)"\s+\S+$', line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"PREGAP\s+(\d+):(\d+):(\d+)$", line)
        if m and current:
            mm, ss, ff = map(int, m.groups())
            out[current] = (mm * 60 + ss) * 75 + ff
    return out


def _track_label(i: int, n: int, path: Path) -> str:
    return (f"Track {i:02d}" if n > 1 else path.suffix.lstrip(".").upper()) + path.suffix


def extract_and_hash(path: Path, tmp_root: Path, cancel: threading.Event | None = None,
                     progress: Callable[[str, float], None] | None = None) -> tuple[ChdInfo, list[Entry]]:
    """Return (info, entries). Entries are the data tracks in order; .cue/.gdi are skipped."""
    ci = info(path)
    tmp_root.mkdir(parents=True, exist_ok=True)
    need = ci.logical_size + 64 * 1024 * 1024
    free = shutil.disk_usage(tmp_root).free
    if free < need:
        raise RuntimeError(f"not enough space in {tmp_root} ({free >> 20} MiB free, need {need >> 20} MiB)")

    work = Path(tempfile.mkdtemp(prefix=f"chd-{os.getpid()}-", dir=tmp_root))
    try:
        stage = lambda f: progress and progress("extracting", f)  # noqa: E731
        cmd = _extract_cmd(ci, path, work)
        _run(cmd, cancel, stage)

        files = sorted((p for p in work.iterdir() if p.suffix.lower() not in (".cue", ".gdi")),
                       key=lambda p: _natural(p.name))
        total = sum(p.stat().st_size for p in files) or 1
        done = 0

        def hp(n):
            nonlocal done
            done += n
            if progress:
                progress("hashing", done / total)

        pregaps = _cue_pregaps(work / "t.cue")
        entries = []
        for i, f in enumerate(files, 1):
            entries.append(hash_file(f, cancel, hp, name=_track_label(i, len(files), f),
                                     prefix_zeros=pregaps.get(f.name, 0) * SECTOR_RAW))
            f.unlink()  # free space early on multi-track discs
        return ci, entries
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cleanup_stale(tmp_root: Path) -> None:
    """Remove leftovers from a crashed run."""
    if not tmp_root.is_dir():
        return
    for p in tmp_root.glob("chd-*-*"):
        try:
            pid = int(p.name.split("-")[1])
        except ValueError:
            continue
        if pid != os.getpid() and Path(f"/proc/{pid}").exists():
            continue  # another running instance owns it
        shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------- RAM streaming mode
#
# chdman can't write to a pipe (it uses positioned writes), but it writes each
# output file strictly front-to-back.  So in RAM mode it writes into tmpfs
# (/dev/shm) while we read right behind it, hash, and punch out (free) every
# chunk we've hashed.  If hashing falls behind, chdman is paused (SIGSTOP)
# until we catch up.  RAM use stays around HIGH_WATER even for 8.5 GB discs,
# and nothing is ever written to the SSD.

RAM_DIR = Path("/dev/shm/romtools")
STD_PREGAP = 150         # frames (2 seconds), the gap before an audio track
HIGH_WATER = 256 << 20   # pause chdman when this much unhashed data is buffered
LOW_WATER = 32 << 20     # resume it below this
READ_CHUNK = 8 << 20
PUNCH_ALIGN = 1 << 20

_libc = None


def _punch(fd: int, offset: int, length: int) -> bool:
    """Free [offset, offset+length) of a tmpfs file (FALLOC_FL_PUNCH_HOLE | KEEP_SIZE)."""
    global _libc
    if length <= 0:
        return True
    if _libc is None:
        import ctypes
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
    return _libc.fallocate(fd, 0x02 | 0x01, offset, length) == 0


def is_tmpfs(path: Path) -> bool:
    best, fstype = "", ""
    p = str(path.resolve() if path.exists() else path)
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                mnt = parts[1].replace("\\040", " ")
                if (p == mnt or p.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, fstype = mnt, parts[2]
    except OSError:
        return False
    return fstype in ("tmpfs", "ramfs")


def mem_available() -> int:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def stream_hash(path: Path, ram_root: Path = RAM_DIR, cancel: threading.Event | None = None,
                progress: Callable[[str, float], None] | None = None) -> tuple[ChdInfo, list[Entry]]:
    """Like extract_and_hash, but through RAM with a small, bounded footprint."""
    import signal
    from .hashing import _Hasher

    ci = info(path)
    ram_root.mkdir(parents=True, exist_ok=True)
    if not is_tmpfs(ram_root):
        raise RuntimeError(f"{ram_root} is not in RAM (tmpfs); refusing to use it for RAM mode")
    need = HIGH_WATER + READ_CHUNK + (64 << 20)
    if shutil.disk_usage(ram_root).free < need or mem_available() < need + (256 << 20):
        raise RuntimeError(f"not enough free RAM for RAM mode (need ~{need >> 20} MiB)")

    work = Path(tempfile.mkdtemp(prefix=f"chd-{os.getpid()}-", dir=ram_root))
    proc = subprocess.Popen(_extract_cmd(ci, path, work), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    err_tail = bytearray()
    pct = [0.0]

    def drain():
        rx = re.compile(rb"(\d+(?:\.\d+)?)% complete")
        for chunk in iter(lambda: proc.stderr.read1(4096), b""):
            err_tail.extend(chunk)
            del err_tail[:-4096]
            hits = rx.findall(chunk)
            if hits:
                pct[0] = float(hits[-1]) / 100.0
                if progress:
                    progress("extract+hash in RAM", pct[0])

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    stopped = False

    def resume():
        nonlocal stopped
        if stopped:
            try:
                os.kill(proc.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            stopped = False

    done_files: list[tuple[Path, Entry]] = []
    seen: set[str] = set()
    cur: Path | None = None
    fd = -1
    off = punched = 0
    h = None
    try:
        while True:
            if cancel is not None and cancel.is_set():
                resume()
                proc.kill()
                raise Cancelled()
            exited = proc.poll() is not None
            files = sorted((p for p in work.iterdir()
                            if p.suffix.lower() not in (".cue", ".gdi") and p.name not in seen),
                           key=lambda p: _natural(p.name))
            if cur is None:
                if files:
                    cur, off, punched, h = files[0], 0, 0, _Hasher()
                    idx = len(done_files)
                    hg = None  # same data with a standard 2-second silent pregap in front, for audio tracks
                    if idx > 0 and idx < len(ci.track_types) and ci.track_types[idx] == "AUDIO":
                        hg = _Hasher()
                        hg.update(bytes(STD_PREGAP * SECTOR_RAW))
                    fd = os.open(cur, os.O_RDWR)  # RDWR needed to punch holes
                    continue
                if exited:
                    break
                time.sleep(0.02)
                continue
            size = os.fstat(fd).st_size
            if off < size:
                data = os.pread(fd, min(size - off, READ_CHUNK), off)
                if not data:
                    time.sleep(0.01)
                    continue
                h.update(data)
                if hg is not None:
                    hg.update(data)
                off += len(data)
                upto = off - off % PUNCH_ALIGN
                if upto > punched and _punch(fd, punched, upto - punched):
                    punched = upto
                gap = size - off
                if not stopped and gap > HIGH_WATER and not exited:
                    os.kill(proc.pid, signal.SIGSTOP)
                    stopped = True
                elif stopped and gap < LOW_WATER:
                    resume()
                continue
            # caught up with the writer
            resume()
            later = [p for p in files if p != cur]
            if later or exited:
                if exited and os.fstat(fd).st_size > off:
                    continue  # final bytes landed after our stat
                os.close(fd)
                fd = -1
                done_files.append((cur, Entry(cur.name, h.result()), hg.result() if hg else None))
                seen.add(cur.name)
                cur.unlink()
                cur = None
                continue
            time.sleep(0.005)
        proc.wait()
        t.join(timeout=5)
        if proc.returncode != 0:
            msg = _last_line(bytes(err_tail).decode(errors="replace"))
            raise RuntimeError(f"chdman: {msg or 'exit code %d' % proc.returncode}")
        pregaps = _cue_pregaps(work / "t.cue")
        n = len(done_files)
        entries = []
        for i, (p, e, with_gap) in enumerate(done_files, 1):
            gap = pregaps.get(p.name, 0)
            if gap == STD_PREGAP and with_gap is not None:
                e.full = with_gap
            elif gap:
                raise RuntimeError(f"unusual {gap}-frame pregap on {p.name}; switch to SSD mode for this disc")
            e.name = _track_label(i, n, p)
            entries.append(e)
        return ci, entries
    finally:
        resume()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if fd >= 0:
            os.close(fd)
        shutil.rmtree(work, ignore_errors=True)
