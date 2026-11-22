"""Hashing the disc inside compressed GameCube / Wii images (RVZ, CISO, GCZ, WIA, WBFS) with nodtool.

Redump lists the plain disc, so the compressed file's own hash never matches. ``nodtool verify`` streams the
image, decompresses it in memory and prints the CRC32 and SHA-1 of the whole disc: no temporary files, so
nothing is written to the SSD whatever mode is chosen.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from .hashing import Entry, HashSet

DISC_EXTS = {".rvz", ".ciso", ".gcz", ".wia", ".wbfs"}

_DIGEST = re.compile(r"^\s*(CRC32|MD5|SHA-1)\s*:\s*([0-9a-fA-F]+)")
_TITLE = re.compile(r"^\s*(Title|Game ID|Format)\s*:\s*(.+?)\s*$")


class NodError(RuntimeError):
    pass


def available() -> bool:
    from .compat import find_tool
    return find_tool("nodtool") is not None


def parse_verify(lines: list[str]) -> dict:
    """{'crc32', 'sha1', 'md5'?, 'title', 'game_id', 'format'} from ``nodtool verify`` output."""
    out: dict = {}
    for ln in lines:
        m = _DIGEST.match(ln)
        if m:
            out[{"CRC32": "crc32", "MD5": "md5", "SHA-1": "sha1"}[m.group(1)]] = m.group(2).lower()
            continue
        m = _TITLE.match(ln)
        if m:
            out.setdefault(m.group(1).lower().replace(" ", "_"), m.group(2))
    return out


def disc_hash(path: Path, cancel: threading.Event | None = None, on_line=None) -> tuple[Entry, dict]:
    """(Entry for the disc inside ``path``, info dict). The Entry is named like the plain image Redump lists
    (``<stem>.iso``); its size is unknown until it is matched (0)."""
    from .chdman_tool import Process
    from .compat import find_tool
    exe = find_tool("nodtool")
    if not exe:
        from .compat import FROZEN
        raise NodError("nodtool not found, needed to check RVZ/CISO/GCZ/WIA/WBFS files" +
                       (" (the build is incomplete: tools/ is missing it)" if FROZEN else
                        " - run ./install.sh (or `make tools`) in the romtools folder to fetch it"))
    lines: list[str] = []

    def line(s: str):
        lines.append(s)
        if on_line:
            on_line(s)

    p = Process([exe, "verify", str(path)], line)
    stop = threading.Event()
    if cancel is not None:
        def watch():
            while not stop.is_set():
                if cancel.wait(0.5):
                    p.cancel()
                    return
        threading.Thread(target=watch, daemon=True).start()
    try:
        rc = p.run()
    finally:
        stop.set()
    if cancel is not None and cancel.is_set():
        from .hashing import Cancelled
        raise Cancelled()
    info = parse_verify(lines)
    if rc != 0 or "sha1" not in info or "crc32" not in info:
        tail = next((ln for ln in reversed(lines) if ln.strip() and not ln.startswith("$ ")), f"exit code {rc}")
        raise NodError(f"nodtool couldn't read the disc: {tail}")
    info["container"] = path.suffix.lower().lstrip(".")
    return Entry(path.stem + ".iso", HashSet(0, info["crc32"], info.get("md5", ""), info["sha1"])), info
