"""Space-saving advisor: where can the library be stored smaller (or load faster) without losing anything?

Each suggestion names the files, the batch task that does it, an estimate (from compressing samples of the actual
files), and whether the usual emulator can read the result.
"""

from __future__ import annotations

import re
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .systems import guess_system

CD_SYSTEMS = {"psx", "ps2", "psp", "dc", "saturn", "segacd", "pcecd", "3do", "cdi", "neogeocd"}


@dataclass
class Suggestion:
    key: str                     # stable id for the row
    folder: str                  # system folder (or "Other")
    title: str                   # e.g. "Disc images → CHD"
    preset: str                  # converter preset that does it
    files: list[Path] = field(default_factory=list)
    size_now: int = 0
    size_after: int = 0
    compat: str = ""             # who can read the result
    warning: str = ""            # shown in orange (e.g. "Xenia can't read CHD")
    kind: str = "space"          # space | speed
    per_file: dict[Path, tuple[int, int]] = field(default_factory=dict)   # file -> (size now, size after)

    def note(self, f: Path, now: int, after: int) -> None:
        a, b = self.per_file.get(f, (0, 0))
        self.per_file[f] = (a + now, b + after)
        self.size_now += now
        self.size_after += after

    @property
    def saving(self) -> int:
        return max(0, self.size_now - self.size_after)


def sample_ratio(path: Path, samples: int = 12, chunk: int = 1 << 20) -> float:
    """Compressed/original ratio from evenly spaced 1 MiB samples (zlib level 6 approximates CHD/RVZ/NSZ)."""
    try:
        size = path.stat().st_size
        if size == 0:
            return 1.0
        n = max(1, min(samples, size // chunk))
        step = max(chunk, size // n)
        raw = comp = 0
        with open(path, "rb") as fh:
            for i in range(n):
                fh.seek(min(i * step, max(0, size - chunk)))
                data = fh.read(chunk)
                raw += len(data)
                comp += len(zlib.compress(data, 6))
        return min(1.0, comp / max(raw, 1))
    except OSError:
        return 1.0


def _chd_codecs(path: Path) -> str:
    from .chdman_tool import chdman_bin
    try:
        out = subprocess.run([chdman_bin(), "info", "-i", str(path)], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"^Compression:\s*(.+)$", out, re.M)
    return m.group(1) if m else ""


def analyse(cfg, root: Path, progress: Callable[[str], None] = lambda s: None,
            cancel=None) -> list[Suggestion]:
    from . import converter as cv
    from .library import OTHER, split
    from .scanner import _items_in, enumerate_system
    from .tasks import trim_info

    systems, _others, _loose = split(cfg, root)
    groups: list[tuple[str, list]] = [(s, enumerate_system(s, root / s)) for s in systems]
    # everything else, grouped by its top folder (e.g. "switch"), so suggestions name a real folder
    other: dict[str, list] = {}
    for it in _items_in(OTHER, root, skip_dirs=set(systems)):
        rel = it.path.relative_to(root).parts
        other.setdefault(rel[0] if len(rel) > 1 else root.name or "(top folder)", []).append(it)
    groups += sorted(other.items())
    out: list[Suggestion] = []

    def add(s: Suggestion, ratio_of: Callable[[Path], float] | None = None):
        for f in s.files:
            if cancel is not None and cancel.is_set():
                return
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            s.note(f, sz, int(sz * (ratio_of(f) if ratio_of else 1.0)))
        if s.files:
            out.append(s)

    for folder, items in groups:
        if cancel is not None and cancel.is_set():
            break
        progress(f"Looking at {folder}…")
        sysdef = guess_system(folder)
        sid = sysdef.id if sysdef else ""
        files = [it.path for it in items if it.kind != "folder"]
        by_ext: dict[str, list[Path]] = {}
        for f in files:
            by_ext.setdefault(f.suffix.lower(), []).append(f)

        # 1. uncompressed CD/DVD images -> CHD
        disc = by_ext.get(".cue", []) + by_ext.get(".gdi", []) + (by_ext.get(".iso", []) if sid in CD_SYSTEMS else [])
        if disc and sid in CD_SYSTEMS:
            def disc_ratio(f: Path) -> float:
                if f.suffix.lower() in (".cue", ".gdi"):
                    from .scanner import _cue_parts, _gdi_parts
                    parts = _cue_parts(f) if f.suffix.lower() == ".cue" else _gdi_parts(f)
                    return sample_ratio(parts[0]) if parts else 1.0
                return sample_ratio(f)
            s = Suggestion(f"{folder}:chd", folder, "Disc images → CHD", cv.TO_CHD, disc,
                           compat="DuckStation, PCSX2, PPSSPP, Flycast, Mednafen, RetroArch")
            for f in disc:   # count the tracks too
                if f.suffix.lower() in (".cue", ".gdi"):
                    from .scanner import _cue_parts, _gdi_parts
                    for p in (_cue_parts(f) if f.suffix.lower() == ".cue" else _gdi_parts(f)):
                        if p.exists():
                            sz = p.stat().st_size
                            s.note(f, sz, int(sz * disc_ratio(f)))
            add(s, lambda f: disc_ratio(f) if f.suffix.lower() == ".iso" else 1.0)

        # 2. GameCube / Wii -> RVZ
        gc = [f for e in (".iso", ".gcm", ".gcz", ".wia", ".ciso", ".wbfs") for f in by_ext.get(e, [])]
        if gc and sid in ("ngc", "wii"):
            add(Suggestion(f"{folder}:rvz", folder, "GameCube / Wii → RVZ", cv.TO_RVZ, gc, compat="Dolphin",
                           warning="WBFS files are already scrubbed" if by_ext.get(".wbfs") else ""),
                lambda f: 1.0 if f.suffix.lower() in (".gcz", ".wia") else sample_ratio(f) * 0.9)

        # 3. Xbox / Xbox 360 ISOs -> CHD (archive only)
        if by_ext.get(".iso") and sid in ("xbox", "xbox360"):
            add(Suggestion(f"{folder}:chd-xbox", folder, "Xbox ISOs → CHD (archive)", cv.TO_CHD, by_ext[".iso"],
                           compat="—", warning=("xemu" if sid == "xbox" else "Xenia") +
                           " can't read CHD: only worth it for archiving (convert back to play)"), sample_ratio)

        # 4. Switch -> NSZ / XCZ
        sw = by_ext.get(".nsp", []) + by_ext.get(".xci", [])
        if sw:
            add(Suggestion(f"{folder}:nsz", folder, "Switch NSP / XCI → NSZ / XCZ", cv.TO_NSZ, sw,
                           compat="Ryujinx and its forks, Eden, Citron, sudachi",
                           warning="needs your own prod.keys"), lambda f: sample_ratio(f) * 0.9)

        # 5. NDS / 3DS trimming
        nds = [f for e in (".nds", ".dsi", ".3ds", ".cci") for f in by_ext.get(e, [])]
        trimmable = []
        for f in nds:
            try:
                size, used, full = trim_info(f)
                if size > used and size == full:
                    trimmable.append((f, used / size))
            except Exception:  # noqa: BLE001
                continue
        if trimmable:
            ratios = dict(trimmable)
            add(Suggestion(f"{folder}:trim", folder, "Trim NDS / 3DS padding (reversible)", cv.TRIM,
                           [f for f, _r in trimmable], compat="all emulators (melonDS, DeSmuME, Azahar/Citra)"),
                lambda f: ratios.get(f, 1.0))

        # 6. LZMA-compressed CHDs -> Zstandard (faster to load, about the same size)
        chds = by_ext.get(".chd", [])
        if chds and sid in CD_SYSTEMS:
            progress(f"Checking CHD compression in {folder}…")
            slow = [f for f in chds if "lzma" in _chd_codecs(f).lower() and "zstd" not in _chd_codecs(f).lower()]
            if slow:
                add(Suggestion(f"{folder}:zstd", folder, "Recompress LZMA CHDs with Zstandard (faster loading)",
                               cv.RECOMPRESS, slow, kind="speed",
                               compat="DuckStation, PCSX2 2.x, PPSSPP 1.17+, recent RetroArch cores",
                               warning="files get ~5-10% bigger; older emulators can't read Zstandard CHDs"),
                    lambda f: 1.07)
    return out
