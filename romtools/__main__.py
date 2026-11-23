"""Single entry point for all three apps (used by the packaged builds and `python -m romtools`).

Which app starts is decided by --app, or else by the executable's name
("ROM Renamer.exe", "rom-renamer", "rom-optimizer", ...), so one build can ship three launchers.

    python -m romtools --app converter
    rom-verifier --selftest [report-file]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APPS = ("verifier", "renamer", "converter")


def pick_app(argv: list[str]) -> str:
    if "--app" in argv:
        i = argv.index("--app")
        if i + 1 < len(argv) and argv[i + 1] in APPS:
            return argv[i + 1]
    name = Path(sys.executable if getattr(sys, "frozen", False) else argv[0]).stem.lower()
    for app in APPS:
        if app in name:
            return app
    if "optimizer" in name:      # ROM Optimizer is the converter app (renamed in 1.3.0)
        return "converter"
    return "verifier"


def selftest(report: str | None) -> int:
    """Headless check that a build is complete: imports, bundled tools, hashing, DAT parsing, Qt widgets.
    Writes a report (windowed Windows programs have no console) and returns 0 on success."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines, ok = [], True

    def check(label, fn):
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"PASS  {label}" + (f": {detail}" if detail else ""))
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"FAIL  {label}: {exc.__class__.__name__}: {exc}")
        if sys.stdout:
            print(lines[-1], flush=True)
        if report:
            Path(report).write_text("\n".join(lines) + "\n", encoding="utf-8")

    import tempfile
    import zlib

    def imports():
        import romtools
        from romtools import (cache, chd, chdfix, chdman_tool, compat, config, converter, dat, datmanager,  # noqa
                              advisor, discdetect, hashing, library, patching, renamer, scanner, systems, tags,
                              tasks, updater)
        from romtools.gui import common, converter as c2, datdialog, renamer as r2, verifier  # noqa
        return f"romtools {romtools.__version__}, Python {sys.version.split()[0]}, {sys.platform}"

    def chdman():
        from romtools import chdman_tool
        exe = chdman_tool.chdman_bin()
        v = chdman_tool.version(exe) if exe else None
        if not v:
            raise RuntimeError(f"chdman not usable ({exe})")
        n = len(chdman_tool.commands(exe))
        if n < 10:
            raise RuntimeError(f"only {n} chdman commands parsed")
        return f"{v.number} at {exe}, {n} commands"

    def sevenzip():
        from romtools.hashing import SEVENZIP
        if not SEVENZIP:
            raise RuntimeError("7z not found")
        return SEVENZIP

    def hashing():
        from romtools.hashing import hash_file
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.nes"
            data = b"NES\x1a" + b"\0" * 12 + bytes(range(256)) * 64
            p.write_bytes(data)
            e = hash_file(p)
            assert e.full.crc == f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", "crc mismatch"
            assert e.headerless and e.header_len == 16, "iNES header not detected"
        return "crc32/md5/sha1 + header detection"

    def dats():
        from romtools.dat import DatIndex, parse_dat
        xml = ('<?xml version="1.0"?><datafile><header><name>Test</name></header>'
               '<game name="Game (USA)"><rom name="Game (USA).bin" size="4" crc="d87f7e0c" '
               'sha1="a94a8fe5ccb19ba61c4c0873d391e987982fbbd3"/></game></datafile>')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.dat"
            p.write_text(xml)
            idx = DatIndex([parse_dat(p)])
            assert idx.lookup(4, "d87f7e0c", None, "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3"), "lookup failed"
        return "Logiqx parse + lookup"

    def gui():
        from PySide6.QtCore import QEventLoop, QTimer
        from romtools.gui.common import Worker, make_app
        app = make_app("selftest", "rom-verifier")
        from romtools.gui.converter import ConverterWindow
        from romtools.gui.renamer import RenamerWindow
        from romtools.gui.verifier import VerifierWindow
        # use a throwaway config so the self-test never touches real settings
        wins = [cls() for cls in (VerifierWindow, RenamerWindow, ConverterWindow)]
        loop = QEventLoop()
        QTimer.singleShot(1500, loop.quit)   # let start-up timers fire (remembered results, update check)
        loop.exec()
        Worker.wait_all(20000)               # never destroy a running thread
        for w in wins:
            w.close()
        app.processEvents()
        return f"{len(wins)} main windows created"

    def patching():
        import struct
        from romtools import patching as pt
        from romtools.compat import find_tool
        orig = bytes(range(256)) * 2048
        mod = bytearray(orig)
        mod[100:108] = b"PATCHED!"
        mod = bytes(mod)
        assert pt.apply_bps(orig, pt.create_bps(orig, mod)) == mod, "BPS"
        ips = b"PATCH" + (100).to_bytes(3, "big") + (8).to_bytes(2, "big") + b"PATCHED!" + b"EOF"
        assert pt.apply_ips(orig, ips) == mod, "IPS"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            d = Path(d)
            (d / "o.bin").write_bytes(orig)
            (d / "m.bin").write_bytes(mod)
            ppf = b"PPF10\x00" + b"t".ljust(50) + struct.pack("<I", 100) + bytes([8]) + b"PATCHED!"
            (d / "p.ppf").write_bytes(ppf)
            pt.apply_ppf(d / "o.bin", d / "p.ppf", d / "r.bin")
            assert (d / "r.bin").read_bytes() == mod, "PPF"
            exe = find_tool("xdelta3")
            if not exe:
                from romtools.compat import FROZEN
                if FROZEN:
                    raise RuntimeError("bundled xdelta3 missing")
                return "IPS, BPS, PPF (xdelta skipped: xdelta3 isn't installed; the builds bundle it)"
            import subprocess
            subprocess.run([exe, "-e", "-f", "-s", str(d / "o.bin"), str(d / "m.bin"), str(d / "p.xd")], check=True,
                           capture_output=True)
            pt.apply_xdelta(d / "o.bin", d / "p.xd", d / "x.bin")
            assert (d / "x.bin").read_bytes() == mod, "xdelta"
        return f"IPS, BPS, PPF, xdelta ({exe})"

    def disc_tools():
        from romtools.compat import FROZEN, find_tool
        found = {t: find_tool(t) for t in ("nodtool", "nsz", "nstool")}
        # the Switch package reader needs the cryptography library (AES-XTS / CTR / ECB): prove it's bundled
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        blk = Cipher(algorithms.AES(bytes(range(32))), modes.XTS(bytes(16))).encryptor().update(bytes(512))
        assert len(blk) == 512 and Cipher(algorithms.AES(bytes(16)), modes.CTR(bytes(16))).decryptor().update(b"x")
        from romtools.nod import parse_verify
        got = parse_verify(["Title: X", "Game ID: GZ2E01", "Redump: X ✅", "CRC32 : 0ab54d43 ✅",
                            "SHA-1 : 75EDD3DDFF41F125D1B4CE1A40378F1B565519E7 ✅", "XXH64 : 0123456789abcdef"])
        assert got.get("sha1") == "75edd3ddff41f125d1b4ce1a40378f1b565519e7" and got.get("crc32") == "0ab54d43" \
            and got.get("game_id") == "GZ2E01", f"nodtool output parsing: {got}"
        missing = [t for t, p in found.items() if not p]
        if missing and FROZEN:
            raise RuntimeError("bundled tool(s) missing: " + ", ".join(missing))
        return ", ".join(f"{t}: {'bundled' if p and 'tools' in p else (p or 'not installed (bundled in builds)')}"
                         for t, p in found.items()) + ", cryptography OK"

    def console_packages():
        import struct
        import tempfile
        from romtools import packages as pk
        # a minimal PS4 package: header + entry table + PARAM.SFO
        keys = b"APP_VER\0CATEGORY\0CONTENT_ID\0TITLE\0TITLE_ID\0"
        vals = [b"01.05\0\0\0", b"gp\0\0", b"UP0001-CUSA11111_00-TEST000000000000\0".ljust(40, b"\0"),
                b"GAME\xe2\x84\xa2 ONE\0".ljust(12, b"\0"), b"CUSA11111\0\0\0"]
        idx, data, ko = b"", b"", 0
        for k, v in zip(keys.split(b"\0")[:-1], vals):
            idx += struct.pack("<HHIII", ko, 0x204, len(v.rstrip(b"\0")) + 1, len(v), len(data))
            ko += len(k) + 1
            data += v
        ks = 20 + len(idx)
        sfo = b"\0PSF" + struct.pack("<IIII", 0x101, ks, ks + len(keys), 5) + idx + keys + data
        head = bytearray(0x200)
        head[:4] = b"\x7fCNT"
        head[0x10:0x14], head[0x18:0x1C] = struct.pack(">I", 1), struct.pack(">I", 0x100)
        head[0x100:0x118] = struct.pack(">IIIIII", 0x1000, 0, 0, 0, 0x200, len(sfo))
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.pkg"
            f.write_bytes(bytes(head) + sfo)
            i = pk.read(f)
        assert (i.kind, i.title_id, i.version_str, i.region) == ("Update", "CUSA11111", "1.05", "USA"), i
        name = pk.suggested_name(i, "Game One")
        assert name == "Game One [CUSA11111] (Update v1.05)", name
        assert pk.check_set([i])[str(f)] == ["no base game (CUSA11111) found next to this update"]
        return f"PS4 PKG metadata, naming and checks; Switch reader {'with' if pk.switch_keys() else 'without'} keys"

    def theme():
        from romtools.gui.common import _captured_palette, current_theme, default_theme
        cap = _captured_palette()
        if cap is None:
            raise RuntimeError("captured dark colour scheme (theme/dark-palette.json) missing")
        return f"default {default_theme()}, current {current_theme()}, dark window {cap.color(cap.ColorRole.Window).name()}"

    def paths():
        from romtools import compat, config
        mode = (f"portable build (data folder: {compat.app_dir() / 'data'})" if portable_build
                else "per-user settings")
        # round-trip a path inside the data folder through the relative-path storage
        sample = str(config.DATA_DIR / "dats" / "x.dat")
        assert config.unpack_path(config.pack_path(sample)) == sample, "relative path round-trip failed"
        return (f"{mode}; config {config.CONFIG_DIR}, temp {config.DEFAULTS['tmp_dir']}, "
                f"RAM mode {compat.ram_mode_supported()}")

    # Windows keeps open files (e.g. the hash-cache database) locked, so leftovers may outlive the test.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
        # isolate settings, data and cache for the self-test
        from romtools import compat
        portable_build = compat.portable_marker()   # checked before isolating
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"):
            os.environ[var] = home
        os.environ["ROMTOOLS_HOME"] = str(Path(home) / "portable")   # never touch the build's own data folder
        for label, fn in (("imports", imports), ("paths", paths), ("chdman", chdman), ("7-Zip", sevenzip),
                          ("hashing", hashing), ("DAT parsing", dats), ("GUI", gui), ("theme", theme),
                          ("patching", patching), ("RVZ / NSZ / nstool tools", disc_tools), ("console packages", console_packages)):
            check(label, fn)
    lines.append("RESULT " + ("OK" if ok else "FAILED"))
    if report:
        Path(report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sys.stdout:
        print(lines[-1], flush=True)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--selftest" in argv:
        i = argv.index("--selftest")
        report = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else None
        return selftest(report)
    app = pick_app(argv)
    if app == "renamer":
        from romtools.gui.renamer import main as run
    elif app == "converter":
        from romtools.gui.converter import main as run
    else:
        from romtools.gui.verifier import main as run
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
