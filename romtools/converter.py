"""Batch job planning and execution for chdman (create / extract / verify / recompress / info).

Sources are only read, unless the user explicitly enables "move sources to
trash after success" - and even then only after the output was verified.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import chd, discdetect
from .discdetect import CD, DVD
from .compat import lies_inside
from .chdman_tool import Process, build_argv, chdman_bin
from .scanner import _cue_parts, _gdi_parts

# ------------------------------------------------------------------ presets

TO_CHD, FROM_CHD, VERIFY, INFO, RECOMPRESS, CUSTOM = "to_chd", "from_chd", "verify", "info", "recompress", "custom"
FIX_CHD, M3U, TRIM, UNTRIM = "fix_chd", "m3u", "trim", "untrim"
TO_RVZ, FROM_RVZ, TO_NSZ, FROM_NSZ = "to_rvz", "from_rvz", "to_nsz", "from_nsz"
SWITCH_FS = "switch_fs"
PS4_EXTRACT = "ps4_extract"
DISC_EXTS = (".chd", ".cue", ".gdi", ".iso", ".pbp", ".rvz", ".ccd", ".mds")

PRESETS = {
    TO_CHD: ("Create CHD (auto CD / GD-ROM / DVD)", (".cue", ".gdi", ".toc", ".iso", ".nrg")),
    FROM_CHD: ("Extract CHD back to original (auto)", (".chd",)),
    VERIFY: ("Verify CHD integrity", (".chd",)),
    RECOMPRESS: ("Recompress CHD (copy with new settings)", (".chd",)),
    INFO: ("Show CHD info", (".chd",)),
    FIX_CHD: ("Fix CHD types (CD ↔ DVD, lossless)", (".chd",)),
    M3U: ("Create .m3u playlists for multi-disc games", DISC_EXTS),
    TO_RVZ: ("ISO / GCZ / WIA / WBFS → RVZ (Dolphin)", (".iso", ".gcm", ".gcz", ".wia", ".ciso", ".wbfs")),
    FROM_RVZ: ("RVZ → ISO", (".rvz",)),
    TO_NSZ: ("NSP / XCI → NSZ / XCZ (compress)", (".nsp", ".xci")),
    FROM_NSZ: ("NSZ / XCZ → NSP / XCI (decompress)", (".nsz", ".xcz")),
    SWITCH_FS: ("NSP / XCI → RomFS + ExeFS folders (auto-applies the newest update NSP / XCI)",
                (".nsp", ".xci", ".nsz", ".xcz")),
    PS4_EXTRACT: ("PKG → extracted game folder (BETA - use with care)", (".pkg",)),
    TRIM: ("Trim ROMs (reversible)", (".nds", ".dsi", ".3ds", ".cci")),
    UNTRIM: ("Untrim ROMs back to the full dump", (".nds", ".dsi", ".3ds", ".cci")),
}

# Which systems each task is for: (abbreviations shown in front of the task, full names for its tooltip)
_CHD_SYS = ("PS1/PS2/PSP/DC/SAT/SCD/PCE…",
            "PlayStation, PlayStation 2, PSP, Dreamcast, Saturn, Sega CD / Mega-CD, PC Engine CD / TurboGrafx-CD, "
            "3DO, Philips CD-i, Neo Geo CD (and Xbox / Xbox 360 ISOs, for archiving)")
PRESET_SYSTEMS = {
    TO_CHD: _CHD_SYS, FROM_CHD: _CHD_SYS, RECOMPRESS: _CHD_SYS, VERIFY: _CHD_SYS, INFO: _CHD_SYS,
    FIX_CHD: ("PS1/PS2/PSP/…", "Any CHD made as the wrong type - most often PlayStation 2 and PSP discs stored as "
                              "CD instead of DVD (or the reverse)"),
    M3U: ("PS1/PS2/DC/SAT/SCD/PCE…", "Multi-disc games: PlayStation, PlayStation 2, Dreamcast, Saturn, Sega CD, "
                                       "PC Engine CD and any other disc system"),
    TO_RVZ: ("GC/Wii", "Nintendo GameCube and Wii"), FROM_RVZ: ("GC/Wii", "Nintendo GameCube and Wii"),
    TO_NSZ: ("NSW", "Nintendo Switch"), FROM_NSZ: ("NSW", "Nintendo Switch"), SWITCH_FS: ("NSW", "Nintendo Switch"),
    PS4_EXTRACT: ("PS4", "PlayStation 4 (fake / homebrew packages only)"),
    TRIM: ("NDS/3DS", "Nintendo DS (and DSi) and Nintendo 3DS"), UNTRIM: ("NDS/3DS", "Nintendo DS (and DSi) and 3DS"),
}


def preset_label(key: str) -> str:
    """'[GC/Wii]  RVZ → ISO' - the systems first, so the list reads at a glance (full names in the tooltip)."""
    short = PRESET_SYSTEMS.get(key, ("", ""))[0]
    return f"[{short}]  {PRESETS[key][0]}" if short else PRESETS[key][0]


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
    all_dats: Callable[[], object] | None = None        # -> DatIndex of every DAT (CHD type check, untrim)
    switch_keys: str = ""                               # prod.keys for NSZ
    title_keys: str = ""                                # title.keys (Switch content whose package has no ticket)
    ram: bool = False                                   # CHD type fix: unpack in RAM
    tmp_dir: str = ""
    ps4_merge: bool = True                              # a PS4 game's update goes into the game's folder


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
    func: Callable | None = None            # Python task instead of chdman steps: func(ctx) -> message
    func_label: str = ""                    # e.g. "Trimming", "Converting to RVZ"
    log_total: int = 0                      # lines ever logged (the log keeps only the newest ones)
    follows: "Job | None" = None            # a row that only mirrors another job (e.g. the update it applies)
    preset: str = ""                        # the task it was planned for, with the options that shaped it, so a
    planned: dict = field(default_factory=dict)     # queued job can be planned again when those change
    overrides: dict = field(default_factory=dict)   # options fixed by whoever queued it (ROM Storage Advisor)

    @property
    def step_name(self) -> str:
        if self.func is not None:
            return self.func_label or "Working"
        if not self.steps:
            return ""
        return STEP_NAMES.get(self.steps[min(self.step_index, len(self.steps) - 1)][1], "Running chdman")

    def measure(self) -> int:
        if self.follows is not None:        # done by the job it follows: don't count its size twice
            self.weight = 0
            return 0
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


def _task_job(title: str, src: Path, func, label: str, outputs=(), sources=(), opts: Options | None = None,
              detail: str = "") -> Job:
    j = Job(next(_ids), title, src, [], list(outputs), list(sources))
    j.func, j.func_label, j.detail = func, label, detail
    j.trash = bool(opts and opts.trash_sources)
    return j


def _skipped(title: str, src: Path, why: str) -> Job:
    j = Job(next(_ids), title, src, [])
    j.status, j.message = "Skipped", why
    return j


def _open_ps4(src: Path):
    """(package info, its files, why it can't be extracted or "")."""
    from . import packages, ps4pkg
    info = None
    try:
        info = packages.read_ps4(src)
        with ps4pkg.PkgReader(src) as r:
            if not r.has_files:
                return info, [], "nothing to extract: it only holds a licence (an unlock-only DLC)"
            return info, r.files(), ""
    except (packages.PackageError, ps4pkg.Ps4PkgError, OSError) as exc:
        return info, [], str(exc)


def _extracted_ps4(folder: Path) -> tuple[str, int, str] | None:
    """(title ID, version number, version) of an extracted PS4 game folder."""
    from . import packages
    try:
        d = packages._sfo((folder / "sce_sys" / "param.sfo").read_bytes())
    except Exception:  # noqa: BLE001 - no game, or not readable
        return None
    ver = str(d.get("APP_VER") or "")
    return str(d.get("TITLE_ID", "")), packages._ps4_version(ver), re.sub(r"^0(\d)", r"\1", ver)


def plan_ps4(files: list[Path], opts: Options) -> list[Job]:
    """One job per game: the game's package with its newest update (picked, or next to it) merged in, in the folder
    emulators expect. An update whose game's package isn't there goes into the already extracted game, else into its
    own "CUSA…-patch" folder. With merging off, every package gets its own folder."""
    from . import packages, ps4pkg, tasks
    picked: list[Path] = []
    for f in files:
        ps = ps4pkg.pieces(f)
        f = ps[0] if ps else f                  # a split package counts once
        if f not in picked:
            picked.append(f)
    seen: dict[Path, tuple] = {}

    def opened(p: Path):
        if p not in seen:
            seen[p] = _open_ps4(p)
        return seen[p]

    def neighbours(p: Path, title_id: str, kind: str) -> list[Path]:
        """Packages of the same game in the same folder."""
        out = []
        try:
            others = sorted(x for x in p.parent.iterdir() if x.suffix.lower() == ".pkg" and x.is_file())
        except OSError:
            return []
        for x in others:
            ps = ps4pkg.pieces(x)
            if x == p or ps and x != ps[0]:
                continue
            try:
                i = packages.read_ps4(x)
            except (packages.PackageError, OSError):
                continue
            if i.title_id == title_id and i.kind == kind:
                out.append(x)
        return out

    def job(title, src, func, out, what, n, size):
        return _task_job(title, src, func, "Extracting (beta)", [out], [],
                         detail=f"⚠ BETA - {what}, {n} files, {size / 1e9:.2f} GB → {out.name}/")
    jobs: list[Job] = []
    done: set[Path] = set()
    infos = {p: opened(p) for p in picked}
    # games first, so an update picked together with its game is merged into that game's job
    for src in sorted(picked, key=lambda p: infos[p][0] is not None and infos[p][0].kind == packages.UPDATE):
        if src in done:
            continue
        done.add(src)
        info, fl, why = infos[src]
        title = f"extract PKG: {src.name}"
        if why:
            jobs.append(_skipped(title, src, why))
            continue
        base = _out_dir(src, opts)
        size = sum(f.size for f in fl)
        if info.kind == packages.DLC:
            out = base / "addcont" / info.title_id / info.content_id.split("-")[-1]
            jobs.append(job(title, src, tasks.extract_ps4(src, out), out, info.kind, len(fl), size))
            continue
        home = base / info.title_id
        if info.kind == packages.UPDATE:
            got = _extracted_ps4(home) if opts.ps4_merge else None
            games = [g for g in neighbours(src, info.title_id, packages.GAME) if not opened(g)[2]] \
                if opts.ps4_merge and got is None else []
            if got and got[0] == info.title_id:
                done.add(src)
                jobs.append(_ps4_update_job(src, info, fl, home, got))
                continue
            if games:                           # only the update was picked, its game lies next to it: do both
                src, (info, fl, why) = games[0], opened(games[0])
                done.add(src)
                title, size = f"extract PKG: {src.name}", sum(f.size for f in fl)
            else:
                out = base / f"{info.title_id}-patch"
                jobs.append(job(title, src, tasks.extract_ps4(src, out), out, info.kind +
                                (" (its game isn't next to it)" if opts.ps4_merge else ""), len(fl), size))
                continue
        # a game
        ups: list[Path] = []
        if opts.ps4_merge:
            ups = [p for p in picked if infos[p][0] is not None and infos[p][0].kind == packages.UPDATE
                   and infos[p][0].title_id == info.title_id]
            ups += [p for p in neighbours(src, info.title_id, packages.UPDATE) if p not in ups]
            ups.sort(key=lambda p: -opened(p)[0].version)
        use = next((p for p in ups if not opened(p)[2]), None)
        rows: list[Job] = []
        for p in ups:
            i, _f, bad = opened(p)
            done.add(p)
            name = f"  ↳ apply update v{i.version_str}: {p.name}"
            if p == use:
                continue
            rows.append(_skipped(name, p, f"not applied: {bad}" if bad else
                                 f"not applied: v{opened(use)[0].version_str} is newer"))
        got = _extracted_ps4(home)
        if use and got and got[0] == info.title_id and got[1] < opened(use)[0].version:
            jobs.append(_skipped(title, src, f"{home.name} is already there (v{got[2]}): only the update is applied"))
            jobs.append(_ps4_update_job(use, *opened(use)[:2], home, got))
            jobs += rows
            continue
        if use is None:
            jobs.append(job(title, src, tasks.extract_ps4(src, home), home, info.kind, len(fl), size))
            jobs += rows
            continue
        ui, ufl, _ = opened(use)
        sizes = {f.path.lower(): f.size for f in fl} | {f.path.lower(): f.size for f in ufl}
        # what's in this package, and what comes out of the job: the update is the other package, in the next row
        what = f"{info.kind} v{info.version_str}; update v{ui.version_str} (next row) is merged in" + (
            "" if ui.content_id == info.content_id else " (⚠ made for another release of the game)")
        game = _task_job(title, src, tasks.extract_ps4([src, use], home), "Extracting (beta)", [home], [],
                         detail=f"⚠ BETA - {what} → {home.name}/, {len(sizes)} files, {sum(sizes.values()) / 1e9:.2f} GB "
                                f"in total")
        jobs.append(game)
        upd = Job(next(_ids), f"  ↳ apply update v{ui.version_str}: {use.name}", use, [])
        upd.follows, upd.func_label = game, "Applying"
        upd.detail = f"update v{ui.version_str}, {len(ufl)} files - merged in while extracting {src.name}"
        jobs.append(upd)
        jobs += rows
    return jobs


def _ps4_update_job(src: Path, info, files, home: Path, got) -> Job:
    from . import tasks
    title = f"update {home.name} to v{info.version_str}: {src.name}"
    if got[1] >= info.version:
        return _skipped(title, src, f"{home.name} is already at v{got[2]}")
    size = sum(f.size for f in files)
    return _task_job(title, src, tasks.apply_ps4_update(src, home, info.version_str), "Updating (beta)", [home], [],
                     detail=f"⚠ BETA - into the extracted game (v{got[2]}): {len(files)} files, {size / 1e9:.2f} GB → "
                            f"{home.name}/ (what it replaces is kept)")


def _plan_task(preset: str, src: Path, opts: Options) -> Job | None:
    from . import tasks
    out_dir = _out_dir(src, opts)
    if preset == FIX_CHD:
        from .discdetect import check_chd
        from .systems import guess_system
        try:
            ci = chd.info(src)
        except Exception as exc:  # noqa: BLE001
            return _skipped(f"fix type: {src.name}", src, str(exc))
        idx = opts.all_dats() if opts.all_dats else None
        sysdef = guess_system(src.parent.name)
        game = idx.game_named(src.stem) if idx else None
        mc = check_chd(ci.as_dict(), sysdef.id if sysdef else "", game)
        if mc is None:
            return _skipped(f"fix type: {src.name}", src, "can't tell the right type (unknown system, not in a DAT)")
        if mc.ok:
            return _skipped(f"fix type: {src.name}", src, f"already the right type ({mc.actual.upper()})")
        if not mc.fixable:
            return _skipped(f"fix type: {src.name}", src, f"{mc.label}: {mc.fix_note}")
        return _task_job(f"fix type: {src.name}", src,
                         tasks.fix_chd(src, mc.expected, opts.ram, Path(opts.tmp_dir or "/var/tmp/romtools")),
                         f"Converting {mc.actual.upper()} → {mc.expected.upper()}", detail=f"{mc.label} ({mc.reason})")
    if preset in (TRIM, UNTRIM):
        out = out_dir / src.name
        try:
            size, used, full = tasks.trim_info(src)
            detail = f"{size / 2**20:.1f} MiB now, {used / 2**20:.1f} MiB used, cartridge {full / 2**20:.0f} MiB"
        except Exception as exc:  # noqa: BLE001
            return _skipped(f"{preset}: {src.name}", src, str(exc))
        if preset == TRIM:
            return _task_job(f"trim: {src.name}", src, tasks.trim(src, out), "Trimming", [out] if out != src else [],
                             [src], opts, detail)
        idx = opts.all_dats() if opts.all_dats else None
        return _task_job(f"untrim: {src.name}", src, tasks.untrim(src, out, idx), "Untrimming",
                         [out] if out != src else [], [src], opts, detail)
    if preset in (TO_RVZ, FROM_RVZ):
        out = out_dir / (src.stem + (".rvz" if preset == TO_RVZ else ".iso"))
        return _task_job(f"to {out.suffix[1:].upper()}: {src.name}", src, tasks.convert_disc_nod(src, out),
                         f"Converting to {out.suffix[1:].upper()}", [out], [src], opts)
    if preset in (TO_NSZ, FROM_NSZ):
        ext = {".nsp": ".nsz", ".xci": ".xcz", ".nsz": ".nsp", ".xcz": ".xci"}[src.suffix.lower()]
        if preset == TO_NSZ:
            from .packages import NSZ_MULTI_TICKET, ticket_count
            if ticket_count(src) > 1:
                return _skipped(f"to {ext[1:].upper()}: {src.name}", src, f"not compressed: it {NSZ_MULTI_TICKET}")
        out = out_dir / (src.stem + ext)
        keys = Path(opts.switch_keys) if opts.switch_keys else None
        return _task_job(f"to {ext[1:].upper()}: {src.name}", src, tasks.convert_nsz(src, out, keys),
                         "Compressing" if preset == TO_NSZ else "Decompressing", [out], [src], opts)
    return None


def plan_playlists(paths: list[Path], opts: Options) -> list[Job]:
    from . import tasks
    folders = sorted({p if p.is_dir() else p.parent for p in paths})
    jobs, seen = [], set()
    for f in folders:
        for m3u, discs in tasks.find_playlists(f):
            if m3u in seen:
                continue
            seen.add(m3u)
            jobs.append(_task_job(f"playlist: {m3u.name}", discs[0], tasks.write_playlist(m3u, discs),
                                  "Writing playlist", detail=f"{len(discs)} discs: " +
                                  ", ".join(d.name for d in discs)))
    return jobs


def plan_switch_fs(files: list[Path], opts: Options) -> list[Job]:
    """One job per game (its base program NCA, with the newest update found in the same package or next to it
    applied) and one per DLC, whichever of their packages were picked."""
    from . import packages as pk
    from . import tasks
    keys = Path(opts.switch_keys) if opts.switch_keys else None
    if not keys or not keys.is_file():
        return [_skipped(f"extract: {f.name}", f, NEEDS_PROD_KEYS) for f in files]
    tkeys = Path(opts.title_keys) if opts.title_keys and Path(opts.title_keys).is_file() else None

    def missing_title_key(cont: Path, nca: str, tickets: list[str]) -> str:
        """The rights ID whose title key we don't have (no ticket in the package, not in title.keys), or ""."""
        try:
            rid = pk.nca_rights_id(cont, nca, keys)
        except Exception:  # noqa: BLE001
            return ""
        if not rid or f"{rid}.tik" in tickets:
            return ""
        known = pk._Keys(None, tkeys).titles if tkeys else {}
        return "" if rid in known else rid
    pool: set[Path] = set()
    for f in files:
        for d in {f.parent, f.parent.parent}:
            try:
                pool.update(p for p in d.glob("*") if p.suffix.lower() in (".nsp", ".xci"))
                pool.update(p for p in d.glob("*/*") if p.suffix.lower() in (".nsp", ".xci"))
            except OSError:
                pass
    layouts: dict[Path, list] = {}
    for p in sorted(pool | {f for f in files if f.suffix.lower() in (".nsp", ".xci")}):
        try:
            layouts[p] = pk.switch_layout(p, keys)
        except Exception:  # noqa: BLE001
            continue
    games, updates = {}, {}
    for p, ts in layouts.items():
        for t in ts:
            if t.kind == pk.GAME and t.ncas.get(1):
                games.setdefault(t.title_id, (p, t))
            elif t.kind == pk.UPDATE and t.ncas.get(1):
                b = f"{int(t.title_id, 16) & ~0x800:016X}"
                if b not in updates or t.version > updates[b][1].version:
                    updates[b] = (p, t)
    jobs, seen = [], set()
    for f in files:
        if f.suffix.lower() in (".nsz", ".xcz"):
            jobs.append(_skipped(f"extract: {f.name}", f, "compressed: convert it back to NSP / XCI first "
                                                          "(Switch NSZ / XCZ → NSP / XCI)"))
            continue
        if f not in layouts:
            jobs.append(_skipped(f"extract: {f.name}", f, "couldn't read the package's contents (keys?)"))
            continue
        for t in layouts[f]:
            if t.kind == pk.DLC:
                key = ("dlc", t.title_id)
                if key in seen or not t.ncas.get(2):
                    continue
                seen.add(key)
                try:
                    title = pk.clean_title(pk.read(f, keys).title)
                except Exception:  # noqa: BLE001
                    title = f.stem
                out = _out_dir(f, opts) / CIFS_SAFE(f"{title} [{t.title_id}] (DLC)")
                tik = [(f, n) for n in t.tickets]
                rid = missing_title_key(f, t.ncas[2][0], t.tickets)
                if rid:
                    jobs.append(_skipped(f"extract DLC: {f.name}", f, f"{NEEDS_TITLE_KEY} ({rid})"))
                    continue
                jobs.append(_task_job(f"extract DLC: {f.name}", f, tasks.extract_switch_fs(
                    out, keys, (f, t.ncas[2][0]), None, tik, {"romfs": 0}, tkeys), "Extracting", [out], [],
                    detail=f"DLC {t.title_id} v{t.version} → {out.name}/romfs"))
                continue
            base_id = t.title_id if t.kind == pk.GAME else f"{int(t.title_id, 16) & ~0x800:016X}"
            if ("game", base_id) in seen:
                continue
            seen.add(("game", base_id))
            if base_id not in games:
                jobs.append(_skipped(f"extract: {f.name}", f, f"the base game {base_id} wasn't found next to this "
                                                              f"update"))
                continue
            gp, gt = games[base_id]
            up = updates.get(base_id)
            try:
                title = pk.clean_title(pk.read(gp, keys).title)
            except Exception:  # noqa: BLE001
                title = gp.stem
            name = f"{title} [{base_id}]" + (f" (v{up[1].version})" if up else "")
            out = _out_dir(gp, opts) / CIFS_SAFE(name)
            tik = [(gp, n) for n in gt.tickets] + ([(up[0], n) for n in up[1].tickets] if up else [])
            if up:
                main, base = (up[0], up[1].ncas[1][0]), (gp, gt.ncas[1][0])
                detail = (f"base game + update v{up[1].version}" +
                          ("" if up[0] == gp else f" from {up[0].name}") + f" → {out.name}/exefs, romfs")
            else:
                main, base = (gp, gt.ncas[1][0]), None
                detail = f"base game (no update found) → {out.name}/exefs, romfs"
            rid = missing_title_key(gp, gt.ncas[1][0], gt.tickets) or \
                (missing_title_key(up[0], up[1].ncas[1][0], up[1].tickets) if up else "")
            if rid:
                jobs.append(_skipped(f"extract: {gp.name}", gp, f"{NEEDS_TITLE_KEY} ({rid})"))
                continue
            game_job = _task_job(f"extract: {gp.name}", gp, tasks.extract_switch_fs(
                out, keys, main, base, tik, {"exefs": 0, "romfs": 1}, tkeys), "Extracting", [out], [],
                detail=detail)
            jobs.append(game_job)
            if up:   # its own row, for visibility: applied by the game's extraction (nstool needs both at once)
                upd = Job(next(_ids), f"  ↳ apply update v{up[1].version}: {up[0].name}", up[0], [])
                upd.follows, upd.func_label = game_job, "Applying"
                upd.detail = f"update {up[1].title_id} v{up[1].version} - applied while extracting {gp.name}"
                jobs.append(upd)
    return jobs


NEEDS_PROD_KEYS = "needs your Switch prod.keys - set it under Options → Switch keys"
NEEDS_TITLE_KEY = "needs a title key: the package has no ticket and it isn't in your title.keys"


def CIFS_SAFE(name: str) -> str:   # noqa: N802 - folder names that also work on Samba / Windows
    import re as _re
    return _re.sub(r'[\\/:*?"<>|]', "", name).strip(" .")


def plan(preset: str, files: list[Path], opts: Options) -> list[Job]:
    if preset == SWITCH_FS:
        jobs = plan_switch_fs(files, opts)
    elif preset == PS4_EXTRACT:
        jobs = plan_ps4(files, opts)
    else:
        jobs = []
        for src in files:
            try:
                j = _plan_one(preset, src, opts)
            except Exception as exc:  # noqa: BLE001 - one unreadable file mustn't keep the others out of the queue
                j = _skipped(src.name, src, f"couldn't be read: {exc}")
            if j:
                jobs.append(j)
    used = used_options(preset, opts)
    for j in jobs:
        j.preset, j.planned = preset, dict(used)
    return jobs


def used_options(preset: str, opts: Options) -> dict:
    """The options that shape this task's jobs (a changed codec doesn't matter to a PS4 extraction)."""
    names: list[str] = []
    if preset not in (FIX_CHD, VERIFY, INFO, M3U):
        names.append("output_dir")
    if preset in (TO_CHD, RECOMPRESS):
        names += ["overwrite", "cd_codec", "dvd_codec", "custom_codec", "hunk_size", "processors", "verify_after",
                  "trash_sources"]
    if preset == TO_CHD:
        names.append("iso_as")
    if preset == FROM_CHD:
        names += ["overwrite", "split_bin"]
    if preset in (TO_RVZ, FROM_RVZ, TO_NSZ, FROM_NSZ, TRIM, UNTRIM):
        names.append("trash_sources")
    if preset in (TO_NSZ, FROM_NSZ, SWITCH_FS):
        names.append("switch_keys")
    if preset == SWITCH_FS:
        names.append("title_keys")
    if preset == PS4_EXTRACT:
        names.append("ps4_merge")
    out = {n: getattr(opts, n) for n in names}
    if out.get("output_dir"):
        out["output_dir"] = str(Path(out["output_dir"]).expanduser())     # "/x/" and "/x" are the same folder
    return out


def outdated(jobs: list[Job], opts: Options) -> list[Job]:
    """Queued jobs that were planned with other options than the current ones."""
    return [j for j in jobs if j.status == "Queued" and j.preset
            and used_options(j.preset, replace(opts, **j.overrides)) != j.planned]


def replan(jobs: list[Job], opts: Options) -> tuple[list[tuple[Job, Job | None]], list[Job]]:
    """Plan these jobs again with the current options: [(job, its new plan or None)], and rows that now come with one
    of them (the update a Switch extraction applies). The jobs themselves aren't changed yet."""
    groups: dict[tuple, list[Job]] = {}
    for j in jobs:
        groups.setdefault((j.preset, tuple(sorted(j.overrides.items()))), []).append(j)
    res: list[tuple[Job, Job | None]] = []
    extra: list[Job] = []
    for (preset, over), old in groups.items():
        fresh = plan(preset, list(dict.fromkeys(j.source for j in old)), replace(opts, **dict(over)))
        pairs = pair(old, fresh)
        match = {f.id: j for j, f in pairs if f is not None}
        needed = {f.follows.id for f in fresh if f.follows and f.id in match}
        for f in fresh:
            if f.id not in match and (f.id in needed or f.follows and f.follows.id in match):
                extra.append(f)
        for f in fresh:
            if f.follows:
                f.follows = match.get(f.follows.id, f.follows)
        res += pairs
    return res, extra


def reset(job: Job) -> None:
    """Put a finished job back in the queue as if it had just been added."""
    job.status, job.message, job.log, job.log_total = "Queued", "", [], 0
    job.progress = job.step_progress = job.started = job.finished = job.step_started = 0.0
    job.step_index, job.step_label, job.phase, job.weight = 0, "", "", 0


def pair(old: list[Job], fresh: list[Job]) -> list[tuple[Job, Job | None]]:
    """Match queued jobs with newly planned ones for the same task and file."""
    by_src: dict[tuple, list[Job]] = {}
    for f in fresh:
        by_src.setdefault((f.preset, f.source), []).append(f)
    res = []
    for j in old:
        same = by_src.get((j.preset, j.source), [])
        hit = next((f for f in same if f.title == j.title), None)     # one file can hold several jobs (game + DLC)
        if hit is None and len(same) == 1 and j.preset != SWITCH_FS:    # e.g. createcd became createdvd
            hit = same[0]
        res.append((j, hit))
    return res


def merge_plan(queue: list[Job], fresh: list[Job]) -> tuple[list[Job], list[Job]]:
    """Files added again while still waiting for the same task: their jobs take the new plan instead of being queued
    twice. Returns (jobs that are new to the queue, queued jobs that were updated)."""
    waiting = [j for j in queue if j.status == "Queued" and j.preset]
    match = {f.id: j for f, j in pair(fresh, waiting) if j is not None}
    for f in fresh:
        if f.follows:
            f.follows = match.get(f.follows.id, f.follows)
    for f in fresh:
        if f.id in match:
            take_plan(match[f.id], f)
    return [f for f in fresh if f.id not in match], list(match.values())


def take_plan(job: Job, fresh: Job | None) -> None:
    """Give a queued job its new plan (it keeps its id, its place in the queue and the row it mirrors)."""
    if fresh is None:
        job.status, job.message = "Skipped", "it can't be done with the current options - add the file again"
        return
    for n in ("title", "steps", "outputs", "sources", "func", "func_label", "detail", "trash", "status", "message",
              "planned", "log", "log_total", "follows"):
        setattr(job, n, getattr(fresh, n))


def _plan_one(preset: str, src: Path, opts: Options) -> Job | None:
    if preset in (FIX_CHD, TRIM, UNTRIM, TO_RVZ, FROM_RVZ, TO_NSZ, FROM_NSZ):
        return _plan_task(preset, src, opts)
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
        _log_line(j, f"Detected: {note}")
        j.trash = opts.trash_sources and opts.verify_after  # only ever after a verified output
        if j.trash and not all(lies_inside(p, src.parent) for p in parts):
            j.trash = False
            _log_line(j, "The sources won't be moved to the trash: the sheet names tracks outside its own folder.")
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

LOG_KEEP = 20000        # lines kept per job (nstool prints one per extracted file - tens of thousands)


def _log_line(job: Job, s: str) -> None:
    """Add a line to a job's log without flooding the window: no update per line, and only the newest
    LOG_KEEP lines are kept."""
    job.log.append(s)
    job.log_total += 1
    if len(job.log) > LOG_KEEP + 2000:
        del job.log[:len(job.log) - LOG_KEEP]


def save_log(job: Job) -> Path | None:
    """Keep a failed job's full log in <data>/logs/ (the window only holds it while it's open)."""
    import re as _re
    from .config import DATA_DIR
    try:
        d = DATA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        name = _re.sub(r"[^\w.-]+", "_", job.title)[:80]
        f = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.log"
        f.write_text(f"{job.title}\nsource: {job.source}\nstatus: {job.status}: {job.message}\n"
                     f"detail: {job.detail}\n\n" + "\n".join(job.log) + "\n", encoding="utf-8")
        _log_line(job, f"(log saved to {f})")
        return f
    except OSError:
        return None


class Runner:
    def __init__(self, jobs: list[Job], parallel: int, on_update: Callable[[Job], None],
                 trash: Callable[[Path], bool] | None = None) -> None:
        self.jobs = jobs
        self.parallel = max(1, parallel)
        self._report = on_update
        self.on_update = self._notify
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
            list(ex.map(self._run_and_keep_log,
                        [j for j in self.jobs if j.status == "Queued" and j.follows is None]))

    def _run_and_keep_log(self, job: Job) -> None:
        try:
            self._run_job(job)
        except Exception as exc:  # noqa: BLE001 - one job's problem mustn't stop the others
            job.status, job.message = "Failed", str(exc) or exc.__class__.__name__
            job.finished, job.phase = time.time(), ""
            _log_line(job, f"Failed: {exc}")
        finally:
            self._notify(job)          # final state onto any rows that follow this job
        if job.status == "Failed":
            save_log(job)

    def _notify(self, job: Job) -> None:
        """Report a job's change, and copy it onto the rows that only mirror it (e.g. the update a Switch
        extraction applies, shown as its own row)."""
        self._report(job)
        for f in self.jobs:
            if f.follows is job:
                f.status = job.status
                f.progress, f.step_progress, f.phase = job.progress, job.step_progress, job.phase
                f.step_label = "applied during the game's extraction"
                f.message = {"Done": "applied", "Failed": "not applied: the extraction failed",
                             "Cancelled": "stopped", "Skipped": "not applied: " + job.message}.get(job.status, "")
                self._report(f)

    def _run_job(self, job: Job) -> None:
        if self.cancel.is_set():
            job.status = "Cancelled"
            self.on_update(job)
            return
        if job.func is not None:
            return self._run_func(job)
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
        # planned jobs write into a work folder next to the output; the files only get their real names once every step
        # (also verify) has succeeded. So nothing half-written or wrong is ever left under a real name, and with
        # Overwrite the old file stays until the new one is ready.
        work = None
        steps = job.steps
        if job.preset and job.outputs and steps[0][1] not in ("verify", "info"):
            work = job.outputs[0].parent / f".romtools-job-{uuid.uuid4().hex[:8]}"
            work.mkdir()
            staged = {str(o): str(work / o.name) for o in job.outputs}
            steps = [[staged.get(a, a) for a in argv] for argv in steps]
        try:
            self._run_steps(job, steps, existing, work, main_has_force)
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
        job.finished = time.time()
        job.phase = ""
        self.on_update(job)

    def _run_steps(self, job: Job, steps: list[list[str]], existing: list[Path], work: Path | None,
                   overwrite: bool) -> None:
        n = len(steps)
        for i, argv in enumerate(steps):
            job.step_index = i
            job.step_label = f"Step {i + 1}/{n} · {job.step_name}"
            job.step_progress, job.step_started, job.phase = 0.0, time.time(), "starting chdman…"
            _log_line(job, f"── {job.step_label} ──")

            def prog(f, i=i):
                job.step_progress = f
                job.progress = (i + f) / n
                self.on_update(job)

            def status(s):
                job.phase = pretty_phase(s)

            def line(s):
                _log_line(job, s)          # the window picks new lines up on its twice-a-second refresh
                if s.startswith("Error") or "error" in s.lower():
                    job.message = s

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
                if work is None:
                    self._cleanup_partial(job, existing)
                break
            if rc != 0:
                job.status = "Failed"
                job.message = job.message or f"{argv[1]} exited with code {rc}"
                if argv[1] == "verify" and n > 1:
                    job.message = f"the new file failed its check, so it wasn't kept: {job.message}"
                if work is None and i == 0:
                    self._cleanup_partial(job, existing)
                break
        else:
            if work is not None:
                _put_in_place(work, job.outputs[0].parent, overwrite)
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

    def _run_func(self, job: Job) -> None:
        """Run a Python task (see romtools/tasks.py) with the same progress / log / stop handling."""
        from .tasks import TaskContext, TaskSkipped
        job.status, job.started = "Running", time.time()
        job.step_label = f"Step 1/1 · {job.step_name}"
        job.step_started, job.phase = time.time(), ""
        _log_line(job, f"── {job.step_name}: {job.source} ──")
        self.on_update(job)

        def progress(f: float, phase: str = ""):
            job.step_progress = job.progress = max(0.0, min(1.0, f))
            if phase:
                job.phase = phase
            self.on_update(job)

        def log(s: str):
            _log_line(job, s)

        ctx = TaskContext(log, progress, self.cancel, self.trash, job.trash)
        try:
            job.message = job.func(ctx) or "done"
            job.status, job.progress = "Done", 1.0
        except TaskSkipped as s:
            job.status, job.message = "Skipped", str(s)
        except Exception as exc:  # noqa: BLE001
            cancelled = self.cancel.is_set()
            job.status = "Cancelled" if cancelled else "Failed"
            job.message = "stopped by user" if cancelled else str(exc)
            log(f"{'Cancelled' if cancelled else 'Failed'}: {exc}")
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


def _put_in_place(work: Path, folder: Path, overwrite: bool) -> None:
    """Give the finished files in ``work`` their real places. All or nothing: files they replace are kept aside until
    everything is in place, and put back if something goes wrong."""
    new = sorted(f for f in work.iterdir() if f.is_file())
    if not new:
        raise RuntimeError("no output was written")
    taken = [f.name for f in new if (folder / f.name).exists()]
    if taken and not overwrite:
        raise RuntimeError(f"{taken[0]} already exists (tick Overwrite to replace)")
    old = work / "old"
    if taken:
        old.mkdir()
    done: list[str] = []
    try:
        for f in new:
            if f.name in taken:
                os.rename(folder / f.name, old / f.name)
            done.append(f.name)
            os.rename(f, folder / f.name)
    except OSError:
        for name in done:
            try:
                if not (work / name).exists():
                    os.rename(folder / name, work / name)
                if name in taken:
                    os.rename(old / name, folder / name)
            except OSError:
                continue
        # the old files must survive even if they couldn't be put back
        left = list(old.iterdir()) if old.is_dir() else []
        if left:
            keep = folder / f"{left[0].stem} (replaced files)"
            os.rename(old, keep)
            raise RuntimeError(f"the new files couldn't be put in place; the old ones are in “{keep.name}”")
        raise


def _gio_trash(path: Path) -> bool:
    from .compat import trash
    return trash(path)


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
