"""Is there a newer official update than the one you have?

* Switch: the public, key-free title database (github.com/blawar/titledb, ``versions.json``: base Title ID ->
  {version: release date}). Downloaded once a day into the cache folder (~1.7 MB); nothing about your library
  is sent anywhere.
* PS4: there's no public source that can be read without Sony's own signing key, so instead the Verifier offers
  to open the game's patch page (orbispatches.com) in your browser.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR

TITLEDB_URL = "https://raw.githubusercontent.com/blawar/titledb/master/versions.json"
PS4_PATCH_PAGE = "https://orbispatches.com/{title_id}"
MAX_AGE = 24 * 3600


@dataclass
class Latest:
    base_id: str
    version: int          # raw Switch version (65536 per update)
    date: str             # release date "YYYY-MM-DD"


def _versions_file() -> Path:
    return CACHE_DIR / "titledb-versions.json"


def switch_versions(force: bool = False, timeout: int = 60) -> dict:
    """{base id (lower-case): {version str: date}}, refreshed when older than a day. Falls back to the
    cached copy when offline."""
    f = _versions_file()
    fresh = f.is_file() and time.time() - f.stat().st_mtime < MAX_AGE
    if force or not fresh:
        try:
            req = urllib.request.Request(TITLEDB_URL, headers={"User-Agent": "romtools"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            json.loads(data)                       # only keep it if it parses
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(f)
        except (OSError, ValueError):
            if not f.is_file():
                raise
    return json.loads(f.read_text(encoding="utf-8"))


def switch_latest(base_id: str, db: dict) -> Latest | None:
    vers = db.get(base_id.lower()) or {}
    if not vers:
        return None
    v = max(vers, key=lambda k: int(k))
    return Latest(base_id.upper(), int(v), vers[v])


def ps4_patch_page(title_id: str) -> str:
    return PS4_PATCH_PAGE.format(title_id=title_id)
