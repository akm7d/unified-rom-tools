"""SQLite cache of computed hashes, keyed by path + size + mtime.

Re-scans only re-hash files that changed, so a second pass over a large
CHD library takes seconds instead of hours.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .config import CACHE_DIR
from .hashing import Entry


class HashCache:
    def __init__(self, path: Path | None = None) -> None:
        path = path or CACHE_DIR / "hashes.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, "
                         "mtime REAL, kind TEXT, extra TEXT, entries TEXT)")
        self._db.commit()

    def get(self, path: Path, size: int, mtime: float) -> tuple[str, dict, list[Entry]] | None:
        with self._lock:
            row = self._db.execute("SELECT size, mtime, kind, extra, entries FROM hashes WHERE path=?",
                                   (str(path),)).fetchone()
        if not row or row[0] != size or abs(row[1] - mtime) > 1e-3:
            return None
        try:
            return row[2], json.loads(row[3] or "{}"), [Entry.from_dict(d) for d in json.loads(row[4])]
        except (ValueError, KeyError, TypeError):
            return None

    def put(self, path: Path, size: int, mtime: float, kind: str, extra: dict, entries: list[Entry]) -> None:
        data = json.dumps([e.to_dict() for e in entries])
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?,?,?)",
                             (str(path), size, mtime, kind, json.dumps(extra), data))
            self._db.commit()

    def rename(self, old: Path, new: Path) -> None:
        with self._lock:
            self._db.execute("UPDATE OR REPLACE hashes SET path=? WHERE path=?", (str(new), str(old)))
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM hashes")
            self._db.commit()
