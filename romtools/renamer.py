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

FILE, M3U_EDIT, RMDIR = "rename", "m3u-edit", "rmdir"


@dataclass
class Op:
    src: Path
    dst: Path
    reason: str = ""
    kind: str = FILE            # rename | m3u-edit | rmdir (an empty folder; src == dst)
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
    from .scanner import MISNAMED, MISPLACED
    ops = []
    for r in results:
        if r.status not in (MISNAMED, MISPLACED) or r.found_by == "package" or r.game == r.item.stem or not r.game or r.item.kind == "folder" or getattr(r.item, "member", ""):
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


# ---------------------------------------------------------------- sorting into system folders

# saves, patches and subchannel data that belong to a ROM: they move (and are named) with it
SIDECARS = (".sav", ".srm", ".state", ".sbi", ".cht", ".dsv", ".eep", ".fla", ".sra", ".mpk", ".rtc")
_DISC_PART = re.compile(r"\s*\((?:Disc|Disk|CD|Side|Track)\b[^)]*\)", re.I)


def system_folders(root: Path, cfg, results=()) -> dict[str, Path]:
    """Where each system's ROMs live in this library: {catalogue id: folder}. A folder directly in the library wins
    over one further down (which is taken from the scanned files)."""
    from .datmanager import library_folders
    from .systems import guess_system
    out: dict[str, Path] = {}
    exact: set[str] = set()
    for name in library_folders(root):
        st = (cfg["systems"] or {}).get(name) or {}
        g = guess_system(name)
        sid = st.get("system") or (g.id if g else "")
        if sid and (sid not in out or (name.lower() == sid and sid not in exact)):
            out[sid] = root / name
            if name.lower() == sid:
                exact.add(sid)
    deeper: dict[str, dict[Path, int]] = {}
    for r in results:
        if r.misplaced or not r.item.expected or r.item.expected in out:
            continue
        for p in r.item.path.parents:
            if p == root or root not in p.parents:
                break
            g = guess_system(p.name)
            if g and g.id == r.item.expected:
                d = deeper.setdefault(g.id, {})
                d[p] = d.get(p, 0) + 1
                break
    for sid, found in deeper.items():
        out[sid] = max(found, key=found.get)
    return out


def _right_ext(r) -> str:
    """The extension this file should have, when its own isn't one its system uses."""
    from . import identify
    from .scanner import STRONG_EXTS
    path, ext = r.item.path, r.item.path.suffix
    if r.item.kind in ("archive", "chd", "disc", "package"):
        hit = identify.identify(path)
        ok = {"archive": (".zip", ".7z", ".rar"), "chd": (".chd",), "package": (".nsp", ".nsz", ".xci", ".xcz", ".pkg", ".wua"),
              "disc": (".rvz", ".wia", ".gcz", ".ciso", ".wbfs")}[r.item.kind]
        return hit.ext if hit and hit.ext and ext.lower() not in ok else ext
    if r.item.kind != "file":
        return ext
    known = identify.SYSTEM_EXTS.get(r.found)
    if known is None:                            # disc images
        if ext.lower() in STRONG_EXTS - {e for x in identify.SYSTEM_EXTS.values() for e in x} or \
                ext.lower() in (".bin", ".img"):
            return ext
        return r.ident.ext if r.ident is not None and r.ident.ext else ext
    if ext.lower() in known:
        return ext
    rom = Path(r.entries[0].rom).suffix if r.entries and r.entries[0].rom and not r.entries[0].headerless else ""
    for e in (rom, r.ident.ext if r.ident is not None else ""):
        if e.lower() in known:
            return e
    return known[0]


def plan_sorting(results, root: Path, cfg, by_header: bool = True, fix_ext: bool = True) -> tuple[list[Op], list[str]]:
    """Move ROMs that are loose, or in another system's folder, into their own system's folder (made if there is none).
    ``by_header``: also files that no DAT knows but whose own header says the system (hacks, translations, no DAT)."""
    from .library import root_system
    notes: list[str] = []
    if root_system(cfg, root):
        n = sum(r.misplaced for r in results)
        return [], [f"{n} ROM(s) here belong to other systems: choose the library folder above to sort them"] if n else []
    homes = system_folders(root, cfg, results)
    groups: dict[Path, list] = {}
    for r in results:
        groups.setdefault(r.item.path, []).append(r)
    # what else is in each folder: one that holds several files of a single game is kept as that game's folder
    games: dict[Path, set] = {}
    count: dict[Path, int] = {}
    for path, rs in groups.items():
        count[path.parent] = count.get(path.parent, 0) + 1 + len(rs[0].item.parts)
        games.setdefault(path.parent, set()).update(
            (r.found, _DISC_PART.sub("", r.game or r.item.stem).lower()) for r in rs)
    ops: list[Op] = []
    mixed = folders = 0
    for path, rs in groups.items():
        r = rs[0]
        if r.item.kind == "folder" or path.is_dir():
            folders += bool(r.misplaced)
            continue
        systems = {x.found for x in rs}
        if len(systems) > 1:
            mixed += any(x.misplaced or not x.item.expected for x in rs)
            continue
        if not r.found or r.found_by == "header" and not by_header:
            continue
        ext = _right_ext(r) if fix_ext else path.suffix
        name = path.stem + ext if path.suffix and len(path.suffix) <= 6 and " " not in path.suffix else path.name + (
            ext if ext != path.suffix else "")
        home = homes.get(r.found) or root / r.found
        if home in path.parents or not any(x.misplaced for x in rs) and r.item.expected:     # it's in the right place
            if name != path.name and not r.item.parts:
                ops.append(Op(path, path.with_name(name), f"Extension: {r.found} ROMs aren't called "
                                                          f"{path.suffix or 'without one'}", system=r.found))
            continue
        dest, parent = home, path.parent
        from .systems import guess_system
        if parent != root and count[parent] > 1 and len(games[parent]) == 1 and not guess_system(parent.name):
            dest = home / parent.name
        how = {"hash": f"DAT match: {r.dat}", "package": "its own package data"}.get(
            r.found_by, "its own header" + (f" (“{r.ident.title}”)" if r.ident is not None and r.ident.title else ""))
        ops.append(Op(path, dest / name, f"Sort: {how}", system=r.found, move=True))
        for part in r.item.parts:               # a .cue / .gdi keeps its tracks, in the same places around it
            try:
                rel = part.relative_to(path.parent)
            except ValueError:
                ops.pop()                       # tracks outside the sheet's own folder: leave the set alone
                ops = [o for o in ops if o.src not in r.item.parts]
                break
            ops.append(Op(part, dest / rel, f"Sort: goes with {path.name}", system=r.found, move=True))
    if mixed:
        notes.append(f"{mixed} archive(s) hold games of several systems: not sorted")
    if folders:
        notes.append(f"{folders} extracted game folder(s) are in another system's folder: not moved")
    return ops, notes


def merge(ops: list[Op], sorting: list[Op]) -> list[Op]:
    """One change per file. Names come from the first op that has one (DAT, package, rules), the folder from sorting;
    a package's own game folder is kept inside the system folder."""
    out: list[Op] = []
    by_src: dict[Path, Op] = {}
    for op in ops:
        if op.src not in by_src:
            by_src[op.src] = op
            out.append(op)
    for so in sorting:
        op = by_src.get(so.src)
        if op is None:
            by_src[so.src] = so
            out.append(so)
            continue
        dest = so.dst.parent
        if op.move and op.dst.parent != op.src.parent and op.dst.parent.name != dest.name:
            dest = dest / op.dst.parent.name
        ext = so.dst.suffix if so.dst.suffix != so.src.suffix else op.dst.suffix
        stem = op.dst.name[:-len(op.dst.suffix)] if op.dst.suffix else op.dst.name
        op.dst, op.system = dest / (stem + ext), so.system
        op.move = op.move or so.move
        op.reason += " · sort" if so.reason.startswith("Sort: DAT match") and op.reason.startswith("DAT") else \
            " · " + so.reason
    for op in list(out):                        # saves and the like
        if op.kind != FILE or not op.move or op.dst.parent == op.src.parent:
            continue
        for ext in SIDECARS:
            side = op.src.with_suffix(ext)
            if side not in by_src and side != op.src and side.is_file():
                by_src[side] = Op(side, op.dst.with_suffix(ext), f"Sort: goes with {op.src.name}", system=op.system,
                                  move=True)
                out.append(by_src[side])
    return out


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
    from .ps4pkg import pieces
    from .scanner import IGNORE_EXTS, _cue_parts, _gdi_parts
    ops = []
    entries = [(k, f, set()) for k, f in folders.items()] if isinstance(folders, dict) else folders
    for system, folder, skip in entries:
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        tracks = {t for p in children for t in (_cue_parts(p) if p.suffix.lower() == ".cue" else
                                                _gdi_parts(p) if p.suffix.lower() == ".gdi" else ())}
        for p in children:
            if p.name.startswith(".") or p.name in skip or p in tracks or p.suffix.lower() == ".pkg" and pieces(p):
                # (a sheet lists its tracks by name, the pieces of a split package find each other by name)
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


def plan_rules_for(paths: list[Path], rules: Rules) -> list[Op]:
    """The cleanup rules for picked files only (not a disc's tracks: its .cue / .gdi lists them by name)."""
    from .library import sheet_of
    from .ps4pkg import pieces
    ops = []
    for p in paths:
        if sheet_of(p) or pieces(p):
            continue
        new = apply_rules(p.name, rules)
        if new != p.name:
            ops.append(Op(p, p.with_name(new), "cleanup rules", system=p.parent.name))
    return ops


# ---------------------------------------------------------------- empty folders

class FolderTree:
    """What's in these folders, read once (so the preview can be recalculated without asking the disk or the network
    again). Extracted games and hidden folders count as content and are never looked into: a game can need its empty
    folders."""

    def __init__(self, scopes: list[Path], removable: set[Path] = frozenset(), cancel=None):
        from .identify import extracted_game
        self.scopes = list(dict.fromkeys(scopes))
        self.removable = set(removable)         # scopes that may go themselves (a system's own folder may not)
        self.inside: dict[Path, tuple[set[str], set[str]]] = {}        # folder -> (files and other content, sub-folders)
        todo = list(self.scopes)
        while todo:
            if cancel is not None and cancel.is_set():
                self.scopes = []
                return
            d = todo.pop()
            if d in self.inside or len(d.parts) > 60:
                continue
            files, dirs = set(), set()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        (dirs if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") else files).add(e.name)
            except OSError:
                files.add("(not readable)")
            if extracted_game(d, {n.lower() for n in files | dirs}) is not None:
                self.inside[d] = ({"(extracted game)"}, set())
                continue
            self.inside[d] = (files, dirs)
            todo += [d / n for n in dirs]


def plan_empty_folders(tree: FolderTree, ops: list[Op]) -> list[Op]:
    """Folders that are empty, or will be once the enabled moves in ``ops`` are done - innermost first. Only truly empty
    ones: a hidden file or a picture keeps its folder."""
    leaving: dict[Path, set[str]] = {}
    arriving: set[Path] = set()
    for op in ops:
        if op.kind == RMDIR or not op.enabled or not op.ok:
            continue
        if op.dst.parent != op.src.parent:
            leaving.setdefault(op.src.parent, set()).add(op.src.name)
        arriving.add(op.dst.parent)
        arriving.update(op.dst.parent.parents)
    out: list[Op] = []
    gone: dict[Path, bool] = {}

    def walk(d: Path) -> bool:
        """True if this folder goes."""
        if d in gone:
            return gone[d]
        files, dirs = tree.inside.get(d, ({"?"}, set()))
        left = [n for n in sorted(dirs) if not walk(d / n)]
        goes = not left and not files - leaving.get(d, set()) and d not in arriving and (
            d not in tree.scopes or d in tree.removable)
        gone[d] = goes
        if goes:
            out.append(Op(d, d, "empty once its files are moved" if leaving.get(d) else "empty folder", RMDIR,
                          system=d.parent.name))
        return goes
    for d in tree.scopes:
        walk(d)
    return out


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
            refs = [m3u.parent / ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
            if not any(r in renames for r in refs):
                continue
            # all discs go to one other folder: the playlist goes with them
            homes = {renames.get(r, r).parent for r in refs}
            home = homes.pop() if len(homes) == 1 else m3u.parent
            new_lines = []
            for line in lines:
                ref = line.strip()
                if ref and not ref.startswith("#"):
                    target = m3u.parent / ref
                    to = renames.get(target, target)
                    if to != target or home != m3u.parent:
                        new_lines.append(line.replace(ref, os.path.relpath(to, home)))
                        continue
                new_lines.append(line)
            text = "\n".join(new_lines) + "\n"
            m3u_dst = home / m3u.name
            renamed_discs = [renames[r] for r in refs if r in renames]
            if m3u in renames:  # already renamed by a rule: fold it into this single edit op
                m3u_dst = home / renames[m3u].name
                out = [op for op in out if not (op.kind == FILE and op.src == m3u)]
            elif renamed_discs and len(renamed_discs) == len(refs) and any(d.name != r.name for d, r in
                                                                           zip(renamed_discs, refs)):
                base = re.sub(r"\s*\(Disc \d+[^)]*\)", "", renamed_discs[0].stem).strip()
                cand = home / (base + ".m3u")
                if cand != m3u and cand not in existing_dsts:
                    m3u_dst = cand
            out.append(Op(m3u, m3u_dst, "update playlist" + (" + move" if home != m3u.parent else
                                                             " + rename" if m3u_dst != m3u else ""),
                          M3U_EDIT, text, system="", move=home != m3u.parent))
    return out


# ---------------------------------------------------------------- validation

def validate(ops: list[Op]) -> list[Op]:
    srcs = {op.src for op in ops if op.enabled}
    seen: dict[str, Op] = {}
    for op in ops:
        op.problem = ""
        if op.kind == RMDIR:            # whether it's empty by then is checked when applying
            op.problem = "" if op.src.is_dir() else "folder is gone"
            continue
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
            # only the letter case changes: on Samba / Windows that "exists" already, it's the same file. In another
            # folder it's somebody else's file, whatever its case.
            case_only = op.dst.parent == op.src.parent and op.dst.name.lower() == op.src.name.lower()
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

def _says_it_all(stem: str, info) -> bool:
    """Does a file name already say which Wii U title this is, in whatever order and style?"""
    from . import packages as pk
    words = set(re.findall(r"[a-z0-9]+", stem.lower()))
    need = set(re.findall(r"[a-z0-9]+", info.title.lower()))
    if info.kind == pk.UPDATE:
        need |= {"update"} if f"v{info.version}" not in words else set()
    if info.kind == pk.DLC:
        need |= {"dlc"}
    return bool(need) and need <= words


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
        from .ps4pkg import pieces
        split = [p for p in files if p.suffix.lower() == ".pkg" and pieces(p)]
        if split:                       # the pieces find each other by name
            files = [p for p in files if p not in split]
            notes.append(f"{key}: {len(split)} piece(s) of split packages keep their names")
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
                    elif i.platform == "wiiu" and _says_it_all(f.stem, i):
                        new_name = f.name       # already names the game (and "update" / "DLC"): keep your wording
                    elif re.sub(r"[^a-z0-9]", "", new_name.lower()) == re.sub(r"[^a-z0-9]", "", low):
                        new_name = f.name       # only punctuation would change
                dst_dir = target_dir or f.parent
                dst = dst_dir / new_name
                if dst == f:
                    continue
                why = []
                if new_name != f.name:
                    why.append(f"{pk.PLATFORM_NAMES.get(i.platform, i.platform)} {i.kind} {i.title_id}")
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
    made_top: str | None = None   # the topmost folder that had to be created for it
    via: str | None = None        # temporary name while renames that depend on each other are done


def _rename(src: Path, dst: Path) -> None:
    case_only = src.parent == dst.parent and src.name.lower() == dst.name.lower()
    if dst.exists() and not (case_only and os.path.samefile(src, dst)):
        raise FileExistsError(f"{dst.name} already exists")
    if case_only:  # case-only change: go via a temp name (CIFS is case-insensitive)
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
    dirs = sorted((op for op in todo if op.kind == RMDIR), key=lambda op: -len(op.src.parts))
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
    for op in dirs:                                 # last: the folders are empty by now
        entries.append(JournalEntry(RMDIR, str(op.src), str(op.dst)))
    _save_journal(jpath, meta, entries)  # written BEFORE touching anything

    errors = []
    # renames that depend on each other (A→B while B→C, or two names swapped) go via a temporary name first
    skip, errors = _park([e for e in entries if e.kind == FILE], "src", "dst")
    _save_journal(jpath, meta, entries)
    for i, e in enumerate(entries):
        try:
            src, dst = Path(e.src), Path(e.dst)
            if e.kind == RMDIR:
                try:
                    os.rmdir(src)                   # refuses anything that isn't empty
                except OSError:
                    if src.is_dir():                # something is (still) in it: it stays
                        continue
                    raise
            elif e.kind == FILE:
                if id(e) in skip:     # couldn't be moved aside (reported above): left as it was
                    continue
                _make_parent(e, dst)
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
                           if l.strip() and not l.startswith("#") and not (dst.parent / l.strip()).exists()]
                if missing:  # a disc rename failed: leave the playlist as it was
                    raise OSError(f"playlist not updated, missing {missing[0]}")
                _make_parent(e, dst)
                _write_text(src, e.new_text)
                if dst != src:
                    try:
                        _rename(src, dst)
                    except OSError:
                        _write_text(src, e.old_text or "")      # as it was
                        raise
            e.done = True
        except OSError as exc:
            errors.append(f"{Path(e.src).name}: {exc}")
        _save_journal(jpath, meta, entries)
        if progress:
            progress(i + 1, len(entries))
    return jpath, errors


def _make_parent(e: JournalEntry, dst: Path) -> None:
    if dst.parent.exists():
        return
    top = dst.parent
    while not top.parent.exists():
        top = top.parent
    dst.parent.mkdir(parents=True)
    e.made_dir, e.made_top = str(dst.parent), str(top)


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
            if e.kind == RMDIR:
                src.mkdir(parents=True, exist_ok=True)
            elif e.kind == FILE:
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
            d = Path(e.made_dir)
            try:
                d.rmdir()
                while e.made_top and d != Path(e.made_top):
                    d = d.parent
                    d.rmdir()
            except OSError:
                pass
    data["undone"] = not errors
    data["entries"] = [asdict(e) for e in entries]
    jpath.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return errors
