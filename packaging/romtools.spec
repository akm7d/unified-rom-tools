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
