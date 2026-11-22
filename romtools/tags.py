"""ROM hack / translation tags.

A modified ROM can never match No-Intro or Redump, so without a tag it shows up as "Bad dump" (when it keeps
the original's name) or "Unknown". A tag moves it to the "Modified" status instead.

Tags come from:
  * your own marks (right-click → Mark as…), stored by file *content* (SHA-1), so they survive renames/moves;
  * the file name, using the usual community conventions ([T-En by …], (Hack), Undub, …).
A mark of "" means "not modified" and overrides name detection.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .config import DATA_DIR

TAGS = {
    "Translation": "Fan translation into another language",
    "Hack": "Gameplay / content hack (new levels, mechanics, difficulty…)",
    "Undub": "Original Japanese voices restored in a localized release",
    "Improvement": "Bug fixes, restorations, QoL or graphics patches",
    "Homebrew": "Unofficial game or demo, not a modified commercial ROM",
    "Trainer": "Cheats / trainer built into the ROM",
    "Prototype": "Unreleased beta / prototype not in the DAT",
    "Modified": "Other modification",
}

# (tag, pattern) — first match wins. Case-insensitive, matched against the file/folder name.
_NAME_RULES = [
    ("Translation", r"\[T[-+]?[A-Z][a-z]{1,2}\b|\(T[-+][A-Z][a-z]{1,2}\)|\btranslat|\b(eng(lish)?|spanish|french|"
                    r"german|italian|portuguese)[ _-]?(patch(ed)?|trans)|\bpatched[ _-]?(eng|en)\b"),
    ("Undub", r"undub"),
    # not a bare "hack": ".hack//Infection" etc. are real games
    ("Hack", r"\(hack\)|\[h\d*[\] ]|\bhack(ed)?[ _-]+(v\d|by\b)|\brom ?hack|\[hack"),
    ("Improvement", r"\[f\d*\b|\b(bug ?fix|restoration|improvement|enhanced|qol)\b"),
    ("Trainer", r"\[t\d*\]|\btrainer\b|\(cheat"),
    ("Homebrew", r"\bhomebrew\b|\(pd\)|\(aftermarket\)"),
    ("Prototype", r"\((proto|prototype|beta)\b"),
    ("Modified", r"\bpatched\b|\(mod(ded)?\)|\bmodded\b"),
]
_COMPILED = [(t, re.compile(p, re.I)) for t, p in _NAME_RULES]


def tag_from_name(name: str) -> tuple[str, str] | None:
    """(tag, the text that matched) or None."""
    for tag, rx in _COMPILED:
        m = rx.search(name)
        if m:
            return tag, m.group(0)
    return None


def content_key(result) -> str:
    """Stable key for a scan result: SHA-1s of its data, or the path when nothing was hashed."""
    sha = [er.entry.full.sha1 for er in getattr(result, "entries", [])][:8]
    return "sha1:" + ",".join(sha) if sha else "path:" + str(result.item.path)


class MarkStore:
    """Your manual tags, saved in ~/.local/share/romtools/marks.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DATA_DIR / "marks.json"
        self._lock = threading.Lock()
        self.marks: dict[str, dict] = {}
        try:
            self.marks = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.marks = {}

    def get(self, key: str) -> str | None:
        m = self.marks.get(key)
        return None if m is None else m.get("tag", "")

    def set(self, key: str, tag: str | None, name: str = "") -> None:
        """tag None = forget the mark (back to name detection); "" = explicitly not modified."""
        with self._lock:
            if tag is None:
                self.marks.pop(key, None)
            else:
                self.marks[key] = {"tag": tag, "name": name}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.marks, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)


def apply(result, store: MarkStore | None, modified_status: str, taggable: set[str]) -> None:
    """Set result.tag / tag_source and move it to the Modified status when tagged."""
    if not hasattr(result, "orig_status") or result.orig_status is None:
        result.orig_status = result.status
        result.orig_note = result.note
    status, note = result.orig_status, result.orig_note
    result.tag, result.tag_source = "", ""
    mark = store.get(content_key(result)) if store else None
    if mark:
        result.tag, result.tag_source = mark, "marked by you"
    elif mark is None and status in taggable:
        hit = tag_from_name(result.item.path.name)
        if hit:
            result.tag, result.tag_source = hit[0], f"from name: “{hit[1]}”"
    if result.tag:
        result.status = modified_status
        why = {"Bad dump": "the original game's name, different data", "Unknown": "not in the DAT",
               "Partial": "only partly matches the DAT"}.get(status, status.lower())
        result.note = f"{result.tag} ({result.tag_source}) — {why}"
    else:
        result.status, result.note = status, note
