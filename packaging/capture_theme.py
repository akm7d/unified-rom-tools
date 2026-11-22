#!/usr/bin/env python3
"""Capture the current desktop's Qt colour scheme into packaging/theme/dark-palette.json.

The apps' "Dark" theme (the default in the Windows build) uses these colours, so the Windows build looks like
the Linux apps do on this desktop. Run it from your desktop session (not over SSH / offscreen):

    make capture-theme
"""

from __future__ import annotations

import configparser
import json
import os
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "theme" / "dark-palette.json"
GROUPS = ("Active", "Inactive", "Disabled")
ROLES = ("Window", "WindowText", "Base", "AlternateBase", "ToolTipBase", "ToolTipText", "PlaceholderText", "Text",
         "Button", "ButtonText", "BrightText", "Light", "Midlight", "Mid", "Dark", "Shadow", "Highlight",
         "HighlightedText", "Link", "LinkVisited", "Accent")


def scheme_name() -> str:
    for f in (Path.home() / ".config/kdeglobals", Path.home() / ".config/kdedefaults/kdeglobals"):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f)
            name = cp.get("General", "ColorScheme", fallback="")
            if name:
                return name
        except configparser.Error:
            continue
    return ""


def main() -> int:
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        del os.environ["QT_QPA_PLATFORM"]
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    if app.platformName() in ("offscreen", "minimal"):
        print("Run this from your desktop session: Qt can't see the desktop's colours here.")
        return 1
    pal = app.palette()
    colours = {}
    for g in GROUPS:
        group = getattr(QPalette.ColorGroup, g)
        colours[g] = {r: pal.color(group, getattr(QPalette.ColorRole, r)).name(QColorNameFormat())
                      for r in ROLES if hasattr(QPalette.ColorRole, r)}
    window = pal.color(QPalette.ColorRole.Window)
    if window.lightness() >= 128:
        print(f"The current scheme is light (window {window.name()}); switch the desktop to a dark scheme first.")
        return 1
    data = {"source": scheme_name() or app.style().name(), "platform": app.platformName(),
            "captured": time.strftime("%Y-%m-%d"), "colours": colours}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"Captured '{data['source']}' ({window.name()} window, {pal.color(QPalette.ColorRole.Highlight).name()} "
          f"highlight) into {OUT}")
    return 0


def QColorNameFormat():
    from PySide6.QtGui import QColor
    return QColor.NameFormat.HexArgb


if __name__ == "__main__":
    sys.exit(main())
