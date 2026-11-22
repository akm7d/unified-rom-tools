# PyInstaller spec shared by the Linux and Windows builds (run by packaging/build.py).
# One Analysis, three launchers (Verifier / Renamer / Converter) sharing one runtime folder;
# romtools/__main__.py picks the app from the launcher's name.
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
WIN = sys.platform == "win32"
ICONS = Path(os.environ.get("ROMTOOLS_ICON_DIR", ROOT / "build" / "icons"))

APPS = [  # (module, Windows name, Linux name, icon)
    ("verifier", "ROM Verifier", "rom-verifier", "rom-verifier"),
    ("renamer", "ROM Renamer", "rom-renamer", "rom-renamer"),
    ("converter", "ROM Converter", "rom-converter", "rom-converter"),
]

a = Analysis(
    [str(ROOT / "romtools" / "__main__.py")],
    pathex=[str(ROOT)],
    hiddenimports=[f"romtools.gui.{m}" for m, *_ in APPS] + ["romtools.gui.datdialog"],
    datas=[(str(ROOT / "icons"), "icons")],
    excludes=["tkinter", "unittest", "pydoc_data", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtNetwork"],
    noarchive=False,
)
if not WIN:
    # Portable Linux build: don't ship the build machine's own system libraries (glibc, X11/xcb, GLib, fontconfig,
    # freetype, GL, ICU, ...). Every desktop Linux has them, and copies from a rolling-release distro would demand
    # its very new glibc. Qt comes from the PySide6 wheel and Python from python-build-standalone, both built for
    # an old glibc. A few small XCB helpers that some distros don't install by default are kept.
    KEEP = ("libxcb-cursor", "libxcb-icccm", "libxcb-image", "libxcb-keysyms", "libxcb-render-util",
            "libxcb-util", "libxkbcommon-x11")
    HOST = ("/usr/lib", "/lib", "/usr/local/lib")

    def _host_lib(src: str) -> bool:
        return src.startswith(HOST) and not Path(src).name.startswith(KEEP)

    dropped = [name for name, src, kind in a.binaries if _host_lib(src)]
    a.binaries = [b for b in a.binaries if not _host_lib(b[1])]
    print(f"romtools.spec: left out {len(dropped)} host system libraries")

pyz = PYZ(a.pure)

exes = []
for module, win_name, linux_name, icon in APPS:
    ico = ICONS / f"{icon}.ico"
    exes.append(EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=win_name if WIN else linux_name,
        console=False,
        icon=str(ico) if (WIN and ico.exists()) else None,
        version=None,
        upx=False,
    ))

COLLECT(*exes, a.binaries, a.datas, name="romtools", upx=False)
