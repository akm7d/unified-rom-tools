"""How a chosen folder is split into things to scan.

Any folder works:
  * sub-folders recognised as systems (psx/, snes/, ... or mapped in Manage DATs) are scanned with their own DAT;
  * everything else - ROMs and archives lying directly in the folder, game-named folders like
    "Blue Stinger (USA)/", download folders - goes into one "Other folders & loose files" group whose ROMs are
    identified against ALL available DATs, so no system has to be picked.
If the chosen folder is itself a system folder (e.g. .../roms/gb), it is used with that system's DAT.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .config import DAT_DIR, Config
from .datmanager import IndexCache, library_folders
from .systems import BY_ID, CATALOG, guess_system

OTHER = "(other)"                       # group key for loose files / unrecognised folders
OTHER_LABEL = "Other folders & loose files"


def is_system_folder(cfg: Config, name: str) -> bool:
    s = (cfg["systems"] or {}).get(name) or {}
    return bool(s.get("system") or s.get("dats") or guess_system(name))


def root_system(cfg: Config, root: Path) -> str | None:
    """The chosen folder itself is a system folder (e.g. roms/gb): its key, else None."""
    return root.name if root.name and is_system_folder(cfg, root.name) else None


def split(cfg: Config, root: Path) -> tuple[list[str], list[Path], list[Path]]:
    """(system folder names, other sub-folders, loose files directly in root)."""
    from .scanner import IGNORE_EXTS
    systems, others = [], []
    for name in library_folders(root):
        (systems if is_system_folder(cfg, name) else others).append(name)
    try:
        loose = sorted(p for p in root.iterdir()
                       if p.is_file() and not p.name.startswith(".") and p.suffix.lower() not in IGNORE_EXTS)
    except OSError:
        loose = []
    return systems, [root / o for o in others], loose


def other_label(cfg: Config, root: Path) -> tuple[str, str]:
    """(label, DAT column text) for the OTHER row."""
    rs = root_system(cfg, root)
    if rs:
        return f"{root.name} (this folder)", "its own DAT"
    return OTHER_LABEL, "identified using all your DATs"


_SHARED = IndexCache()


def shared_index(dats: list[str]):
    """One parsed index of these DATs for the whole program (loading all of them takes a moment)."""
    return _SHARED.get(dats) if dats else None


def dats_of(index, sysid: str, family: bool = False) -> set[str]:
    """Names of the DATs in an index that belong to a system (``family``: or to one whose games look the same)."""
    from .scanner import same_family
    out = set()
    for d in index.dats:
        s = system_for_dat(d.name)
        if s and (s == sysid or family and same_family(s, sysid)):
            out.add(d.name)
    return out


def all_dats(cfg: Config) -> list[str]:
    found = {str(p) for p in DAT_DIR.glob("*") if p.suffix.lower() in (".dat", ".xml", ".zip")} \
        if DAT_DIR.is_dir() else set()
    for s in (cfg["systems"] or {}).values():
        found.update(d for d in (s or {}).get("dats", []) if Path(d).is_file())
    return sorted(found)


def system_for_dat(dat_name: str) -> str | None:
    """Catalog id for a DAT header name ("Nintendo - Game Boy", "Sony - PlayStation")."""
    n = dat_name.lower().strip()
    for s in CATALOG:
        if n in (s.ref.lower(), s.name.lower()) or n.startswith(s.ref.lower() + " ("):
            return s.id
    return None


def place_of(cfg: Config, path: Path) -> str:
    """The system of the nearest folder above ``path`` that is one ("" = none). Inside the library every folder up to
    it counts, elsewhere only the three nearest."""
    root = Path(cfg["library_root"] or "/nonexistent").expanduser()
    above = list(path.parents)
    above = above[:above.index(root)] if root in above else above[:3]
    for p in above:
        mapped = ((cfg["systems"] or {}).get(p.name) or {}).get("system") if p.parent == root else ""
        g = guess_system(p.name)
        if mapped or g:
            return mapped or g.id
    return ""


def collect(cfg: Config, root: Path, keys: list[str], index_cache: IndexCache,
            progress: Callable[[str], None] = lambda s: None, place: str = ""):
    """(items, indexes) for the checked groups. Items in the OTHER group get system == OTHER."""
    from .scanner import _items_in, enumerate_system
    systems, others, _loose = split(cfg, root)
    items, indexes = [], {}
    for key in keys:
        if key == OTHER:
            progress("Listing loose files and other folders…")
            rs = root_system(cfg, root)
            own = (cfg["systems"].get(rs) or {}).get("system") or getattr(guess_system(rs), "id", "") if rs else place
            items += _items_in(OTHER, root, skip_dirs=set(systems), expected=own)
            dats = (cfg["systems"].get(rs) or {}).get("dats", []) if rs else []
            needed = any(it.system == OTHER for it in items)
            if not needed:
                dats = []
            elif not dats:
                dats = all_dats(cfg)
                if dats:
                    progress(f"Loading all {len(dats)} DATs (first time takes a few seconds)…")
            indexes[OTHER] = (index_cache.get(dats) if rs else shared_index(dats)) if dats else None
        else:
            progress(f"Listing {key}…")
            items += enumerate_system(key, root / key, (cfg["systems"].get(key) or {}).get("system") or "")
            dats = cfg.system(key)["dats"]
            if dats:
                progress(f"Loading DAT for {key}…")
            indexes[key] = index_cache.get(dats)
    return items, indexes


def sheet_of(path: Path) -> Path | None:
    """The .cue / .gdi in the same folder that lists this file as one of its tracks."""
    from .scanner import _cue_parts, _gdi_parts
    if path.suffix.lower() not in (".bin", ".img", ".raw", ".iso", ".wav"):
        return None
    try:
        for f in sorted(path.parent.iterdir()):
            parts = _cue_parts(f) if f.suffix.lower() == ".cue" else _gdi_parts(f) if f.suffix.lower() == ".gdi" else []
            if path in parts:
                return f
    except OSError:
        pass
    return None


def item_for_file(path: Path, cfg: Config | None = None):
    """A scan item for one picked file (ROM, archive, CHD, or a .cue/.gdi with its tracks)."""
    from .config import get_config
    from .scanner import Item, _cue_parts, _gdi_parts, kind_for
    path = sheet_of(path) or path              # one track of a disc: the whole disc
    ext = path.suffix.lower()
    parts = _cue_parts(path) if ext == ".cue" else _gdi_parts(path) if ext == ".gdi" else None
    expected = place_of(cfg or get_config(), path)
    if parts is not None:
        return Item(OTHER, path, "cueset", [p for p in parts if p.exists()], expected=expected)
    return Item(OTHER, path, kind_for(ext), expected=expected)


def collect_paths(cfg: Config, paths: list[Path], index_cache: IndexCache,
                  progress: Callable[[str], None] = lambda s: None):
    """(items, indexes) for files/folders picked anywhere (Check file / Check folder / drag and drop).
    Folders are split like a library; single files and everything outside system folders are identified
    against all DATs."""
    items, indexes = [], {}
    for p in paths:
        if p.is_dir():
            systems, _others, _loose = split(cfg, p)
            its, idx = collect(cfg, p, systems + [OTHER], index_cache, progress, place_of(cfg, p))
            items += its
            indexes.update({k: v for k, v in idx.items() if k != OTHER})
        elif p.is_file():
            items.append(item_for_file(p, cfg))
    if any(it.system == OTHER for it in items):
        dats = all_dats(cfg)
        if dats:
            progress(f"Loading all {len(dats)} DATs…")
        indexes[OTHER] = shared_index(dats)
    seen, unique = set(), []
    for it in items:                       # the same file picked twice, or a file inside a picked folder
        if it.path not in seen:
            seen.add(it.path)
            unique.append(it)
    return unique, indexes


def rule_folders(cfg: Config, root: Path, keys: list[str]) -> list[tuple[str, Path, set[str]]]:
    """(key, folder, names to skip) for the Renamer's cleanup rules."""
    systems, others, _ = split(cfg, root)
    out = []
    for key in keys:
        if key == OTHER:
            out.append((OTHER, root, set(systems)))
            out += [(OTHER, o, set()) for o in others]
        else:
            out.append((key, root / key, set()))
    return out


def package_folders(cfg: Config, paths: list[Path]) -> list[tuple[str, Path, set[str]]]:
    """Folders to read console packages from for these picked files / folders: the whole PS4 / Switch folder they are
    in (names come from a game and its updates together), else the folder itself."""
    out: list[tuple[str, Path, set[str]]] = []
    for p in paths:
        home = None
        for q in ([p] if p.is_dir() else []) + list(p.parents)[:4]:
            g = guess_system(q.name)
            if g and g.source == "package":
                home = q
                break
        if home is not None:
            out.append((home.name, home, set()))
        elif p.is_dir():
            out += rule_folders(cfg, p, split(cfg, p)[0] + [OTHER])
        else:
            out.append((OTHER, p.parent, set()))
    return list({(k, f): (k, f, s) for k, f, s in out}.values())


__all__ = ["OTHER", "OTHER_LABEL", "split", "collect", "all_dats", "system_for_dat", "rule_folders",
           "other_label", "is_system_folder", "root_system", "BY_ID"]
