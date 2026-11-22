"""Persistent settings, stored as JSON under the XDG config directory (%APPDATA% on Windows)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .compat import IS_WIN, app_dir, default_tmp_dir, portable_root, ram_mode_supported, romtools_dirs

CONFIG_DIR, DATA_DIR, CACHE_DIR = romtools_dirs()
PORTABLE = portable_root() is not None

# In portable mode, paths inside the portable folder are saved relative to it ({data}/..., {app}/...),
# so DATs, a downloaded chdman or a library next to the programs keep working after the folder moves.
_TOKENS = (("{data}", DATA_DIR), ("{app}", app_dir()))


def pack_path(p: str) -> str:
    if not PORTABLE or not p or p.startswith("{"):
        return p
    try:
        path = Path(p).resolve()
    except OSError:
        return p
    for token, base in _TOKENS:
        try:
            return token + "/" + path.relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return p


def unpack_path(p: str) -> str:
    for token, base in _TOKENS:
        if p.startswith(token + "/"):
            return str(base / p[len(token) + 1:])
    return p


def _map_paths(data: dict, fn) -> dict:
    out = json.loads(json.dumps(data))
    for key in ("library_root", "tmp_dir", "chdman_path"):
        if isinstance(out.get(key), str) and out[key] not in ("system",):
            out[key] = fn(out[key])
    for s in (out.get("systems") or {}).values():
        if isinstance(s, dict) and isinstance(s.get("dats"), list):
            s["dats"] = [fn(d) for d in s["dats"]]
    return out
DAT_DIR = DATA_DIR / "dats"
JOURNAL_DIR = DATA_DIR / "rename-journal"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _default_library() -> str:
    cands = [] if IS_WIN else [Path("/Tank/Romm/roms"), Path.home() / "Tank/Romm/roms"]
    for cand in cands:
        if cand.is_dir():
            return str(cand)
    return str(Path.home())


DEFAULTS = {
    "library_root": None,           # filled lazily by _default_library()
    "tmp_dir": default_tmp_dir(),    # SSD-mode temp folder; local disk, DVD CHDs extract to ~4-8 GB
    "workers": 4,                    # parallel file hashing (network shares like 2-6)
    "chd_workers": 2,                # parallel CHD extractions
    "use_cache": True,
    # "ram": stream CHDs through /dev/shm (no SSD writes) | "ssd": temp files. Windows is always "ssd".
    "temp_mode": "ram" if ram_mode_supported() else "ssd",
    "chdman_path": "system",         # "system" (bundled/PATH) or an absolute path to a chdman binary
    # folder name (e.g. "psx") -> {"system": catalog id or "", "dats": [paths]}
    "systems": {},
    "rename_use_dat_ext": False,
    "theme": "",                     # "" = platform default (dark on Windows, follow system on Linux)
    "window_state": {},
}


class Config:
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                self.data.update(_map_paths(json.load(fh), unpack_path))
        except (OSError, ValueError):
            pass
        if not self.data.get("library_root"):
            self.data["library_root"] = _default_library()
        if not ram_mode_supported():
            self.data["temp_mode"] = "ssd"

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def system(self, folder: str) -> dict:
        s = self.data.setdefault("systems", {}).setdefault(folder, {})
        s.setdefault("system", "")
        s.setdefault("dats", [])
        return s

    def save(self) -> None:
        with self._lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(_map_paths(self.data, pack_path), fh, indent=2)
            os.replace(tmp, CONFIG_FILE)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
