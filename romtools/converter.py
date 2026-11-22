"""Batch job planning and execution for chdman (create / extract / verify / recompress / info).

Sources are only read, unless the user explicitly enables "move sources to
trash after success" - and even then only after the output was verified.
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import chd, discdetect
from .discdetect import CD, DVD
from .chdman_tool import Process, build_argv, chdman_bin
from .scanner import _cue_parts, _gdi_parts

# ------------------------------------------------------------------ presets

TO_CHD, FROM_CHD, VERIFY, INFO, RECOMPRESS, CUSTOM = "to_chd", "from_chd", "verify", "info", "recompress", "custom"

PRESETS = {
    TO_CHD: ("Create CHD (auto CD / GD-ROM / DVD)", (".cue", ".gdi", ".toc", ".iso", ".nrg")),
    FROM_CHD: ("Extract CHD back to original (auto)", (".chd",)),
    VERIFY: ("Verify CHD integrity", (".chd",)),
    RECOMPRESS: ("Recompress CHD (copy with new settings)", (".chd",)),
    INFO: ("Show CHD info", (".chd",)),
}

CD_CODECS = {
    "default": ("chdman default (cdlz,cdzl,cdfl) — best compatibility", ""),
    "zstd": ("Zstandard (cdzs,cdzl,cdfl) — faster to read, needs a recent emulator", "cdzs,cdzl,cdfl"),
    "none": ("No compression", "none"),
}
DVD_CODECS = {
    "default": ("chdman default (lzma,zlib,huff,flac) — best compatibility", ""),
    "zstd": ("Zstandard (zstd,zlib,huff,flac) — faster to read, needs a recent emulator", "zstd,zlib,huff,flac"),
    "lzma": ("LZMA only — smallest, slowest to read", "lzma"),
    "none": ("No compression", "none"),
}


@dataclass
class Options:
    output_dir: str = ""            # "" = next to the source
    overwrite: bool = False
    cd_codec: str = "default"
    dvd_codec: str = "default"
    custom_codec: str = ""          # overrides the presets when set
    hunk_size: str = ""             # "" = chdman default; "2048" is common for PSP/PS2 DVDs
    iso_as: str = "detect"          # detect | dvd | cd  (.iso only; detect = discdetect)
    split_bin: bool = True          # extract CDs as one .bin per track (Redump layout)
    processors: int = 0             # 0 = all
    verify_after: bool = True       # chdman verify on new CHDs
    trash_sources: bool = False     # move inputs (and their tracks) to the trash after a verified success
    dat_lookup: Callable[[Path], object] | None = None  # Path -> DatIndex of its system (PS2 CD/DVD check)


@dataclass
class Job:
    id: int
    title: str
    source: Path
    steps: list[list[str]]                  # argv lists run in order
    outputs: list[Path] = field(default_factory=list)
    sources: list[Path] = field(default_factory=list)   # files to trash on success (if enabled)
    status: str = "Queued"                  # Queued | Running | Done | Failed | Skipped | Cancelled
    progress: float = 0.0
    step_index: int = 0
    log: list[str] = field(default_factory=list)
    message: str = ""
    started: float = 0.0
    finished: float = 0.0
    trash: bool = False
    detail: str = ""                        # e.g. how CD vs DVD was decided
    step_label: str = ""                    # e.g. "Step 1/2 · Creating DVD CHD"
    phase: str = ""                         # chdman's live text, e.g. "Compressing · ratio 40.1%"
    step_progress: float = 0.0
    step_started: float = 0.0
    weight: int = 0                         # bytes of input, for an overall size-weighted progress

    @property
    def step_name(self) -> str:
        if not self.steps:
            return ""
        return STEP_NAMES.get(self.steps[min(self.step_index, len(self.steps) - 1)][1], "Running chdman")

    def measure(self) -> int:
        if not self.weight:
            try:
                files = {self.source, *self.sources}
                self.weight = sum(f.stat().st_size for f in files if f.is_file()) or 1
            except OSError:
                self.weight = 1
        return self.weight


STEP_NAMES = {
    "createcd": "Creating CD CHD", "createdvd": "Creating DVD CHD", "createhd": "Creating hard-disk CHD",
    "createraw": "Creating raw CHD", "createld": "Creating LaserDisc CHD", "copy": "Recompressing CHD",
    "verify": "Verifying CHD", "info": "Reading CHD info", "extractcd": "Extracting CD",
    "extractdvd": "Extracting DVD", "extracthd": "Extracting hard disk", "extractraw": "Extracting raw data",
    "extractld": "Extracting LaserDisc", "addmeta": "Adding metadata", "delmeta": "Removing metadata",
    "dumpmeta": "Dumping metadata", "listtemplates": "Listing templates",
}


def pretty_phase(line: str) -> str:
    """'Compressing, 45.3% complete... (ratio=40.1%)' -> 'Compressing · ratio 40.1%'."""
    import re as _re
    verb = line.split(",", 1)[0].strip()
    m = _re.search(r"ratio=([\d.]+%)", line)
    return verb + (f" · ratio {m.group(1)}" if m else "")


_ids = itertools.count(1)


def _out_dir(src: Path, opts: Options) -> Path:
    return Path(opts.output_dir).expanduser() if opts.output_dir else src.parent


def _codec(kind: str, opts: Options) -> str:
    if opts.custom_codec.strip():
        return opts.custom_codec.strip()
    table = DVD_CODECS if kind == "dvd" else CD_CODECS
    return table.get(opts.dvd_codec if kind == "dvd" else opts.cd_codec, ("", ""))[1]


def _common(opts: Options, kind: str) -> dict:
    v = {"compression": _codec(kind, opts), "hunksize": opts.hunk_size.strip()}
    if opts.processors:
        v["numprocessors"] = str(opts.processors)
    if opts.overwrite:
        v["force"] = True
    return v


def plan(preset: str, files: list[Path], opts: Options) -> list[Job]:
    jobs = []
    for src in files:
        j = _plan_one(preset, src, opts)
        if j:
            jobs.append(j)
    return jobs


def _plan_one(preset: str, src: Path, opts: Options) -> Job | None:
    ext = src.suffix.lower()
    out_dir = _out_dir(src, opts)
    exe = chdman_bin()
    if preset == TO_CHD:
        det = discdetect.detect(src, dat_index=opts.dat_lookup(src) if opts.dat_lookup else None)
        kind, note = det.media, det.summary
        if ext == ".iso" and opts.iso_as in (CD, DVD) and opts.iso_as != det.media:
            kind, note = opts.iso_as, f"forced {opts.iso_as.upper()} by setting (detected: {det.summary})"
        cmd = "createdvd" if kind == "dvd" else "createcd"
        out = out_dir / (src.stem + ".chd")
        steps = [build_argv(cmd, {"input": str(src), "output": str(out), **_common(opts, kind)}, exe)]
        if opts.verify_after:
            steps.append(build_argv("verify", {"input": str(out)}, exe))
        parts = _cue_parts(src) if ext == ".cue" else _gdi_parts(src) if ext == ".gdi" else []
        j = Job(next(_ids), f"{cmd}: {src.name}", src, steps, [out], [src] + parts)
        j.detail = note
        j.log.append(f"Detected: {note}")
        j.trash = opts.trash_sources and opts.verify_after  # only ever after a verified output
        return j
    if preset == FROM_CHD:
        try:
            ci = chd.info(src)
        except Exception as exc:  # noqa: BLE001
            j = Job(next(_ids), f"extract: {src.name}", src, [])
            j.status, j.message = "Failed", str(exc)
            return j
        force = {"force": True} if opts.overwrite else {}
        if ci.kind in ("cd", "gd"):
            out = out_dir / (src.stem + (".gdi" if ci.kind == "gd" else ".cue"))
            vals = {"input": str(src), "output": str(out), **force}
            if ci.kind == "cd" and opts.split_bin and ci.tracks > 1:  # Redump names single-track bins plainly
                vals["splitbin"] = True
            steps = [build_argv("extractcd", vals, exe)]
        elif ci.kind == "dvd":
            out = out_dir / (src.stem + ".iso")
            steps = [build_argv("extractdvd", {"input": str(src), "output": str(out), **force}, exe)]
        elif ci.kind == "hd":
            out = out_dir / (src.stem + ".img")
            steps = [build_argv("extracthd", {"input": str(src), "output": str(out), **force}, exe)]
        else:
            out = out_dir / (src.stem + ".bin")
            steps = [build_argv("extractraw", {"input": str(src), "output": str(out), **force}, exe)]
        return Job(next(_ids), f"{steps[0][1]}: {src.name}", src, steps, [out], [src])
    if preset == VERIFY:
        return Job(next(_ids), f"verify: {src.name}", src, [build_argv("verify", {"input": str(src)}, exe)])
    if preset == INFO:
        return Job(next(_ids), f"info: {src.name}", src,
                   [build_argv("info", {"input": str(src), "verbose": True}, exe)])
    if preset == RECOMPRESS:
        try:
            kind = chd.info(src).kind
        except Exception:  # noqa: BLE001
            kind = "cd"
        out = out_dir / (src.stem + ".chd")
        if out == src:
            out = out_dir / (src.stem + " (recompressed).chd")
        steps = [build_argv("copy", {"input": str(src), "output": str(out),
                                     **_common(opts, "dvd" if kind == "dvd" else "cd")}, exe)]
        if opts.verify_after:
            steps.append(build_argv("verify", {"input": str(out)}, exe))
        j = Job(next(_ids), f"copy: {src.name}", src, steps, [out], [src])
        j.trash = opts.trash_sources and opts.verify_after
        return j
    return None


def custom_job(argv: list[str], title: str = "") -> Job:
    src = Path(argv[argv.index("--input") + 1]) if "--input" in argv else Path(".")
    outs = [Path(argv[argv.index("--output") + 1])] if "--output" in argv else []
    return Job(next(_ids), title or f"{argv[1]}: {src.name}", src, [argv], outs)


def collect(paths: list[Path], preset: str, recursive: bool = True) -> list[Path]:
    """Expand folders into matching files. For CD sets, take the .cue/.gdi, not the tracks."""
    exts = PRESETS.get(preset, ("", ()))[1]
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            out += sorted((f for f in it if f.is_file() and f.suffix.lower() in exts), key=lambda f: str(f).lower())
        elif p.is_file() and (not exts or p.suffix.lower() in exts):
            out.append(p)
    # Drop .iso/.bin that a .cue/.gdi in the list already references.
    referenced = set()
    for f in out:
        if f.suffix.lower() == ".cue":
            referenced.update(_cue_parts(f))
        elif f.suffix.lower() == ".gdi":
            referenced.update(_gdi_parts(f))
    seen, res = set(), []
    for f in out:
        if f not in referenced and f not in seen:
            seen.add(f)
            res.append(f)
    return res


# ------------------------------------------------------------------ running

class Runner:
    def __init__(self, jobs: list[Job], parallel: int, on_update: Callable[[Job], None],
                 trash: Callable[[Path], bool] | None = None) -> None:
        self.jobs = jobs
        self.parallel = max(1, parallel)
        self.on_update = on_update
        self.trash = trash or _gio_trash
        self.cancel = threading.Event()
        self._procs: dict[int, Process] = {}
        self._lock = threading.Lock()

    def stop(self) -> None:
        self.cancel.set()
        with self._lock:
            for p in self._procs.values():
                p.cancel()

    def run(self) -> None:
        with ThreadPoolExecutor(max_workers=self.parallel) as ex:
            list(ex.map(self._run_job, [j for j in self.jobs if j.status == "Queued"]))

    def _run_job(self, job: Job) -> None:
        if self.cancel.is_set():
            job.status = "Cancelled"
            self.on_update(job)
            return
        existing = [o for o in job.outputs if o.exists()]
        main_has_force = bool(job.steps) and "--force" in job.steps[0]
        if existing and not main_has_force and job.steps and job.steps[0][1] not in ("verify", "info"):
            job.status, job.message = "Skipped", f"output exists: {existing[0].name} (tick Overwrite to replace)"
            self.on_update(job)
            return
        for o in job.outputs:
            o.parent.mkdir(parents=True, exist_ok=True)
        job.status, job.started = "Running", time.time()
        self.on_update(job)
        n = len(job.steps)
        for i, argv in enumerate(job.steps):
            job.step_index = i
            job.step_label = f"Step {i + 1}/{n} · {job.step_name}"
            job.step_progress, job.step_started, job.phase = 0.0, time.time(), "starting chdman…"
            job.log.append(f"── {job.step_label} ──")

            def prog(f, i=i):
                job.step_progress = f
                job.progress = (i + f) / n
                self.on_update(job)

            def status(s):
                job.phase = pretty_phase(s)

            def line(s):
                job.log.append(s)
                if s.startswith("Error") or "error" in s.lower():
                    job.message = s
                self.on_update(job)

            p = Process(argv, line, prog, status)
            with self._lock:
                self._procs[job.id] = p
            if self.cancel.is_set():
                p.cancel()
            rc = p.run()
            with self._lock:
                self._procs.pop(job.id, None)
            if p.cancelled:
                job.status, job.message = "Cancelled", "stopped by user"
                self._cleanup_partial(job, existing)
                break
            if rc != 0:
                job.status = "Failed"
                job.message = job.message or f"{argv[1]} exited with code {rc}"
                if i == 0:
                    self._cleanup_partial(job, existing)
                break
        else:
            job.status, job.progress = "Done", 1.0
            if argv[1] == "verify" and n > 1:
                job.message = "created and verified"
            elif argv[1] == "info":
                job.message = "see log"
            elif argv[1] == "verify":
                job.message = "integrity OK"
            if job.trash and job.sources and job.outputs:
                job.step_label, job.phase = "Moving sources to the trash", ""
                self.on_update(job)
                bad = [s for s in job.sources if s.exists() and not self.trash(s)]
                job.message += ("; sources moved to trash" if not bad else
                                f"; could not trash {len(bad)} source file(s)")
        job.finished = time.time()
        job.phase = ""
        self.on_update(job)

    @staticmethod
    def _cleanup_partial(job: Job, existing_before: list[Path]) -> None:
        """Remove half-written outputs we created (never files that existed before)."""
        for o in job.outputs:
            if o not in existing_before and o.exists() and o.suffix.lower() == ".chd":
                try:
                    o.unlink()
                except OSError:
                    pass


def _gio_trash(path: Path) -> bool:
    if shutil.which("gio"):
        return subprocess.run(["gio", "trash", str(path)], capture_output=True).returncode == 0
    return False


def free_space(path: Path) -> int:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


def estimate_output(job: Job) -> int:
    """Rough upper bound of bytes a job writes (for a free-space warning)."""
    try:
        if job.steps and job.steps[0][1].startswith("extract"):
            return chd.info(job.source).logical_size
        return sum(s.stat().st_size for s in [job.source] + [x for x in job.sources if x != job.source]
                   if s.exists())
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["Options", "Job", "Runner", "plan", "collect", "custom_job", "PRESETS", "CD_CODECS", "DVD_CODECS",
           "TO_CHD", "FROM_CHD", "VERIFY", "INFO", "RECOMPRESS", "free_space", "estimate_output"]
