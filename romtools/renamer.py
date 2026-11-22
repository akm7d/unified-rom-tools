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
        elif op.dst.parent != op.src.parent:
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


# ---------------------------------------------------------------- apply / undo

@dataclass
class JournalEntry:
    kind: str
    src: str
    dst: str
    old_text: str | None = None
    new_text: str | None = None
    done: bool = False


def _rename(src: Path, dst: Path) -> None:
    if dst.exists() and dst.name.lower() != src.name.lower():
        raise FileExistsError(f"{dst.name} already exists")
    if src.name.lower() == dst.name.lower():  # case-only change: go via a temp name (CIFS is case-insensitive)
        tmp = src.with_name(f".romtools-{uuid.uuid4().hex[:8]}{src.suffix}")
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)


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
    for i, e in enumerate(entries):
        try:
            src, dst = Path(e.src), Path(e.dst)
            if e.kind == FILE:
                _rename(src, dst)
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
    errors = []
    for e in reversed(entries):
        if not e.done:
            continue
        src, dst = Path(e.src), Path(e.dst)
        try:
            if e.kind == FILE:
                _rename(dst, src)
                if cache:
                    cache.rename(dst, src)
            else:
                if dst != src:
                    _rename(dst, src)
                _write_text(src, e.old_text or "")
            e.done = False
        except OSError as exc:
            errors.append(f"{dst.name}: {exc}")
    data["undone"] = not errors
    data["entries"] = [asdict(e) for e in entries]
    jpath.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return errors
