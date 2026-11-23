"""Rename planning, validation, application and undo.

Nothing touches the disk until `apply()` is called.  Every applied batch is
recorded in a JSON journal first, so it can be undone later.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import JOURNAL_DIR

INVALID = re.compile(r'[/\x00]')
# Characters that break Samba/Windows clients on a CIFS share
CIFS_UNSAFE = re.compile(r'[\\:*?"<>|]')

FILE, M3U_EDIT = "rename", "m3u-edit"


@dataclass
class Op:
    src: Path
    dst: Path
    reason: str = ""
    kind: str = FILE            # rename | m3u-edit
    new_text: str | None = None  # for m3u-edit
    enabled: bool = True
    problem: str = ""
    system: str = ""
    move: bool = False          # may go to another folder (console packages: one folder per game)

    @property
    def ok(self) -> bool:
        return not self.problem


# ---------------------------------------------------------------- planning

def plan_from_results(results, use_dat_ext: bool = False) -> list[Op]:
    from .scanner import MISNAMED
    ops = []
    for r in results:
        if r.status != MISNAMED or not r.game or r.item.kind == "folder" or getattr(r.item, "member", ""):
            continue  # a game inside a multi-game archive: renaming the archive after it would be wrong
        ext = r.item.path.suffix
        if use_dat_ext and r.item.kind == "file" and r.entries and r.entries[0].rom and not r.entries[0].headerless:
            dat_ext = Path(r.entries[0].rom).suffix
            if dat_ext and dat_ext.lower() not in (".unh", ".bin"):  # headerless/generic DAT names
                ext = dat_ext
        if r.item.kind == "cueset":
            continue  # cue/bin sets need cue rewrites; reported, not auto-renamed
        dst = r.item.path.with_name(r.game + ext)
        ops.append(Op(r.item.path, dst, f"DAT match: {r.dat}", system=r.item.system))
    return ops


@dataclass
class Rules:
    strip_dup_suffix: bool = True     # "Game (USA) (1).sfc" -> "Game (USA).sfc"
    underscores: bool = False         # "Bayonetta_3" -> "Bayonetta 3"
    dots_to_spaces: bool = False      # "Some.Game.Name" -> "Some Game Name"
    collapse_spaces: bool = True
    the_suffix: bool = False          # "The Legend of X - Y" -> "Legend of X, The - Y"
    cifs_safe: bool = True            # replace : ? * etc.
    find: str = ""
    replace: str = ""
    regex: bool = False
    include_folders: bool = False
    extensions_lower: bool = False


_DUP = re.compile(r"\s*\((?:\d{1,2}|copy|Copy)\)\s*$")


def apply_rules(name: str, rules: Rules, is_dir: bool = False) -> str:
    if is_dir:
        stem, ext = name, ""
    else:
        p = Path(name)
        stem, ext = p.stem, p.suffix
        if not ext or len(ext) > 6 or " " in ext:
            stem, ext = name, ""
    if rules.find:
        if rules.regex:
            try:
                stem = re.sub(rules.find, rules.replace, stem)
            except re.error:
                pass
        else:
            stem = stem.replace(rules.find, rules.replace)
    if rules.underscores:
        stem = stem.replace("_", " ")
    if rules.dots_to_spaces:
        stem = re.sub(r"(?<=\w)\.(?=\w)", " ", stem)
    if rules.strip_dup_suffix:
        prev = None
        while prev != stem:
            prev, stem = stem, _DUP.sub("", stem)
    if rules.the_suffix and stem.lower().startswith("the "):
        rest = stem[4:]
        cut = len(rest)
        for sep in (" - ", " ("):
            i = rest.find(sep)
            if 0 < i < cut:
                cut = i
        stem = rest[:cut] + ", The" + rest[cut:]
    if rules.cifs_safe:
        stem = CIFS_UNSAFE.sub(lambda m: " - " if m.group() == ":" else "", stem)
    if rules.collapse_spaces:
        stem = re.sub(r"\s+", " ", stem).strip()
        stem = re.sub(r"\s+-\s+-\s+", " - ", stem)
    if rules.extensions_lower:
        ext = ext.lower()
    return stem + ext


def plan_from_rules(folders, rules: Rules) -> list[Op]:
    """folders: {key: folder} or [(key, folder, names to skip)] (see library.rule_folders)."""
    from .scanner import IGNORE_EXTS
    ops = []
    entries = [(k, f, set()) for k, f in folders.items()] if isinstance(folders, dict) else folders
    for system, folder, skip in entries:
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for p in children:
            if p.name.startswith(".") or p.name in skip:
                continue
            is_dir = p.is_dir()
            if is_dir and not rules.include_folders:
                continue
            if not is_dir and p.suffix.lower() in IGNORE_EXTS and p.suffix.lower() != ".m3u":
                continue
            new = apply_rules(p.name, rules, is_dir)
            if new != p.name:
                ops.append(Op(p, p.with_name(new), "cleanup rules", system=system))
    return ops


# ---------------------------------------------------------------- m3u playlists

def _m3u_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def add_playlist_ops(ops: list[Op]) -> list[Op]:
    """Add m3u edits for renamed discs, and rename playlists whose discs all got renamed."""
    renames = {op.src: op.dst for op in ops if op.kind == FILE and op.enabled}
    out = [op for op in ops if op.kind != M3U_EDIT]
    folders = {p.parent for p in renames}
    existing_dsts = {op.dst for op in out}
    for folder in folders:
        for m3u in sorted(folder.glob("*.m3u")):
            try:
                lines = _m3u_lines(m3u)
            except OSError:
                continue
            changed, refs = False, []
            new_lines = []
            for line in lines:
                ref = line.strip()
                if ref and not ref.startswith("#"):
                    target = (m3u.parent / ref)
                    refs.append(target)
                    if target in renames:
                        rel = os.path.relpath(renames[target], m3u.parent)
                        new_lines.append(line.replace(ref, rel))
                        changed = True
                        continue
                new_lines.append(line)
            if not changed:
                continue
            text = "\n".join(new_lines) + "\n"
            m3u_dst = m3u
            renamed_discs = [renames[r] for r in refs if r in renames]
            if m3u in renames:  # already renamed by a rule: fold it into this single edit op
                m3u_dst = renames[m3u]
                out = [op for op in out if not (op.kind == FILE and op.src == m3u)]
            elif renamed_discs and len(renamed_discs) == len(refs):
                base = re.sub(r"\s*\(Disc \d+[^)]*\)", "", renamed_discs[0].stem).strip()
                cand = m3u.with_name(base + ".m3u")
                if cand != m3u and cand not in existing_dsts:
                    m3u_dst = cand
            out.append(Op(m3u, m3u_dst, "update playlist" + (" + rename" if m3u_dst != m3u else ""),
                          M3U_EDIT, text, system=""))
    return out


# ---------------------------------------------------------------- validation

def validate(ops: list[Op]) -> list[Op]:
    srcs = {op.src for op in ops if op.enabled}
    seen: dict[str, Op] = {}
    for op in ops:
        op.problem = ""
        name = op.dst.name
        if not name or name in (".", "..") or INVALID.search(name):
            op.problem = "invalid name"
        elif op.dst.parent != op.src.parent and not op.move:
            op.problem = "must stay in the same folder"
        elif len(name.encode()) > 255:
            op.problem = "name too long"
        elif op.kind == FILE and op.dst == op.src:
            op.problem = "unchanged"
        elif not op.src.exists():
            op.problem = "source missing"
        else:
            case_only = op.dst.name.lower() == op.src.name.lower()
            if op.dst.exists() and not case_only and op.dst != op.src and op.dst not in srcs:
                op.problem = "target already exists"
            key = str(op.dst).lower()
            if op.enabled and key in seen and seen[key] is not op:
                op.problem = "two items would get this name"
                seen[key].problem = seen[key].problem or "two items would get this name"
            if op.enabled:
                seen[key] = op
    return ops


# ---------------------------------------------------------------- console packages

def plan_packages(folders: list[tuple], keys: Path | None, rename: bool = True,
                  organize: bool = False, only_multi: bool = True, progress=None) -> tuple[list[Op], list[str]]:
    """Names for PS4 PKGs and Switch NSP/XCI from their own metadata, and (optionally) one folder per game
    holding its base game, updates and DLC. Returns (ops, notes)."""
    from . import packages as pk
    from .scanner import MAX_DEPTH, _package_info
    ops: list[Op] = []
    notes: list[str] = []
    seen: set[Path] = set()          # a file is planned once even if two listed folders contain it
    for entry in folders:            # (key, folder) or (key, folder, sub-folder names to skip)
        key, folder = entry[0], entry[1]
        skip = entry[2] if len(entry) > 2 else set()
        if not folder.is_dir():
            continue
        files = [p for p in pk.iter_packages(folder, MAX_DEPTH + 1, skip) if p.is_file() and p not in seen]
        seen.update(files)
        infos: list[tuple[Path, pk.PackageInfo]] = []
        unread = 0
        for f in files:
            if progress:
                progress(f"Reading {f.name}")
            try:
                infos.append((f, _package_info(f, keys)))
            except (pk.PackageError, OSError):
                unread += 1
        if unread:
            notes.append(f"{key}: {unread} package(s) couldn't be read" +
                         (" (set your Switch keys in ROM Verifier → Settings)" if not keys else ""))
        groups: dict[tuple, list[tuple[Path, pk.PackageInfo]]] = {}
        for f, i in infos:
            groups.setdefault((i.platform, i.base_id), []).append((f, i))
        for (_plat, base_id), members in groups.items():
            game = next(((f, i) for f, i in members if i.kind.startswith(pk.GAME)), None)
            # keep the title the library already uses for this game ("Bloodborne - Game of the Year Edition")
            base_title, base_sfo = "", ""
            if game:
                gf, gi = game
                base_sfo = gi.title
                m = re.match(rf"(.+?)\s*\[{re.escape(gi.title_id)}\]", gf.stem, re.I)
                base_title = m.group(1).strip() if m else pk.clean_title(gi.title)
            target_dir = None
            if organize and (len(members) > 1 or not only_multi):
                parents = [f.parent for f, _ in members if f.parent != folder]
                if parents:
                    target_dir = max(set(parents), key=parents.count)     # the game's folder you already have
                else:
                    title = base_title or pk.clean_title(members[0][1].title)
                    target_dir = folder / CIFS_UNSAFE.sub("", title).strip(" .")
            for f, i in members:
                new_name = f.name
                if rename:
                    own_title = base_title
                    if not own_title:      # update / DLC without its game: keep its own title part if it has one
                        m = re.match(rf"(.+?)\s*\[{re.escape(i.title_id)}\]", f.stem, re.I)
                        own_title = m.group(1).strip() if m else ""
                    stem = pk.suggested_name(i, own_title, base_sfo)
                    new_name = CIFS_UNSAFE.sub("", stem).strip() + f.suffix.lower()
                    low = f.name.lower()
                    if i.platform == "switch" and f"[{i.title_id.lower()}]" in low and f"[v{i.version}]" in low:
                        new_name = f.name       # already has the right ID and version: keep your wording
                    elif re.sub(r"[^a-z0-9]", "", new_name.lower()) == re.sub(r"[^a-z0-9]", "", low):
                        new_name = f.name       # only punctuation would change
                dst_dir = target_dir or f.parent
                dst = dst_dir / new_name
                if dst == f:
                    continue
                why = []
                if new_name != f.name:
                    why.append(f"{'PS4' if i.platform == 'ps4' else 'Switch'} {i.kind} {i.title_id}")
                if dst_dir != f.parent:
                    why.append(f"into {dst_dir.name}/")
                ops.append(Op(f, dst, " · ".join(why), system=key, move=dst_dir != f.parent))
    return ops, notes


# ---------------------------------------------------------------- apply / undo

@dataclass
class JournalEntry:
    kind: str
    src: str
    dst: str
    old_text: str | None = None
    new_text: str | None = None
    done: bool = False
    made_dir: str | None = None   # folder created for this move (removed again by undo if empty)
    via: str | None = None        # temporary name while renames that depend on each other are done


def _rename(src: Path, dst: Path) -> None:
    if dst.exists() and dst.name.lower() != src.name.lower():
        raise FileExistsError(f"{dst.name} already exists")
    if src.name.lower() == dst.name.lower():  # case-only change: go via a temp name (CIFS is case-insensitive)
        tmp = src.with_name(f".romtools-{uuid.uuid4().hex[:8]}{src.suffix}")
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)


def _park(entries: list[JournalEntry], frm: str, to: str) -> tuple[set[int], list[str]]:
    """Renames that depend on each other (A→B while B→C, or two names swapped): move those files to a
    temporary name first (recorded in ``via``), so the second pass can put each in place without anything in
    the way. ``frm``/``to`` are "src"/"dst" for apply and "dst"/"src" for undo.
    Returns (ids of entries that couldn't be parked, error messages)."""
    starts = {getattr(e, frm).lower() for e in entries}
    ends = {getattr(e, to).lower() for e in entries}
    failed, errors = set(), []
    for e in entries:
        a, b = getattr(e, frm).lower(), getattr(e, to).lower()
        if a == b or not (b in starts or a in ends):
            continue
        cur = Path(getattr(e, frm))
        tmp = cur.with_name(f".romtools-ren-{uuid.uuid4().hex[:8]}{cur.suffix}")
        try:
            os.rename(cur, tmp)
            e.via = str(tmp)
        except OSError as exc:
            failed.add(id(e))
            errors.append(f"{cur.name}: {exc}")
    return failed, errors


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".romtools-{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save_journal(path: Path, meta: dict, entries: list[JournalEntry]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**meta, "entries": [asdict(e) for e in entries]}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def apply(ops: list[Op], cache=None, progress=None) -> tuple[Path, list[str]]:
    """Apply enabled, valid ops. Returns (journal path, list of error strings)."""
    todo = [op for op in validate(ops) if op.enabled and op.ok]
    files = [op for op in todo if op.kind == FILE]
    edits = [op for op in todo if op.kind == M3U_EDIT]
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jpath = JOURNAL_DIR / f"{stamp}-{uuid.uuid4().hex[:6]}.json"
    meta = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "undone": False}
    entries: list[JournalEntry] = []
    for op in files:
        entries.append(JournalEntry(FILE, str(op.src), str(op.dst)))
    for op in edits:
        old = op.src.read_text(encoding="utf-8", errors="replace")
        entries.append(JournalEntry(M3U_EDIT, str(op.src), str(op.dst), old, op.new_text))
    _save_journal(jpath, meta, entries)  # written BEFORE touching anything

    errors = []
    # renames that depend on each other (A→B while B→C, or two names swapped) go via a temporary name first
    skip, errors = _park([e for e in entries if e.kind == FILE], "src", "dst")
    _save_journal(jpath, meta, entries)
    for i, e in enumerate(entries):
        try:
            src, dst = Path(e.src), Path(e.dst)
            if e.kind == FILE:
                if id(e) in skip:     # couldn't be moved aside (reported above): left as it was
                    continue
                if not dst.parent.exists():
                    dst.parent.mkdir(parents=True)
                    e.made_dir = str(dst.parent)
                try:
                    _rename(Path(e.via) if e.via else src, dst)
                except OSError:
                    if e.via:         # couldn't finish: put the file back under its old name
                        os.rename(e.via, src)
                        e.via = None
                    raise
                e.via = None
                if cache:
                    cache.rename(src, dst)
            else:
                if not src.exists():
                    raise OSError("playlist vanished")
                missing = [l.strip() for l in (e.new_text or "").splitlines()
                           if l.strip() and not l.startswith("#") and not (src.parent / l.strip()).exists()]
                if missing:  # a disc rename failed: leave the playlist as it was
                    raise OSError(f"playlist not updated, missing {missing[0]}")
                _write_text(src, e.new_text)
                if dst != src:
                    _rename(src, dst)
            e.done = True
        except OSError as exc:
            errors.append(f"{Path(e.src).name}: {exc}")
        _save_journal(jpath, meta, entries)
        if progress:
            progress(i + 1, len(entries))
    return jpath, errors


def list_journals() -> list[tuple[Path, dict]]:
    out = []
    if JOURNAL_DIR.is_dir():
        for p in sorted(JOURNAL_DIR.glob("*.json"), reverse=True):
            try:
                out.append((p, json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return out


def undo(jpath: Path, cache=None) -> list[str]:
    data = json.loads(jpath.read_text(encoding="utf-8"))
    entries = [JournalEntry(**e) for e in data["entries"]]
    skip, errors = _park([e for e in entries if e.kind == FILE and e.done], "dst", "src")
    for e in reversed(entries):
        if not e.done or id(e) in skip:
            continue
        src, dst = Path(e.src), Path(e.dst)
        try:
            if e.kind == FILE:
                try:
                    _rename(Path(e.via) if e.via else dst, src)
                except OSError:
                    if e.via:
                        os.rename(e.via, dst)
                        e.via = None
                    raise
                e.via = None
                if cache:
                    cache.rename(dst, src)
            else:
                if dst != src:
                    _rename(dst, src)
                _write_text(src, e.old_text or "")
            e.done = False
        except OSError as exc:
            errors.append(f"{dst.name}: {exc}")
    for e in entries:                       # folders made for moves: remove them once they're empty again
        if e.made_dir:
            try:
                Path(e.made_dir).rmdir()
            except OSError:
                pass
    data["undone"] = not errors
    data["entries"] = [asdict(e) for e in entries]
    jpath.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return errors
