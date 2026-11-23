"""Downloading, importing and loading DAT files per library folder."""

from __future__ import annotations

import io
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

from .config import DAT_DIR, Config
from .dat import DatIndex, parse_dat
from .systems import BY_ID, System, guess_system

UA = "romtools/1.0"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "dat"


def dat_filename(system: System) -> str:
    return _safe(system.ref if system.source == "no-intro" else system.name) + ".dat"


def download(system: System, timeout: int = 120) -> Path:
    """Fetch the system's DAT into DAT_DIR and return its path."""
    DAT_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(system.url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".dat", ".xml"))]
            if not names:
                raise RuntimeError("downloaded zip has no DAT inside")
            data = zf.read(names[0])
    head = data[:512].lstrip()
    if not (head.startswith(b"<?xml") or head.startswith(b"<datafile") or head.startswith(b"clrmamepro")):
        raise RuntimeError("server did not return a DAT file (site down or rate-limited?)")
    out = DAT_DIR / dat_filename(system)
    tmp = out.with_suffix(".part")
    tmp.write_bytes(data)
    parse_dat(tmp)  # validate before replacing a working DAT
    tmp.replace(out)
    return out


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
        # console-package systems were added later: map folders set up before (unless "none" was chosen)
        if s.get("_guessed") and not s["system"] and not s.get("_none") and g and g.source == "package":
            s["system"] = g.id
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


class IndexCache:
    """Parsed DAT indexes, re-parsed only when the DAT files change."""

    def __init__(self) -> None:
        self._cache: dict[tuple, DatIndex] = {}

    def get(self, dat_paths: list[str]) -> DatIndex | None:
        paths = [Path(p) for p in dat_paths if Path(p).is_file()]
        if not paths:
            return None
        key = tuple((str(p), p.stat().st_mtime) for p in paths)
        if key not in self._cache:
            self._cache[key] = DatIndex([parse_dat(p) for p in paths])
        return self._cache[key]
