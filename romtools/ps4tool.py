"""The ShadPs4Plus PKG Extractor (BETA feature): downloaded on first use, never bundled.

It's a separate GPL-2.0 program (github.com/AzaharPlus/shadPS4Plus). It embeds key constants that romtools doesn't
distribute, so romtools' builds and repository don't contain it: with the user's OK it is fetched from its official
GitHub release, checked against a pinned SHA-256, and kept in the data folder.
"""

from __future__ import annotations

import hashlib
import io
import os
import urllib.request
import zipfile
from pathlib import Path

from .compat import IS_WIN
from .config import DATA_DIR

VERSION = "1.1"             # 1.0 corrupts files in packages over 2 GB
PAGE = "https://github.com/AzaharPlus/shadPS4Plus/releases/tag/PKG_EXTRACTOR_1_1"
_BASE = "https://github.com/AzaharPlus/shadPS4Plus/releases/download/PKG_EXTRACTOR_1_1/ShadPs4Plus-PkgExtractor-1.1-"
PINS = {
    "linux": (_BASE + "linux.zip", "6dc4f867ced673c940c5c7538024a455bb00a3d3ab0f758db6b96184567bb677",
              ("pkg_extractor.AppImage",)),
    "windows": (_BASE + "windows.zip", "a6a6dee84c1283949245c868e580ff8d0049d0e73df9de3096460ee26c6f04d7",
                ("pkg_extractor.exe", "libc++.dll", "zlib1.dll")),
}


def _pin():
    return PINS["windows" if IS_WIN else "linux"]


def folder() -> Path:
    return DATA_DIR / "tools" / f"pkg_extractor-{VERSION}"


def program() -> Path | None:
    """The installed extractor, or None (then offer ``install``)."""
    names = _pin()[2]
    return folder() / names[0] if all((folder() / n).is_file() for n in names) else None


def env() -> dict:
    """The Linux download is an AppImage: run it without needing FUSE."""
    return {**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"}


def install(data: bytes | None = None) -> Path:
    """Download (or take ``data``), check the pinned SHA-256, and unpack only the expected files."""
    url, sha, names = _pin()
    if data is None:
        req = urllib.request.Request(url, headers={"User-Agent": "romtools"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read(32 << 20)
    if hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError("the download doesn't match its pinned checksum - nothing was installed")
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in names:
            member = next((m for m in z.namelist() if m.rsplit("/", 1)[-1] == n), None)
            if member is None:
                raise RuntimeError(f"{n} is missing from the download")
            tmp = d / (n + ".part")
            tmp.write_bytes(z.read(member))
            tmp.chmod(0o755)
            tmp.replace(d / n)
    return d / names[0]
