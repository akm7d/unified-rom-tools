"""Every test run gets its own throwaway settings / data / cache folders, set BEFORE romtools is imported,
so the tests can never read or change a real configuration or library."""

import os
import sys
import tempfile
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="romtools-tests-"))
os.environ["ROMTOOLS_HOME"] = str(_HOME / "home")
os.environ["XDG_CONFIG_HOME"] = str(_HOME / "xdg")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import struct  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def make_pkg():
    """Write a minimal PS4 package (header + entry table + PARAM.SFO) for the package reader."""
    def build(path: Path, **sfo_vals) -> Path:
        keys, data, idx = b"", b"", b""
        items = sorted(sfo_vals.items())
        for k, v in items:
            ko = len(keys)
            keys += k.encode() + b"\0"
            if isinstance(v, int):
                raw, fm, ln, mx = struct.pack("<I", v), 0x404, 4, 4
            else:
                raw = v.encode() + b"\0"
                fm, ln = 0x204, len(raw)
                mx = (ln + 3) & ~3
                raw = raw.ljust(mx, b"\0")
            idx += struct.pack("<HHIII", ko, fm, ln, mx, len(data))
            data += raw
        ks = 20 + len(idx)
        keys = keys.ljust((len(keys) + 3) & ~3, b"\0")
        sfo = b"\0PSF" + struct.pack("<IIII", 0x101, ks, ks + len(keys), len(items)) + idx + keys + data
        head = bytearray(0x200)
        head[:4] = b"\x7fCNT"
        head[0x10:0x14], head[0x18:0x1C] = struct.pack(">I", 1), struct.pack(">I", 0x100)
        head[0x100:0x118] = struct.pack(">IIIIII", 0x1000, 0, 0, 0, 0x200, len(sfo))
        path.write_bytes(bytes(head) + sfo + b"\0" * 256)
        return path
    return build
