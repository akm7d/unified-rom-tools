"""Walk a system folder, hash each game item, and match it against the DAT index.

Everything here is read-only with respect to the library.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import chd, identify, nod, packages, ps4pkg, textfile
from .cache import HashCache
from .compat import ram_mode_supported
from .dat import DatIndex, Game
from .hashing import ARCHIVE_EXTS, HASH_VERSION, Cancelled, Entry, hash_archive, hash_file

IGNORE_EXTS = {".m3u", ".txt", ".nfo", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".ds_store", ".db", ".ini",
               ".xml", ".json", ".sav", ".srm", ".state", ".keys", ".pdf", ".url", ".lnk", ".sfv", ".md5",
               ".sha1", ".log", ".part", ".tmp"}
ROM_EXTS = {".nes", ".fds", ".unf", ".sfc", ".smc", ".fig", ".swc", ".bs", ".st", ".n64", ".z64", ".v64", ".gb",
            ".gbc", ".gba", ".nds", ".dsi", ".3ds", ".cia", ".cci", ".vb", ".min", ".md", ".gen", ".smd", ".bin",
            ".sms", ".gg", ".32x", ".sg", ".pce", ".sgx", ".a26", ".a52", ".a78", ".lnx", ".lyx", ".j64", ".ngp",
            ".ngc", ".ws", ".wsc", ".rom", ".mx1", ".mx2", ".iso", ".chd", ".cue", ".gdi", ".ccd", ".img", ".cso",
            ".zso", ".rvz", ".ciso", ".wia", ".gcm", ".gcz", ".wbfs", ".wua", ".wux", ".wud", ".nsp", ".nsz", ".xci", ".pkg",
            ".zip", ".7z", ".rar", ".pbp", ".elf"}

VERIFIED, MISNAMED, MODIFIED, PARTIAL, BAD, UNKNOWN, NODAT, ERROR, SKIPPED = (
    "Verified", "Misnamed", "Modified", "Partial", "Bad dump", "Unknown", "No DAT", "Error", "Skipped")
# console packages (PS4 PKG, Switch NSP/XCI) have no DAT: they are identified from their own metadata
IDENTIFIED, ATTENTION = "Identified", "Check"
MISPLACED = "Wrong folder"      # a good dump, but in another system's folder
STATUS_ORDER = [VERIFIED, MISNAMED, MISPLACED, MODIFIED, IDENTIFIED, PARTIAL, ATTENTION, BAD, UNKNOWN, NODAT, ERROR, SKIPPED]
TAGGABLE = {BAD, UNKNOWN, PARTIAL}   # statuses a hack/translation tag can replace


@dataclass
class Item:
    system: str
    path: Path                 # main file (or folder)
    kind: str                  # file | archive | chd | disc (RVZ/CISO/... read by nodtool) | package (PS4/Switch)
                               # | cueset | folder
    parts: list[Path] = field(default_factory=list)  # extra files for cue/gdi sets
    member: str = ""           # one game inside a multi-game archive (its file name inside the archive)
    stamp: tuple | None = None # (size, time) of its files when they were checked
    maybe: bool = False        # only shown if a DAT knows it (no extension or signature says it's a ROM)
    keep: str = ""             # "bios" / "other platform": in such a folder, so neither renamed nor sorted
    expected: str = ""         # catalogue id of the nearest folder named like a system ("" = none)

    @property
    def stem(self) -> str:
        if self.member:
            return Path(self.member).stem
        return self.path.name if self.kind == "folder" else self.path.stem

    @property
    def label(self) -> str:
        """What the user sees: "file.zip" or "file.zip › Game (USA).gb" for one game inside an archive."""
        return f"{self.path.name} › {Path(self.member).name}" if self.member else self.path.name


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
    found: str = ""            # catalogue id of the system the data itself says (by DAT hash, else by its header)
    found_by: str = ""         # "hash" | "header" | "package"
    ident: object = None       # identify.Ident of the file's own header, if it has one
    misplaced: bool = False    # found and item.expected don't agree
    orig_status: str | None = None   # status before tagging
    orig_note: str = ""

    @property
    def media_wrong(self) -> bool:
        return self.media is not None and not self.media.ok

    @property
    def size(self) -> int:
        if self.item.member:   # one game inside an archive: its own (uncompressed) size
            return sum(er.entry.full.size for er in self.entries)
        try:
            if self.item.kind == "folder":
                return 0
            return self.item.path.stat().st_size + sum(p.stat().st_size for p in self.item.parts)
        except OSError:
            return 0

    @property
    def expected_name(self) -> str | None:
        """File name the DAT says this item should have (same extension), or None."""
        if self.status not in (MISNAMED, MISPLACED) or not self.game or self.game == self.item.stem:
            return None
        return self.game + self.item.path.suffix


# ------------------------------------------------------------ enumeration

_CUE_FILE = re.compile(r'^\s*FILE\s+"?(.+?)"?\s+\S+\s*$', re.I | re.M)


def _cue_parts(cue: Path) -> list[Path]:
    try:
        text = textfile.read_text(cue)
    except OSError:
        return []
    return [cue.parent / m for m in _CUE_FILE.findall(text)]


def _gdi_parts(gdi: Path) -> list[Path]:
    parts = []
    try:
        lines = textfile.read_text(gdi).splitlines()[1:]
    except OSError:
        return []
    for line in lines:
        m = re.match(r'\s*\d+\s+\d+\s+\d+\s+\d+\s+("([^"]+)"|(\S+))', line)
        if m:
            parts.append(gdi.parent / (m.group(2) or m.group(3)))
    return parts


MAX_DEPTH = 12      # folders inside folders (symlink loops end here too)
OTHER = "(other)"    # group key for loose files / folders that aren't a system's (see library.py)
# extensions that other kinds of files use as well: outside a system folder they need a ROM's signature to count
WEAK_EXTS = {".bin", ".elf", ".md", ".rom", ".img", ".st", ".bs", ".min", ".gen", ".sg", ".pkg", ".unf", ".swc",
             ".fig"}
STRONG_EXTS = ROM_EXTS - WEAK_EXTS
NEVER_ROMS = {".m3u", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".sav", ".srm", ".state", ".keys", ".url",
              ".lnk", ".mp3", ".mp4", ".mkv", ".ogg", ".wav", ".flac"}
MEDIA_DIRS = {"images", "media", "videos", "manuals", "screenshots", "covers", "boxart", "box art", "snaps",
              "node_modules", "__macosx", "$recycle.bin", "system volume information"}
_TRACK = re.compile(r"^(.*?)\s*\(Track \d+\)", re.I)


def _maybe_rom(f: Path) -> bool:
    """Generic or no extension, no signature, but a size old cartridges come in: only its hash can tell."""
    ext, n = f.suffix.lower(), _safe_size(f)
    return (ext in WEAK_EXTS - {".md", ".pkg", ".elf"} or not ext) and 2048 <= n <= 8 << 20 and n % 1024 in (0, 512)


def _is_rom(f: Path, in_system: bool) -> bool:
    """Should this file be checked? Known ROM extensions always; in a system folder everything that isn't a picture,
    text or save; anything else when its content says it's a ROM (whatever it is called)."""
    ext = f.suffix.lower()
    if ext in STRONG_EXTS:
        return True
    if ext in NEVER_ROMS or f.name.startswith("."):
        return False
    if in_system and ext not in IGNORE_EXTS:
        return True
    size = _safe_size(f)
    if size < (8192 if ext in IGNORE_EXTS else 1024):
        return False
    hit = identify.identify(f)
    if hit and (hit.system or hit.container in ("chd", "disc", "package")
                or (hit.container == "archive" and (not ext or ext in ROM_EXTS))
                or (hit.ext == ".bin" and ext in (".bin", ".img"))):
        return True
    return False


def kind_for(ext: str) -> str:
    return ("chd" if ext == ".chd" else "disc" if ext in nod.DISC_EXTS else "package" if ext in packages.PACKAGE_EXTS
            else "archive" if ext in ARCHIVE_EXTS else "file")


def _entries(folder: Path) -> tuple[list[Path], list[Path]]:
    """(files, sub-folders), sorted, without hidden ones. One directory read - no extra request per file, which is what
    takes the time on a network share."""
    import os
    files, dirs = [], []
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    (dirs if e.is_dir() else files).append(Path(e.path))
                except OSError:
                    continue
    except OSError:
        return [], []
    return sorted(files, key=lambda p: p.name.lower()), sorted(dirs, key=lambda p: p.name.lower())


def _leads_back(d: Path, followed: frozenset[str]) -> str | None:
    """Where a linked folder really is - None if following it would lead in circles."""
    import os
    real, here = os.path.realpath(d), os.path.realpath(d.parent)
    if real in followed or here == real or here.startswith(real.rstrip(os.sep) + os.sep):
        return None
    return real


def _items_in(system: str, folder: Path, depth: int = 0, skip_dirs: set[str] | frozenset = frozenset(),
              expected: str = "", keep: str = "", followed: frozenset[str] = frozenset()) -> list[Item]:
    from .systems import left_alone
    if not depth:
        keep = keep or left_alone(folder.name)
    files, dirs = _entries(folder)
    dirs = [p for p in dirs if p.name not in skip_dirs]
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
        items.append(Item(system, f, "cueset", parts, expected=expected, keep=keep))
    # the tracks of one disc without their .cue
    tracks: dict[str, list[Path]] = {}
    for f in files:
        m = _TRACK.match(f.stem)
        if m and f not in consumed and f.suffix.lower() in (".bin", ".raw", ".img"):
            tracks.setdefault(m[1].lower(), []).append(f)
    for group in tracks.values():
        if len(group) > 1:
            consumed.update(group)
            items.append(Item(system, group[0], "cueset", group[1:], expected=expected, keep=keep))
    for f in files:
        ext = f.suffix.lower()
        if f in consumed or ext in (".cue", ".gdi"):
            continue
        if ext == ".pkg":                   # a split package is one item
            split = ps4pkg.pieces(f)
            if split:
                if f == split[0]:
                    items.append(Item(system, f, "package", split[1:], expected=expected))
                continue
        sure = _is_rom(f, system != OTHER)
        if sure or ext not in NEVER_ROMS and _maybe_rom(f):
            items.append(Item(system, f, kind_for(ext), expected=expected, maybe=not sure, keep=keep))
    for d in dirs:
        here = _system_of_folder(d.name) or expected
        skip, links = d.name.lower() in MEDIA_DIRS or depth >= MAX_DEPTH, followed
        if d.is_symlink():                      # linked folders count too, but not round in circles
            real = _leads_back(d, followed)
            skip, links = skip or real is None, followed | {real or ""}
        inside = None if skip else _entries(d)
        game = identify.extracted_game(d, {p.name.lower() for x in inside for p in x} if inside else None)
        # an extracted Switch / Wii U game: identified, not "Skipped"
        if game in ("switch", "wiiu") and packages.is_extracted_package(d):
            items.append(Item(system, d, "package", expected=here))
        elif game is None and inside and holds_roms(d, inside):
            items.extend(_items_in(system, d, depth + 1, expected=here, keep=keep or left_alone(d.name), followed=links))
        else:
            items.append(Item(system, d, "folder", expected=here))
    return items


def _system_of_folder(name: str) -> str:
    from .systems import guess_system
    g = guess_system(name)
    return g.id if g else ""


def holds_roms(d: Path, inside: tuple[list[Path], list[Path]] | None = None, budget: int = 4000,
               looks: int = 24) -> bool:
    """Is there a ROM anywhere under this folder? Stops at the first one, and gives up on huge trees of other files."""
    from collections import deque
    todo: "deque[tuple[Path, int, frozenset]]" = deque([(d, 0, frozenset())])
    while todo and budget > 0:
        cur, depth, followed = todo.popleft()
        files, dirs = inside if inside is not None and cur is d else _entries(cur)
        if depth:
            game = identify.extracted_game(cur, {p.name.lower() for x in (files, dirs) for p in x})
            if game is not None:
                if game in ("switch", "wiiu") and packages.is_extracted_package(cur):
                    return True
                continue
        budget -= len(files) + len(dirs)
        if any(p.suffix.lower() in STRONG_EXTS for p in files):
            return True
        for p in files:
            if looks <= 0:
                break
            if p.suffix.lower() not in NEVER_ROMS:
                looks -= 1
                if _is_rom(p, False) or _maybe_rom(p):           # by content
                    return True
        if depth < MAX_DEPTH:
            for p in dirs:
                if p.name.lower() in MEDIA_DIRS:
                    continue
                real = _leads_back(p, followed) if p.is_symlink() else ""
                if real is not None:
                    todo.append((p, depth + 1, followed | {real} if real else followed))
    return False


def enumerate_system(system: str, folder: Path, expected: str = "") -> list[Item]:
    return _items_in(system, folder, expected=expected or _system_of_folder(folder.name))


# ------------------------------------------------------------ matching

def hits_of(entries: list[Entry], index: DatIndex) -> int:
    """How many of these files / tracks a DAT knows by hash."""
    return sum(any(index.lookup(v.size, v.crc, v.md5, v.sha1) for v in (e.full, e.headerless) if v is not None)
               for e in entries if not e.name.lower().endswith((".cue", ".gdi")))


def match(item: Item, entries: list[Entry], index: DatIndex, name_dats: set[str] | None = None) -> Result:
    """``name_dats``: only these DATs count for a match by name alone (the data matched nothing)."""
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
        g = index.game_named(stem, name_dats)
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
        if best.name.startswith("[BIOS]"):      # emulators look for these by their own names
            return Result(item, VERIFIED, best.name, best.dat, "BIOS file: its name is left alone", ers)
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

def split_archive(item: Item, entries: list[Entry], index: DatIndex) -> list[tuple[Item, list[Entry]]] | None:
    """For an archive holding several different games: one (sub-item, entries) per game, so each gets its own row.
    Files of the same game (e.g. a zipped .cue/.bin set) stay together; files no DAT knows get their own row.
    Returns None when the archive holds a single game."""
    data = [e for e in entries if not e.name.lower().endswith((".cue", ".gdi"))]
    if item.kind != "archive" or len(data) < 2:
        return None
    games_of: list[set[str]] = []
    for e in data:
        gs = set()
        for v in (e.full, e.headerless):
            if v is not None:
                gs.update(g.name for g, _r in index.lookup(v.size, v.crc, v.md5, v.sha1))
        games_of.append(gs)
    groups: list[tuple[set[str], list[int]]] = []   # union of entries sharing a game
    for i, gs in enumerate(games_of):
        target = next((g for g in groups if gs and g[0] & gs), None) if gs else None
        if target:
            target[0].update(gs)
            target[1].append(i)
        else:
            groups.append((set(gs), [i]))
    if len(groups) < 2:
        return None
    out = []
    for _gs, idxs in groups:
        members = [data[i] for i in idxs]
        names = {Path(m.name).stem for m in members}
        cues = [e for e in entries if e.name.lower().endswith((".cue", ".gdi")) and Path(e.name).stem in names]
        sub = Item(item.system, item.path, item.kind, member=members[0].name, expected=item.expected,
                   stamp=item.stamp, keep=item.keep)
        out.append((sub, members + cues))
    return out


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
        self.switch_keys: Path | None = None   # prod.keys for reading Switch packages (make_scanner sets it)
        self.packages_off: set[str] = set()    # system folders where reading package data is turned off
        self.all_index: Callable[[], DatIndex | None] = lambda: None    # every DAT there is (loaded when needed)
        self.folder_system: dict[str, str] = {}     # folder name -> system chosen for it in Manage DATs
        self.stats = {"remembered": 0, "hashed": 0, "not_scanned": 0}
        self._stats_lock = threading.Lock()

    def _count(self, key: str) -> None:
        with self._stats_lock:
            self.stats[key] += 1

    # -- hashing one item (with cache) --------------------------------
    def hash_item(self, item: Item, progress: ProgressCb | None = None,
                  again: bool = False) -> tuple[list[Entry], dict, bool]:
        p = item.path
        st = p.stat()
        mtime = st.st_mtime + sum(x.stat().st_mtime for x in item.parts)
        size = st.st_size + sum(x.stat().st_size for x in item.parts)
        item.stamp = (size, mtime)
        if self.cache and self.mode != FULL and not again:
            hit = self.cache.get(p, size, mtime)
            if hit and item.kind == "chd" and hit[1].get("kind") == "gd" \
                    and hit[1].get("layout", 1) < chd.TRACK_LAYOUT:
                hit = None  # GD-ROM hashed with the old .gdi layout: redo it with the Redump-exact one
            if hit:
                kind, extra, entries = hit
                if item.kind == "file" and kind in ("archive", "chd", "disc"):
                    item.kind = kind            # found out earlier: it isn't what its extension says
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
        elif item.kind == "disc":
            with self.chd_sem:   # a whole disc is decompressed: throttle like CHDs
                if self.cancel.is_set():
                    raise Cancelled()
                if progress:
                    progress("reading the disc inside (nodtool)", 0.0)
                entry, info = nod.disc_hash(p, self.cancel)
            entries, extra = [entry], {"disc": info}
        elif item.kind == "archive":
            entries = hash_archive(p, self.cancel, bytes_cb)
        elif item.kind == "cueset":
            entries = [hash_file(p, self.cancel)] + [hash_file(x, self.cancel, bytes_cb) for x in item.parts]
        else:
            entries = [hash_file(p, self.cancel, bytes_cb)]
        extra["hv"] = HASH_VERSION
        if item.kind in ("file", "archive", "cueset", "disc"):
            extra["ident"] = _ident_dict(item)
        if self.cache:
            self.cache.put(p, size, mtime, item.kind, extra, entries)
        self._count("hashed")
        return entries, extra, False

    def check(self, item: Item, index: DatIndex | None,
              progress: ProgressCb | None = None) -> Result | list[Result] | None:
        if item.kind == "folder":
            game = identify.extracted_game(item.path)
            return Result(item, SKIPPED, note="Folder (extracted game / installed title) - not checked",
                          found=game or "", found_by="header" if game else "")
        if item.kind == "file" and item.path.suffix.lower() not in STRONG_EXTS:
            self._right_kind(item)      # no extension or a generic one: what is it really?
        if item.kind == "package":
            if item.system in self.packages_off:
                return Result(item, NODAT, note="Reading package data is turned off for this folder (Manage DATs)")
            return self.check_package(item)
        if index is None:
            return None if item.maybe else self._no_dat(item, progress)
        return self._check_dat(item, index, progress)

    def _right_kind(self, item: Item) -> "identify.Ident | None":
        """Files that aren't what their extension says (a zip called .sfc, a CHD called .bin)."""
        hit = identify.identify(item.path)
        if hit and hit.container and item.kind == "file":
            item.kind = hit.container
        return hit

    def _no_dat(self, item: Item, progress) -> Result | list[Result] | None:
        """No DAT for this folder's system. A file that belongs to another system is still checked with that one's."""
        from .systems import UNVERIFIABLE_HINT
        hit = self._right_kind(item) if item.kind == "file" else \
            _ident_from(_ident_dict(item)) if self.mode != REMEMBERED else None
        if item.kind == "package":
            return self.check_package(item)
        want = self._expected(item)
        if hit and hit.system and not same_family(hit.system, want):
            index = self.all_index()
            if index is not None:
                return self._check_dat(item, index, progress)
        r = Result(item, NODAT, note=UNVERIFIABLE_HINT.get(item.system, "No DAT loaded for this system"))
        self._place(r, hit, "")
        return r

    def _check_dat(self, item: Item, index: DatIndex, progress) -> Result | list[Result] | None:
        try:
            entries, extra, cached = self.hash_item(item, progress)
            own = hits_of(entries, index)
            data = sum(not e.name.lower().endswith((".cue", ".gdi")) for e in entries)
            if own < data and item.kind == "file" and self._right_kind(item) and item.kind != "file":
                if item.kind == "package":
                    return self.check_package(item)
                entries, extra, cached = self.hash_item(item, progress, again=True)
                own = hits_of(entries, index)
            elif own < data and cached and self.mode != REMEMBERED and extra.get("hv", 1) < HASH_VERSION \
                    and item.kind in ("file", "archive") and _safe_size(item.path) <= 512 << 20:
                entries, extra, cached = self.hash_item(item, progress, again=True)   # remembered by an older version
                own = hits_of(entries, index)
        except Cancelled:
            raise
        except NotScanned:
            self._count("not_scanned")
            return None
        except Exception as exc:  # noqa: BLE001 - report every failure as a row
            r = Result(item, ERROR, note=str(exc) or exc.__class__.__name__)
            self._place(r, identify.identify(item.path), "")
            return r
        ident = _ident_from(extra.get("ident"))
        if ident is None and not own and self.mode != REMEMBERED and item.kind in ("file", "archive", "cueset", "disc"):
            ident = _ident_from(_ident_dict(item))
        # what the folder's own DAT doesn't know may well be in another system's
        used = index
        if own < data:
            everything = self.all_index()
            if everything is not None and everything is not index and (
                    hits_of(entries, everything) > own or not own and ident is not None and ident.system
                    and not same_family(ident.system, self._expected(item) or ident.system)):
                used = everything
        if item.maybe and not hits_of(entries, used) and ident is None:
            return None
        parts = split_archive(item, entries, used)
        if parts:
            return [self._finish(sub, ents, used, dict(extra), cached, ident, used is index) for sub, ents in parts]
        return self._finish(item, entries, used, extra, cached, ident, used is index)

    # -- whose system is it? ----------------------------------------------
    def _expected(self, item: Item) -> str:
        """The system this file's place in the library says it is ("" = the folder doesn't say)."""
        if item.expected and item.expected != _system_of_folder(item.system):
            return item.expected                    # a nearer folder, named like a system
        if any(p.name == item.system for p in item.path.parents):
            return self.folder_system.get(item.system) or item.expected
        return item.expected

    def _place(self, r: Result, ident, dat: str, own: bool = False) -> None:
        """Fill in r.found / r.misplaced: by the DAT its data matched, else by its own header."""
        from .library import system_for_dat
        r.ident = ident
        by_hash = system_for_dat(dat) if dat and any(er.rom for er in r.entries) else None
        if by_hash:
            r.found, r.found_by = by_hash, "hash"
        elif ident is not None and ident.system:
            r.found, r.found_by = ident.system, "header"
        want = self._expected(r.item)
        if not r.found or not want or r.found == want or own and by_hash and want == self.folder_system.get(
                r.item.system):
            return
        if r.found_by == "header" and same_family(r.found, want):
            return                                  # the header alone can't tell these apart for sure
        if by_hash and ident is not None and ident.system == want:
            return                                  # listed under one system, made for both (Game Boy / Color)
        if _shares_folder(r.item, r.found):
            return
        r.misplaced = True

    # -- console packages ------------------------------------------------
    def check_package(self, item: Item) -> Result:
        """PS4 PKG / Switch NSP·XCI: identified from the package's own metadata (no DAT exists). Problems that
        only show when looking at the whole folder (update without its game, DLC for another region, duplicates,
        older updates) come from reading the other packages next to it."""
        try:
            info = _package_info(item.path, self.switch_keys)
        except packages.PackageError as exc:
            if item.system == "(other)" and packages.package_ext(item.path) in packages.SWITCH_EXTS:
                item.system = "switch"
            if (item.path.suffix.lower() == ".pkg" and "PS3" in str(exc)) or "prod.keys" in str(exc):
                return Result(item, NODAT, note=str(exc))
            return Result(item, ERROR, note=str(exc))
        problems = _package_problems(item, info, self.switch_keys)
        if item.system == "(other)":
            item.system = info.platform
        want = self._expected(item)
        if item.path.is_file():
            item.stamp = stamp_of(item)
        integ = self.integrity_of(item)
        r = Result(item, ATTENTION if problems else IDENTIFIED, packages.clean_title(info.title),
                   f"{packages.PLATFORM_NAMES.get(info.platform, info.platform)} package data",
                   packages.describe(info) + ("" if not problems else " ⚠ " + "; ".join(problems)))
        r.chd_info = {"package": info.as_dict(), "problems": problems}
        r.found, r.found_by = info.platform, "package"
        if want and want != r.found and not (info.platform == "switch" and want == "switch2"):
            r.misplaced, r.status = True, MISPLACED
            r.note += f" ⚠ belongs in a {system_name(r.found)} folder, not {system_name(want)}"
        show_integrity(r, integ)
        return r

    def integrity_of(self, item: Item):
        """What an earlier integrity check of this (unchanged) package found, or None."""
        from .integrity import Integrity
        if not self.cache or not item.path.is_file():
            return None
        try:
            hit = self.cache.get(item.path, *stamp_of(item))
        except OSError:
            return None
        return Integrity.from_dict(hit[1].get("integrity")) if hit and hit[0] == "package" else None

    def check_integrity(self, r: Result, progress: ProgressCb | None = None) -> Result:
        """Read the whole package (see integrity.py), remember the outcome and show it in the result."""
        from . import integrity
        item = r.item
        integ = integrity.check(item.path, self.switch_keys, lambda f, what: progress and progress(what, f), self.cancel)
        if self.cache:
            self.cache.put(item.path, *stamp_of(item), "package", {"integrity": integ.as_dict()}, [])
        fresh = self.check_package(item)
        if not fresh.chd_info:                      # its metadata couldn't be read
            show_integrity(fresh, integ)
        return fresh

    def _finish(self, item: Item, entries: list[Entry], index: DatIndex, extra: dict, cached: bool,
                ident=None, own: bool = True) -> Result:
        from .library import dats_of, system_for_dat
        if item.kind == "disc":
            for e in entries:   # nodtool gives no disc size: take it from the DAT entry the SHA-1 matches
                hits = index.lookup(e.full.size, e.full.crc, e.full.md5, e.full.sha1)
                if hits and not e.full.size:
                    e.full.size = hits[0][1].size or 0
        # a name without matching data only means something within the right system's DAT
        sysid = ident.system if ident is not None and ident.system else identify.system_from_ext(
            item.member or item.path.name) or (self._expected(item) if item.system != OTHER else "")
        named = dats_of(index, sysid, family=True) if sysid else None
        if named is None and len({g.dat for g in index.by_name.get(item.stem.lower(), [])}) > 1:
            named = set()                   # the name is in several systems' DATs: can't tell which
        if named is not None and not named and own and item.system != OTHER and same_family(
                sysid, self.folder_system.get(item.system, sysid)):
            named = None                    # the folder's own DAT, under a name we don't know
        r = match(item, entries, index, named)
        if item.kind == "disc":
            fmt = item.path.suffix[1:].upper()
            if r.status in (BAD, UNKNOWN) and item.path.suffix.lower() == ".wbfs":
                r.note += " - WBFS files are scrubbed when made, so they can't match Redump"
            elif r.status in (BAD, UNKNOWN):
                r.note += f" (checked the disc inside the {fmt} with nodtool)"
            elif not r.note:
                r.note = f"Disc inside the {fmt} matches"
        r.chd_info = {k: v for k, v in extra.items() if k not in ("hv", "ident", "fuzzy")}
        r.cached = cached
        self._place(r, ident, r.dat, own)
        if r.misplaced:
            where = f"{system_name(r.found)} game in a {system_name(self._expected(item))} folder"
            if r.status in (VERIFIED, MISNAMED):
                also = f"; should be named “{r.game}”" if r.status == MISNAMED else ""
                r.status, r.note = MISPLACED, where + also
            else:
                r.note += f" - {where}"
        elif r.found_by == "header" and r.status in (UNKNOWN, NODAT) and item.system in (OTHER, ""):
            r.note += f" - {system_name(r.found)} ROM" + (f", titled “{ident.title}”" if ident.title else "")
        elif r.status == UNKNOWN and ident is not None and ident.title:
            r.note += f" (titled “{ident.title}” inside)"
        if item.system == OTHER:
            # identified across several DATs: show (and use) the system the data belongs to
            item.system = r.found or (system_for_dat(r.dat) if r.dat else None) or OTHER
        if item.kind == "chd":
            from .discdetect import check_chd
            from .systems import guess_system
            sysdef = guess_system(r.found or item.system)
            game = index.game_named(r.game) if r.game else index.game_named(item.stem)
            r.media = check_chd(extra, sysdef.id if sysdef else "", game)
        from . import tags
        tags.apply(r, self.marks, MODIFIED, TAGGABLE)
        if r.status in (UNKNOWN, BAD) and item.kind == "file":
            self._trimmed_or_padded(r, index, extra)
        return r

    def _trimmed_or_padded(self, r: Result, index: DatIndex, extra: dict) -> None:
        """A cartridge dump with its unused end cut off, or with extra bytes after it, is still that game: compare
        it at the sizes the DAT lists. (The result is remembered with the hashes.)"""
        from .library import dats_of
        item = r.item
        if "fuzzy" not in extra:
            if self.mode == REMEMBERED:
                return
            extra["fuzzy"] = None
            size = _safe_size(item.path)
            sysid = r.found or identify.system_from_ext(item.path.name)
            full = self.all_index() or index
            dats = dats_of(full, sysid, family=True) if sysid in identify.SYSTEM_EXTS else set()
            if dats and 1024 < size <= 256 << 20:
                extra["fuzzy"] = _fuzzy(item.path, size, r.entries[0].entry.header_len if r.entries else 0, full, dats)
            if self.cache:
                try:
                    st = item.path.stat()
                    self.cache.put(item.path, st.st_size, st.st_mtime, item.kind, extra,
                                   [er.entry for er in r.entries])
                except OSError:
                    pass
        hit = extra["fuzzy"]
        if hit:
            r.status, r.game, r.dat = MODIFIED, hit["game"], hit["dat"]
            r.note = hit["what"] + (f" - {system_name(r.found)} game in another system's folder" if r.misplaced else "")
            r.tag, r.tag_source = hit["tag"], "from its data"
            r.orig_status, r.orig_note = MODIFIED, r.note
            from .library import system_for_dat
            r.found, r.found_by = system_for_dat(r.dat) or r.found, "hash"

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
            for one in (res if isinstance(res, list) else [res] if res is not None else []):
                on_result(one)
            if on_progress:
                on_progress(done, total, it.path.name, 1.0)

        # Small files first so the table fills quickly; CHDs are throttled by the semaphore.
        order = sorted(items, key=lambda it: (it.kind in ("chd", "disc"), _safe_size(it.path)))
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for f in [ex.submit(job, it) for it in order]:
                exc = f.exception()
                if exc and not isinstance(exc, Cancelled):
                    raise exc


def stamp_of(item: Item) -> tuple[int, float]:
    """(size, time) of an item's files together: changes whenever one of them does."""
    st = item.path.stat()
    parts = [x.stat() for x in item.parts]
    return st.st_size + sum(x.st_size for x in parts), st.st_mtime + sum(x.st_mtime for x in parts)


def show_integrity(r: Result, integ) -> None:
    from . import integrity
    if integ is None:
        return
    r.chd_info = {**(r.chd_info or {}), "integrity": integ.as_dict()}
    if integ.state == integrity.DAMAGED:
        r.status = BAD
        r.note = f"NOT INTACT: {integ.note} (checked {integ.date}) · {r.note}"
    elif integ.state == integrity.INTACT:
        r.note += f" ✔ intact (checked {integ.date})"
    else:
        r.note += f" · integrity unknown: {integ.note}"


# systems whose headers look alike or whose games run on each other's console
_FAMILIES = [{"gb", "gbc"}, {"ngp", "ngpc"}, {"wonderswan", "wonderswan-color"}, {"tg16", "supergrafx", "pcecd"},
             {"genesis", "sega32", "segacd"}, {"sms", "gamegear", "sg1000"}, {"xbox", "xbox360"}, {"nes", "fds"},
             {"switch", "switch2"}, {"ngc", "wii"}]


def same_family(a: str, b: str) -> bool:
    return a == b or any(a in f and b in f for f in _FAMILIES)


def _shares_folder(item: Item, system: str) -> bool:
    """The folder is one both systems use (RomM's "pce" holds PC Engine cartridges and CDs)."""
    from .systems import BY_ID
    names = {n.lower() for n in (item.system, *(p.name for p in item.path.parents[:3]))}
    return bool(system in BY_ID and names & set(BY_ID[system].folders))


def system_name(sysid: str) -> str:
    from .systems import BY_ID
    return BY_ID[sysid].name if sysid in BY_ID else sysid


def _ident_dict(item: Item) -> dict | None:
    if item.kind == "archive":
        hit = identify.identify_archive(item.path)
    else:
        path = item.path
        if item.kind == "cueset" and path.suffix.lower() in (".cue", ".gdi"):
            hit = None
            if path.suffix.lower() == ".gdi":
                return asdict(identify.Ident("dc", "GD-ROM descriptor", ext=".gdi"))
            for part in item.parts[:3]:             # the data track is first or second
                hit = identify.identify(part)
                if hit and hit.system:
                    break
        else:
            hit = identify.identify(path)
    return asdict(hit) if hit else None


def _ident_from(d: dict | None) -> "identify.Ident | None":
    try:
        return identify.Ident(**d) if d else None
    except TypeError:
        return None


def _fuzzy(path: Path, size: int, header: int, index: DatIndex, dats: set[str]) -> dict | None:
    """Does this dump equal a DAT entry once the missing end is filled up, or the extra end is cut off?"""
    import hashlib
    import zlib

    def hit_for(n: int, crc: int, md5, sha1):
        for g, rom in index.lookup(n, f"{crc & 0xFFFFFFFF:08x}", md5.hexdigest(), sha1.hexdigest()):
            if g.dat in dats:
                return g
        return None
    with open(path, "rb") as fh:
        data = memoryview(fh.read())[header:]
    size = len(data)
    sizes = sorted({n for d in dats for n in index.sizes(d)})
    md5, sha1, crc, pos = hashlib.md5(), hashlib.sha1(), 0, 0
    for n in [n for n in sizes if size / 4 <= n < size][-40:] + [size]:      # the beginning, cut at each known size
        crc = zlib.crc32(data[pos:n], crc)
        md5.update(data[pos:n])
        sha1.update(data[pos:n])
        pos = n
        if n < size:
            g = hit_for(n, crc, md5, sha1)
            if g:
                return {"game": g.name, "dat": g.dat, "tag": "Overdump",
                        "what": f"“{g.name}” with {size - n:,} extra bytes at the end"}
    last = data[-1] if size else 0
    for fill in dict.fromkeys((0xFF, 0x00, last)):
        m2, s2, c2, pos = md5.copy(), sha1.copy(), crc, size
        for n in [n for n in sizes if size < n <= max(size * 16, 1 << 20) and n <= 512 << 20][:40]:
            block = bytes([fill]) * min(n - pos, 1 << 20)
            while pos < n:
                b = block[:n - pos]
                c2 = zlib.crc32(b, c2)
                m2.update(b)
                s2.update(b)
                pos += len(b)
            g = hit_for(n, c2, m2, s2)
            if g:
                return {"game": g.name, "dat": g.dat, "tag": "Trimmed",
                        "what": f"“{g.name}” with its unused end cut off ({n - size:,} bytes; ROM Optimizer can "
                                f"untrim it)"}
    return None


def make_scanner(cfg, cache=None, mode: str = QUICK) -> "Scanner":
    """Build a Scanner from the settings as they are right now (a running scan keeps its own)."""
    s = Scanner(Path(cfg["tmp_dir"]), cfg["workers"], cfg["chd_workers"], cache,
                ram_mode=cfg["temp_mode"] == "ram" and ram_mode_supported())
    s.mode = mode
    from .library import all_dats, shared_index
    s.all_index = lambda: shared_index(all_dats(cfg))
    s.folder_system = {f: (st or {}).get("system") or "" for f, st in (cfg["systems"] or {}).items()}
    s.packages_off = {f for f, st in (cfg["systems"] or {}).items() if (st or {}).get("read_packages") is False}
    try:
        s.switch_keys = packages.switch_keys(cfg)
    except Exception:  # noqa: BLE001
        s.switch_keys = None
    return s


# ---------------------------------------------------------------- console packages

_PKG_CACHE: dict[tuple, object] = {}
_PKG_LOCK = threading.Lock()


def _package_info(path: Path, keys: Path | None) -> "packages.PackageInfo":
    """Read (and remember, per size + mtime) one package's metadata. Errors are remembered too."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime, str(keys or ""), str(packages.title_keys() or ""))
    with _PKG_LOCK:
        hit = _PKG_CACHE.get(key)
    if hit is None:
        try:
            hit = packages.read(path, keys)
        except packages.PackageError as exc:
            hit = exc
        with _PKG_LOCK:
            _PKG_CACHE[key] = hit
    if isinstance(hit, Exception):
        raise hit
    return hit


def _package_scope(item: Item) -> Path:
    """The folder whose packages belong together: the system folder (e.g. roms/ps4), else the file's folder."""
    for parent in item.path.parents:
        if parent.name == item.system or parent.name.lower() in PACKAGE_FOLDERS:
            return parent
    return item.path.parent


# folder names that hold one console's packages (they have no DAT, so they're scanned as "other" folders)
PACKAGE_FOLDERS = {"ps4", "playstation4", "switch", "switch-2", "switch2", "nsw", "ns", "nsw2", "wiiu", "wii-u",
                   "wii u"}


_SCOPE_CACHE: dict[tuple, tuple[float, dict]] = {}


def _package_problems(item: Item, info, keys: Path | None) -> list[str]:
    """Problems for this package, from checking every package in its folder together. The folder's result is
    reused for a minute so a scan of N packages reads the folder once, not N times."""
    import time
    scope = _package_scope(item)
    ck = (str(scope), str(keys or ""))
    with _PKG_LOCK:
        hit = _SCOPE_CACHE.get(ck)
    if hit is None or time.monotonic() - hit[0] > 60:
        infos = []
        for f in packages.iter_packages(scope, MAX_DEPTH + 1):
            try:
                infos.append(_package_info(f, keys))
            except (packages.PackageError, OSError):
                continue
        hit = (time.monotonic(), packages.check_set(infos))
        with _PKG_LOCK:
            _SCOPE_CACHE[ck] = hit
    return hit[1].get(str(item.path), [])


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0
