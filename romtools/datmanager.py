"""Downloading, importing and loading DAT files per library folder."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import DAT_DIR, Config
from .dat import DatIndex, parse_dat
from .systems import BY_ID, CATALOG, System, guess_system

UA = "romtools/1.0"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "dat"


def dat_filename(system: System) -> str:
    return _safe(system.ref if system.source == "no-intro" else system.name) + ".dat"


def download(system: System, timeout: int = 120) -> Path:
    """Fetch the system's DAT into DAT_DIR and return its path."""
    import time
    out = DAT_DIR / dat_filename(system)
    tag = {}
    tmp = _fetch(system, out.with_suffix(".part"), timeout, tag)
    os.replace(tmp, out)
    _remember_dat(out.name, tag=tag.get("tag", ""), size=out.stat().st_size, checked=time.time())
    return out


def _fetch(system: System, dest: Path, timeout: int, tag: dict | None = None) -> Path:
    """Download a DAT to ``dest`` and make sure it is one."""
    DAT_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(system.url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if tag is not None:
            tag["tag"] = resp.headers.get("ETag") or resp.headers.get("Content-Disposition") or ""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".dat", ".xml"))]
            if not names:
                raise RuntimeError("downloaded zip has no DAT inside")
            data = zf.read(names[0])
    head = data[:512].lstrip()
    if not (head.startswith(b"<?xml") or head.startswith(b"<datafile") or head.startswith(b"clrmamepro")):
        raise RuntimeError("server did not return a DAT file (site down or rate-limited?)")
    dest.write_bytes(data)
    try:
        parse_dat(dest)  # validate before replacing a working DAT
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest


def import_file(src: Path) -> Path:
    DAT_DIR.mkdir(parents=True, exist_ok=True)
    dest = DAT_DIR / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    parse_dat(dest)
    return dest


def library_folders(root: Path) -> list[str]:
    try:
        return sorted((p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=str.lower)
    except OSError:
        return []


def ensure_mapping(cfg: Config, folders: list[str]) -> None:
    """Give every new folder a guessed catalog system (user can change it)."""
    changed = False
    for f in folders:
        s = cfg.system(f)
        g = guess_system(f)
        # systems and folder names that are recognised now but weren't when the folder was first seen (unless "none"
        # was chosen for it)
        if s.get("_guessed") and not s["system"] and not s.get("_none") and not s["dats"] and g:
            s["system"] = g.id
            cand = DAT_DIR / dat_filename(g)
            if g.url and cand.exists():
                s["dats"] = [str(cand)]
            changed = True
        if not s.get("_guessed"):
            g = guess_system(f)
            if g and not s["system"]:
                s["system"] = g.id
            if g and not s["dats"]:
                cand = DAT_DIR / dat_filename(g)
                if cand.exists():
                    s["dats"] = [str(cand)]
            s["_guessed"] = True
            changed = True
    if changed:
        cfg.save()


def system_for(cfg: Config, folder: str) -> System | None:
    return BY_ID.get(cfg.system(folder).get("system", ""))


# ---------------------------------------------------------------- is there a newer DAT?

@dataclass
class DatUpdate:
    system: System
    path: Path
    state: str                  # "new" | "current" | "error"
    note: str = ""
    new_file: Path | None = None        # the newer DAT, already downloaded and checked
    added: int = 0
    removed: int = 0
    changed: int = 0


def downloaded_dats() -> list[tuple[System, Path]]:
    """DATs fetched from Redump / No-Intro (not the ones added by hand)."""
    return [(s, DAT_DIR / dat_filename(s)) for s in CATALOG if s.url and (DAT_DIR / dat_filename(s)).is_file()]


def oldest_check_days() -> int | None:
    """Days since the DAT that was checked longest ago was downloaded or checked; None without DATs."""
    import time
    meta = _dat_meta()
    ages = [time.time() - max(p.stat().st_mtime, meta.get(p.name, {}).get("checked", 0)) for _s, p in downloaded_dats()]
    return int(max(ages) // 86400) if ages else None


def _dat_meta() -> dict:
    try:
        return json.loads((DAT_DIR / "dat-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember_dat(name: str, **vals) -> None:
    meta = _dat_meta()
    meta.setdefault(name, {}).update(vals)
    tmp = DAT_DIR / "dat-info.tmp"
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(tmp, DAT_DIR / "dat-info.json")


def _games_of(path: Path) -> dict[str, tuple]:
    return {g.name: tuple(sorted((r.sha1 or r.md5 or r.crc or "", r.size or 0) for r in g.data_roms))
            for g in parse_dat(path).games}


def check_update(system: System, timeout: int = 60) -> DatUpdate:
    """Compare one downloaded DAT with the server's. The server is first asked what version it has; only when that isn't
    the remembered one is the DAT downloaded (next to yours) and compared game by game. Nothing is replaced here."""
    import time
    path = DAT_DIR / dat_filename(system)
    meta = _dat_meta().get(path.name, {})
    try:
        req = urllib.request.Request(system.url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tag = resp.headers.get("ETag") or resp.headers.get("Content-Disposition") or ""
    except OSError:
        tag = ""                        # not every server answers this: download and compare
    if tag and tag == meta.get("tag") and meta.get("size") == path.stat().st_size:
        _remember_dat(path.name, checked=time.time())
        return DatUpdate(system, path, "current", "up to date")
    try:
        new = _fetch(system, DAT_DIR / (path.stem + ".new"), timeout)
        old_games, new_games = _games_of(path), _games_of(new)
    except Exception as exc:  # noqa: BLE001
        return DatUpdate(system, path, "error", str(exc))
    if not new_games:
        new.unlink(missing_ok=True)
        return DatUpdate(system, path, "error", "the server's DAT is empty")
    added, removed = len(new_games.keys() - old_games.keys()), len(old_games.keys() - new_games.keys())
    changed = sum(old_games[g] != new_games[g] for g in old_games.keys() & new_games.keys())
    if not (added or removed or changed):
        os.replace(new, path)           # the same games: keep the server's copy, so its version is known from now on
        _remember_dat(path.name, tag=tag, size=path.stat().st_size, checked=time.time())
        return DatUpdate(system, path, "current", "up to date")
    _remember_dat(path.name, checked=time.time(), new_tag=tag)
    what = ", ".join(f"{n} {w}" for n, w in ((added, "games added"), (changed, "changed"), (removed, "removed or renamed"))
                     if n)
    return DatUpdate(system, path, "new", what, new, added, removed, changed)


def apply_update(u: DatUpdate) -> None:
    if u.new_file is not None:
        os.replace(u.new_file, u.path)
        _remember_dat(u.path.name, tag=_dat_meta().get(u.path.name, {}).get("new_tag", ""), size=u.path.stat().st_size)


def discard_update(u: DatUpdate) -> None:
    if u.new_file is not None:
        u.new_file.unlink(missing_ok=True)


class IndexCache:
    """Parsed DAT indexes, re-parsed only when the DAT files change."""

    def __init__(self) -> None:
        self._cache: dict[tuple, DatIndex] = {}
        self._lock = threading.Lock()

    def get(self, dat_paths: list[str]) -> DatIndex | None:
        paths = [Path(p) for p in dat_paths if Path(p).is_file()]
        if not paths:
            return None
        key = tuple((str(p), p.stat().st_mtime) for p in paths)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = DatIndex([_parsed(p) for p in paths])
            return self._cache[key]


_PARSED: dict[Path, tuple[float, object]] = {}


def _parsed(path: Path):
    """A DAT, parsed once: its own index and the one over all DATs share the entries."""
    mtime = path.stat().st_mtime
    hit = _PARSED.get(path)
    if hit is None or hit[0] != mtime:
        hit = _PARSED[path] = (mtime, parse_dat(path))
    return hit[1]
