"""Walk a system folder, hash each game item, and match it against the DAT index.

Everything here is read-only with respect to the library.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import chd
from .cache import HashCache
from .compat import ram_mode_supported
from .dat import DatIndex, Game
from .hashing import ARCHIVE_EXTS, Cancelled, Entry, hash_archive, hash_file

IGNORE_EXTS = {".m3u", ".txt", ".nfo", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".ds_store", ".db", ".ini",
               ".xml", ".json", ".sav", ".srm", ".state", ".keys", ".pdf", ".url", ".lnk", ".sfv", ".md5",
               ".sha1", ".log", ".part", ".tmp"}
ROM_EXTS = {".nes", ".fds", ".unf", ".sfc", ".smc", ".fig", ".swc", ".bs", ".st", ".n64", ".z64", ".v64", ".gb",
            ".gbc", ".gba", ".nds", ".dsi", ".3ds", ".cia", ".cci", ".vb", ".min", ".md", ".gen", ".smd", ".bin",
            ".sms", ".gg", ".32x", ".sg", ".pce", ".sgx", ".a26", ".a52", ".a78", ".lnx", ".lyx", ".j64", ".ngp",
            ".ngc", ".ws", ".wsc", ".rom", ".mx1", ".mx2", ".iso", ".chd", ".cue", ".gdi", ".ccd", ".img", ".cso",
            ".zso", ".rvz", ".gcm", ".gcz", ".wbfs", ".wua", ".wux", ".wud", ".nsp", ".nsz", ".xci", ".pkg",
            ".zip", ".7z", ".rar", ".pbp", ".elf"}

VERIFIED, MISNAMED, MODIFIED, PARTIAL, BAD, UNKNOWN, NODAT, ERROR, SKIPPED = (
    "Verified", "Misnamed", "Modified", "Partial", "Bad dump", "Unknown", "No DAT", "Error", "Skipped")
STATUS_ORDER = [VERIFIED, MISNAMED, MODIFIED, PARTIAL, BAD, UNKNOWN, NODAT, ERROR, SKIPPED]
TAGGABLE = {BAD, UNKNOWN, PARTIAL}   # statuses a hack/translation tag can replace


@dataclass
class Item:
    system: str
    path: Path                 # main file (or folder)
    kind: str                  # file | archive | chd | cueset | folder
    parts: list[Path] = field(default_factory=list)  # extra files for cue/gdi sets

    @property
    def stem(self) -> str:
        return self.path.name if self.kind == "folder" else self.path.stem


@dataclass
class EntryResult:
    entry: Entry
    rom: str | None = None     # matched DAT rom name
    headerless: bool = False


@dataclass
class Result:
    item: Item
    status: str
    game: str = ""             # matched (or name-matched) DAT game
    dat: str = ""
    note: str = ""
    entries: list[EntryResult] = field(default_factory=list)
    chd_info: dict = field(default_factory=dict)
    cached: bool = False
    media: object = None       # discdetect.MediaCheck for CHDs (None if unknown / not a CHD)
    tag: str = ""              # hack / translation tag (see tags.py)
    tag_source: str = ""       # "marked by you" or "from name: …"
    orig_status: str | None = None   # status before tagging
    orig_note: str = ""

    @property
    def media_wrong(self) -> bool:
        return self.media is not None and not self.media.ok

    @property
    def size(self) -> int:
        try:
            if self.item.kind == "folder":
                return 0
            return self.item.path.stat().st_size + sum(p.stat().st_size for p in self.item.parts)
        except OSError:
            return 0

    @property
    def expected_name(self) -> str | None:
        """File name the DAT says this item should have (same extension), or None."""
        if self.status != MISNAMED or not self.game:
            return None
        return self.game + self.item.path.suffix


# ------------------------------------------------------------ enumeration

_CUE_FILE = re.compile(r'^\s*FILE\s+"?(.+?)"?\s+\S+\s*$', re.I | re.M)


def _cue_parts(cue: Path) -> list[Path]:
    try:
        text = cue.read_text(errors="replace")
    except OSError:
        return []
    return [cue.parent / m for m in _CUE_FILE.findall(text)]


def _gdi_parts(gdi: Path) -> list[Path]:
    parts = []
    try:
        lines = gdi.read_text(errors="replace").splitlines()[1:]
    except OSError:
        return []
    for line in lines:
        m = re.match(r'\s*\d+\s+\d+\s+\d+\s+\d+\s+("([^"]+)"|(\S+))', line)
        if m:
            parts.append(gdi.parent / (m.group(2) or m.group(3)))
    return parts


MAX_DEPTH = 3   # system/game/disc-style nesting


def _items_in(system: str, folder: Path, depth: int = 0, skip_dirs: set[str] | frozenset = frozenset()) -> list[Item]:
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    files = [p for p in children if p.is_file() and not p.name.startswith(".")]
    dirs = [p for p in children if p.is_dir() and not p.name.startswith(".") and p.name not in skip_dirs]
    consumed: set[Path] = set()
    items: list[Item] = []
    for f in files:
        ext = f.suffix.lower()
        if ext == ".cue":
            parts = [p for p in _cue_parts(f) if p.exists()]
        elif ext == ".gdi":
            parts = [p for p in _gdi_parts(f) if p.exists()]
        else:
            continue
        consumed.update(parts)
        items.append(Item(system, f, "cueset", parts))
    for f in files:
        ext = f.suffix.lower()
        if f in consumed or ext in (".cue", ".gdi") or ext in IGNORE_EXTS:
            continue
        kind = "chd" if ext == ".chd" else "archive" if ext in ARCHIVE_EXTS else "file"
        items.append(Item(system, f, kind))
    for d in dirs:
        if depth < MAX_DEPTH and (_has_roms(d) or _has_rom_subdirs(d)):
            items.extend(_items_in(system, d, depth + 1))
        else:
            items.append(Item(system, d, "folder"))
    return items


def _has_roms(d: Path) -> bool:
    try:
        return any(p.is_file() and p.suffix.lower() in ROM_EXTS and p.suffix.lower() not in (".bin", ".elf")
                   for p in d.iterdir()) or any(p.suffix.lower() == ".cue" for p in d.iterdir())
    except OSError:
        return False


def _has_rom_subdirs(d: Path) -> bool:
    try:
        return any(p.is_dir() and not p.name.startswith(".") and _has_roms(p) for p in d.iterdir())
    except OSError:
        return False


def enumerate_system(system: str, folder: Path) -> list[Item]:
    return _items_in(system, folder)


# ------------------------------------------------------------ matching

def match(item: Item, entries: list[Entry], index: DatIndex) -> Result:
    data_entries = [e for e in entries if not e.name.lower().endswith((".cue", ".gdi"))]
    per_entry: list[tuple[Entry, list[tuple[Game, object, bool]]]] = []
    for e in data_entries:
        hits = []
        for v, is_nohdr in ((e.full, False), (e.headerless, True)):
            if v is None:
                continue
            for g, r in index.lookup(v.size, v.crc, v.md5, v.sha1):
                hits.append((g, r, is_nohdr))
        per_entry.append((e, hits))

    # Score candidate games by how many distinct roms of theirs we hold.
    scores: dict[int, tuple[Game, set[int]]] = {}
    for _e, hits in per_entry:
        for g, r, _h in hits:
            scores.setdefault(id(g), (g, set()))[1].add(id(r))

    stem = item.stem
    if not scores:
        g = index.game_named(stem)
        ers = [EntryResult(e) for e in data_entries]
        if g:
            return Result(item, BAD, g.name, g.dat, "Name matches the DAT but the data does not "
                          "(bad dump, hack, trimmed or modified file)", ers)
        return Result(item, UNKNOWN, note="No DAT entry has these hashes", entries=ers)

    games_hit = {id(g) for _e, hits in per_entry for g, _r, _h in hits}
    if item.kind == "archive" and len(per_entry) > 1 and len(games_hit) > 1 and all(h for _e, h in per_entry):
        ers = [EntryResult(e, hits[0][1].name, hits[0][2]) for e, hits in per_entry]
        return Result(item, VERIFIED, ", ".join(sorted({h[0][0].name for _e, h in per_entry})),
                      per_entry[0][1][0][0].dat, "Multi-game archive", ers)

    def rank(t):
        g, roms = t
        return (len(roms), g.name.lower() == stem.lower(), -len(g.data_roms))

    best, held = max(scores.values(), key=rank)
    ers = []
    unmatched = 0
    for e, hits in per_entry:
        mine = [(r, h) for g, r, h in hits if g is best]
        if mine:
            ers.append(EntryResult(e, mine[0][0].name, mine[0][1]))
        else:
            ers.append(EntryResult(e))
            unmatched += 1
    required = best.data_roms
    have_all = all(id(r) in held for r in required)
    if have_all and unmatched == 0:
        status = VERIFIED if best.name == stem else MISNAMED
        note = "" if status == VERIFIED else f"Should be named “{best.name}”"
        return Result(item, status, best.name, best.dat, note, ers)
    got = sum(1 for r in required if id(r) in held)
    note = f"{got}/{len(required)} DAT tracks match"
    if unmatched:
        note += f", {unmatched} file track(s) not in DAT entry"
    bad = [er for er in ers if er.rom is None]
    if bad and all(er.entry.name.lower().endswith(".raw") for er in bad):
        note += " - only audio tracks differ from Redump (the game data is intact)"
    return Result(item, PARTIAL, best.name, best.dat, note, ers)


# ------------------------------------------------------------ scanning

ProgressCb = Callable[[str, float], None]

# Scan modes
QUICK = "quick"            # reuse remembered hashes, hash only new/changed files
FULL = "full"              # re-hash everything, refresh what is remembered
REMEMBERED = "remembered"  # show remembered results only, hash nothing


class NotScanned(Exception):
    """REMEMBERED mode: this file has never been hashed (or changed since)."""


class Scanner:
    def __init__(self, tmp_dir: Path, workers: int = 4, chd_workers: int = 2,
                 cache: HashCache | None = None, ram_mode: bool = False) -> None:
        self.tmp_dir = tmp_dir
        self.ram_mode = ram_mode
        self.workers = max(1, workers)
        self.chd_sem = threading.Semaphore(max(1, chd_workers))
        self.cache = cache
        self.cancel = threading.Event()
        self.mode = QUICK
        self.marks = None          # tags.MarkStore; set by the caller to apply your hack marks
        self.stats = {"remembered": 0, "hashed": 0, "not_scanned": 0}
        self._stats_lock = threading.Lock()

    def _count(self, key: str) -> None:
        with self._stats_lock:
            self.stats[key] += 1

    # -- hashing one item (with cache) --------------------------------
    def hash_item(self, item: Item, progress: ProgressCb | None = None) -> tuple[list[Entry], dict, bool]:
        p = item.path
        st = p.stat()
        mtime = st.st_mtime + sum(x.stat().st_mtime for x in item.parts)
        size = st.st_size + sum(x.stat().st_size for x in item.parts)
        if self.cache and self.mode != FULL:
            hit = self.cache.get(p, size, mtime)
            if hit and item.kind == "chd" and hit[1].get("kind") == "gd" \
                    and hit[1].get("layout", 1) < chd.TRACK_LAYOUT:
                hit = None  # GD-ROM hashed with the old .gdi layout: redo it with the Redump-exact one
            if hit:
                kind, extra, entries = hit
                if item.kind == "chd" and "frames" not in extra:
                    # remembered before track details were stored: read the CHD header (fast) and remember it
                    try:
                        extra = chd.info(p).as_dict()
                        self.cache.put(p, size, mtime, kind, extra, entries)
                    except Exception:  # noqa: BLE001
                        pass
                self._count("remembered")
                return entries, extra, True
        if self.mode == REMEMBERED:
            raise NotScanned()

        done = 0

        def bytes_cb(n):
            nonlocal done
            done += n
            if progress:
                progress("hashing", done / max(size, 1))

        extra: dict = {}
        if item.kind == "chd":
            with self.chd_sem:
                if self.cancel.is_set():
                    raise Cancelled()
                hasher = chd.stream_hash if self.ram_mode else chd.extract_and_hash
                root = chd.RAM_DIR if self.ram_mode else self.tmp_dir
                ci, entries = hasher(p, root, self.cancel, lambda stage, f: progress and progress(stage, f))
            extra = ci.as_dict()
        elif item.kind == "archive":
            entries = hash_archive(p, self.cancel, bytes_cb)
        elif item.kind == "cueset":
            entries = [hash_file(p, self.cancel)] + [hash_file(x, self.cancel, bytes_cb) for x in item.parts]
        else:
            entries = [hash_file(p, self.cancel, bytes_cb)]
        if self.cache:
            self.cache.put(p, size, mtime, item.kind, extra, entries)
        self._count("hashed")
        return entries, extra, False

    def check(self, item: Item, index: DatIndex | None, progress: ProgressCb | None = None) -> Result | None:
        if item.kind == "folder":
            return Result(item, SKIPPED, note="Folder (extracted game / installed title) - not checked")
        if index is None:
            from .systems import UNVERIFIABLE_HINT
            return Result(item, NODAT, note=UNVERIFIABLE_HINT.get(item.system, "No DAT loaded for this system"))
        try:
            entries, extra, cached = self.hash_item(item, progress)
        except Cancelled:
            raise
        except NotScanned:
            self._count("not_scanned")
            return None
        except Exception as exc:  # noqa: BLE001 - report every failure as a row
            return Result(item, ERROR, note=str(exc) or exc.__class__.__name__)
        r = match(item, entries, index)
        r.chd_info = extra
        r.cached = cached
        if item.system == "(other)":
            # identified across several DATs: show (and use) the system the matching DAT belongs to
            from .library import system_for_dat
            detected = system_for_dat(r.dat) if r.dat else None
            item.system = detected or "(other)"
        if item.kind == "chd":
            from .discdetect import check_chd
            from .systems import guess_system
            sysdef = guess_system(item.system)
            game = index.game_named(r.game) if r.game else index.game_named(item.stem)
            r.media = check_chd(extra, sysdef.id if sysdef else "", game)
        from . import tags
        tags.apply(r, self.marks, MODIFIED, TAGGABLE)
        return r

    def run(self, items: list[Item], indexes: dict[str, DatIndex | None],
            on_result: Callable[[Result], None],
            on_progress: Callable[[int, int, str, float], None] | None = None) -> None:
        total = len(items)
        done = 0
        lock = threading.Lock()

        def job(it: Item):
            nonlocal done
            if self.cancel.is_set():
                return
            prog = (lambda stage, f: on_progress(done, total, f"{stage}: {it.path.name}", f)) if on_progress else None
            if on_progress:
                on_progress(done, total, it.path.name, 0.0)
            try:
                res = self.check(it, indexes.get(it.system), prog)
            except Cancelled:
                return
            with lock:
                done += 1
            if res is not None:
                on_result(res)
            if on_progress:
                on_progress(done, total, it.path.name, 1.0)

        # Small files first so the table fills quickly; CHDs are throttled by the semaphore.
        order = sorted(items, key=lambda it: (it.kind == "chd", _safe_size(it.path)))
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for f in [ex.submit(job, it) for it in order]:
                exc = f.exception()
                if exc and not isinstance(exc, Cancelled):
                    raise exc


def make_scanner(cfg, cache=None, mode: str = QUICK) -> "Scanner":
    """Build a Scanner from the settings as they are right now (a running scan keeps its own)."""
    s = Scanner(Path(cfg["tmp_dir"]), cfg["workers"], cfg["chd_workers"], cache,
                ram_mode=cfg["temp_mode"] == "ram" and ram_mode_supported())
    s.mode = mode
    return s


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0
