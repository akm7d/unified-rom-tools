"""Persistent settings, stored as JSON under the XDG config directory (%APPDATA% on Windows)."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

from .compat import IS_WIN, app_dir, default_tmp_dir, file_lock, portable_root, ram_mode_supported, romtools_dirs

CONFIG_DIR, DATA_DIR, CACHE_DIR = romtools_dirs()
PORTABLE = portable_root() is not None

# In portable mode, paths inside the portable folder are saved relative to it ({data}/..., {app}/...),
# so DATs, a downloaded chdman or a library next to the programs keep working after the folder moves.
_TOKENS = (("{data}", DATA_DIR), ("{app}", app_dir()))


def pack_path(p: str) -> str:
    if not PORTABLE or not os.path.isabs(p):
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
    if PORTABLE and (m := _OTHER_VERSION.match(p)):     # saved by a version that didn't keep all paths relative
        mine = DATA_DIR / m[2]
        if Path(m[1]) == DATA_DIR.parent.parent and mine.exists():
            return str(mine)
    return p


# a path in the data folder of another version next to this one
_OTHER_VERSION = re.compile(r"^(.*)[/\\]romtools-[\d.]+-(?:linux|windows)-x64[/\\]data[/\\](.+)$")


def _map_deep(v, fn):
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [_map_deep(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _map_deep(x, fn) for k, x in v.items()}
    return v


def _map_paths(data: dict, fn) -> dict:
    out = json.loads(json.dumps(data))
    for key in ("library_root", "tmp_dir", "chdman_path", "switch_keys", "title_keys", "window_state"):
        if key in out:
            out[key] = _map_deep(out[key], fn)          # (only full paths inside the portable folder are changed)
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
    "switch_keys": "",               # your prod.keys: reads Switch packages (all apps) and NSZ conversion
    "title_keys": "",                # your title.keys: title keys for Switch content whose package has no ticket
    "theme": "",                     # "" = platform default (dark on Windows, follow system on Linux)
    "check_updates_at_launch": True,  # look for a newer romtools release when an app starts (at most twice a day)
    "window_state": {},
}


_MISSING = object()


def _read_settings() -> dict | None:
    """The settings on disk, complete (defaults filled in); None if there are none or they can't be read."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        if not isinstance(saved, dict):
            raise ValueError("not a settings file")
    except (OSError, ValueError):
        return None
    return _complete({**copy.deepcopy(DEFAULTS), **_map_paths(saved, unpack_path)})


def _complete(data: dict) -> dict:
    if not data.get("library_root"):
        data["library_root"] = _default_library()
    if not ram_mode_supported():
        data["temp_mode"] = "ssd"
    return data


def _merge(mine: dict, base: dict, theirs: dict) -> None:
    """Bring ``mine`` up to date: what was changed here since ``base`` stays, everything else is taken from ``theirs``
    (what another window saved meanwhile). Nested settings are merged one by one, in place."""
    for k in {**mine, **base, **theirs}:
        m, b, t = mine.get(k, _MISSING), base.get(k, _MISSING), theirs.get(k, _MISSING)
        if isinstance(m, dict) and isinstance(t, dict):
            _merge(m, b if isinstance(b, dict) else {}, t)
        elif m == b:                                # not touched here
            if t is _MISSING:
                mine.pop(k, None)
            else:
                mine[k] = copy.deepcopy(t)


class Config:
    """The settings, shared by all three apps. Each window keeps its own copy; saving only writes what this window
    changed, so it never undoes what another one saved meanwhile."""

    _lock = threading.Lock()

    def __init__(self) -> None:
        data = _read_settings()
        if data is None and CONFIG_FILE.exists():
            self._keep_damaged()
        self.data = data if data is not None else _complete(copy.deepcopy(DEFAULTS))
        self._base = copy.deepcopy(self.data)       # as last read from / written to the disk

    @staticmethod
    def _keep_damaged() -> None:
        """Settings that can't be read are kept under another name instead of being overwritten."""
        n = 1
        while (dest := CONFIG_FILE.with_name(f"config.damaged-{n}.json")).exists():
            n += 1
        try:
            os.rename(CONFIG_FILE, dest)
        except OSError:
            pass

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def system(self, folder: str) -> dict:
        s = self.data.setdefault("systems", {}).setdefault(folder, {})
        b = self._base.setdefault("systems", {}).setdefault(folder, {})
        for k, empty in (("system", ""), ("dats", [])):
            if k not in s:                          # only filled in, not a change made here
                s[k] = copy.copy(empty)
                b.setdefault(k, copy.copy(empty))
        return s

    def save(self) -> None:
        with self._lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with file_lock(CONFIG_FILE):
                disk = _read_settings()
                if disk is not None:
                    _merge(self.data, self._base, disk)
                tmp = CONFIG_FILE.with_name(f"config.{os.getpid()}.tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(_map_paths(self.data, pack_path), fh, indent=2)
                os.replace(tmp, CONFIG_FILE)
                self._base = copy.deepcopy(self.data)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
