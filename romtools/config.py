"""Persistent settings, stored as JSON under the XDG config directory."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "romtools"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "romtools"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "romtools"
DAT_DIR = DATA_DIR / "dats"
JOURNAL_DIR = DATA_DIR / "rename-journal"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _default_library() -> str:
    for cand in (Path("/Tank/Romm/roms"), Path.home() / "Tank/Romm/roms"):
        if cand.is_dir():
            return str(cand)
    return str(Path.home())


DEFAULTS = {
    "library_root": None,           # filled lazily by _default_library()
    "tmp_dir": "/var/tmp/romtools",  # local disk, not tmpfs: DVD CHDs extract to ~4-8 GB
    "workers": 4,                    # parallel file hashing (network shares like 2-6)
    "chd_workers": 2,                # parallel CHD extractions
    "use_cache": True,
    "temp_mode": "ram",              # "ram": stream CHDs through /dev/shm (no SSD writes) | "ssd": temp files
    # folder name (e.g. "psx") -> {"system": catalog id or "", "dats": [paths]}
    "systems": {},
    "rename_use_dat_ext": False,
    "window_state": {},
}


class Config:
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass
        if not self.data.get("library_root"):
            self.data["library_root"] = _default_library()

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
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, CONFIG_FILE)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
