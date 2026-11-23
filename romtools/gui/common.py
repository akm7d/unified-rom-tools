"""Shared Qt pieces: app bootstrap, status colours, background workers, system list."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QActionGroup, QColor, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QMenu, QMessageBox, QPushButton, QStyleFactory, QToolButton, QTreeWidget, QTreeWidgetItem, QWidget)

from .. import scanner as sc
from ..config import get_config
from ..datmanager import ensure_mapping, library_folders, system_for
from ..systems import UNVERIFIABLE_HINT

STATUS_COLORS = {
    sc.VERIFIED: "#2e9d53",
    sc.MISNAMED: "#2f7fd1",
    sc.MODIFIED: "#1a9c9c",
    sc.PARTIAL: "#d49a1b",
    sc.BAD: "#d1452f",
    sc.UNKNOWN: "#8a6fc9",
    sc.IDENTIFIED: "#3a9a7a",
    sc.ATTENTION: "#e07b1f",
    sc.NODAT: "#7f8790",
    sc.ERROR: "#c2185b",
    sc.SKIPPED: "#7f8790",
}
STATUS_HELP = {
    sc.VERIFIED: "Hashes match the DAT and the name is correct",
    sc.MISNAMED: "Good dump, but the name differs from the DAT (fix it in ROM Renamer)",
    sc.MODIFIED: "Tagged as a hack, translation, undub or other modified ROM - expected not to match the DAT "
                 "(right-click → Mark as… to change)",
    sc.PARTIAL: "Some tracks/files match, others don't",
    sc.BAD: "The name is in the DAT but the data differs: bad dump, hack, trimmed or modified",
    sc.UNKNOWN: "Not in the DAT at all (hacks, translations, homebrew, or wrong system)",
    sc.IDENTIFIED: "Console package (PS4 PKG, Switch NSP/XCI) identified from its own data - no DAT exists for these",
    sc.ATTENTION: "Console package with a problem: an update or DLC without its game, DLC for another region, a "
                  "duplicate or an older update",
    sc.NODAT: "No DAT loaded for this system - open Manage DATs",
    sc.ERROR: "Could not read the file",
    sc.SKIPPED: "Folders and non-game files are not checked",
}


def make_app(name: str, icon: str) -> QApplication:
    QGuiApplication.setDesktopFileName(name.lower().replace(" ", "-"))
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(name)
    app.setOrganizationName("romtools")
    app.aboutToQuit.connect(Worker.wait_all)  # never destroy a running thread (Qt aborts the process)
    ic = QIcon.fromTheme(icon)
    if ic.isNull():
        from ..compat import resource_dir
        ic = QIcon(str(resource_dir() / "icons" / f"{icon}.svg"))
    app.setWindowIcon(ic)
    # Use the desktop's native style (Breeze on KDE, Adwaita-like on GNOME); fall back to Fusion.
    if app.style().objectName().lower() in ("windows", ""):
        app.setStyle(QStyleFactory.create("Fusion"))
    apply_theme(current_theme())
    return app


# ---------------------------------------------------------------- appearance

THEMES = {"system": "Follow system", "dark": "Dark", "light": "Light"}
_native: dict = {}


def default_theme() -> str:
    from ..compat import IS_WIN
    return "dark" if IS_WIN else "system"


def current_theme() -> str:
    t = get_config()["theme"] or default_theme()
    return t if t in THEMES else default_theme()


def _captured_palette() -> QPalette | None:
    """The desktop colour scheme captured by packaging/capture_theme.py (shipped as theme/dark-palette.json)."""
    import json
    from ..compat import FROZEN, resource_dir
    path = resource_dir() / ("theme" if FROZEN else "packaging/theme") / "dark-palette.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    p = QPalette()
    for gname, roles in data.get("colours", {}).items():
        group = getattr(QPalette.ColorGroup, gname, None)
        for rname, col in roles.items():
            role = getattr(QPalette.ColorRole, rname, None)
            if group is not None and role is not None:
                p.setColor(group, role, QColor(col))
    return p if p.color(QPalette.ColorRole.Window).lightness() < 128 else None


def dark_palette() -> QPalette:
    captured = _captured_palette()
    if captured is not None:
        return captured
    p = QPalette()
    c = {
        QPalette.ColorRole.Window: "#202124", QPalette.ColorRole.WindowText: "#e8eaed",
        QPalette.ColorRole.Base: "#17181a", QPalette.ColorRole.AlternateBase: "#232427",
        QPalette.ColorRole.Text: "#e8eaed", QPalette.ColorRole.Button: "#2b2d31",
        QPalette.ColorRole.ButtonText: "#e8eaed", QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#2b2d31", QPalette.ColorRole.ToolTipText: "#e8eaed",
        QPalette.ColorRole.Highlight: "#2f7fd1", QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#6ab0ff", QPalette.ColorRole.LinkVisited: "#b39ddb",
        QPalette.ColorRole.PlaceholderText: "#8a8f98", QPalette.ColorRole.Light: "#3a3c41",
        QPalette.ColorRole.Midlight: "#313338", QPalette.ColorRole.Mid: "#26282b",
        QPalette.ColorRole.Dark: "#141517", QPalette.ColorRole.Shadow: "#000000",
    }
    for role, col in c.items():
        p.setColor(role, QColor(col))
    for role, col in ((QPalette.ColorRole.WindowText, "#6f737a"), (QPalette.ColorRole.Text, "#6f737a"),
                      (QPalette.ColorRole.ButtonText, "#6f737a"), (QPalette.ColorRole.Base, "#1c1d20"),
                      (QPalette.ColorRole.Button, "#242528"), (QPalette.ColorRole.Highlight, "#3a3c41")):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(col))
    return p


def apply_theme(theme: str) -> None:
    """dark / light: Fusion style with an explicit palette (works the same on every OS, Wine included).
    system: the desktop's own style and colours. Windows title bars follow via Qt's colour-scheme hint."""
    app = QApplication.instance()
    if app is None:
        return
    if not _native:
        _native["style"] = app.style().name()
        _native["palette"] = QPalette(app.palette())
    hints = app.styleHints()
    scheme = getattr(Qt, "ColorScheme", None)
    if theme == "dark":
        app.setStyle(QStyleFactory.create("Fusion"))
        app.setPalette(dark_palette())
        if scheme and hasattr(hints, "setColorScheme"):
            hints.setColorScheme(scheme.Dark)
    elif theme == "light":
        app.setStyle(QStyleFactory.create("Fusion"))
        app.setPalette(app.style().standardPalette())
        if scheme and hasattr(hints, "setColorScheme"):
            hints.setColorScheme(scheme.Light)
    else:
        if hasattr(hints, "unsetColorScheme"):
            hints.unsetColorScheme()
        app.setStyle(QStyleFactory.create(_native["style"]) or QStyleFactory.create("Fusion"))
        app.setPalette(_native["palette"])


class ThemeButton(QToolButton):
    """Toolbar button: Theme ▸ Follow system / Dark / Light (saved, shared by all three apps)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("Theme")
        self.setIcon(QIcon.fromTheme("preferences-desktop-theme"))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolTip("Appearance: dark, light, or follow the system (applies to all three apps)")
        menu = QMenu(self)
        group = QActionGroup(self)
        group.setExclusive(True)
        for key, label in THEMES.items():
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == current_theme())
            act.triggered.connect(lambda _=False, k=key: self._choose(k))
            group.addAction(act)
        self.setMenu(menu)

    def _choose(self, key: str):
        cfg = get_config()
        cfg["theme"] = key
        cfg.save()
        apply_theme(key)


def is_dark() -> bool:
    pal = QApplication.palette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128


def human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return str(n)


def error_box(parent, title: str, exc: BaseException | str) -> None:
    QMessageBox.critical(parent, title, str(exc))


class Worker(QThread):
    """Run a callable off the GUI thread. The callable receives this worker (for cancel/progress)."""
    progress = Signal(int, int, str, float)
    message = Signal(str)
    failed = Signal(str)
    done = Signal(object)

    live: set = set()   # running workers, so the app can wait for them before exiting

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self.fn, self.args = fn, args
        self.cancel = threading.Event()
        Worker.live.add(self)
        self.finished.connect(lambda: Worker.live.discard(self))

    @staticmethod
    def wait_all(timeout_ms: int = 8000) -> None:
        for w in list(Worker.live):
            w.cancel.set()
        for w in list(Worker.live):
            if w.isRunning():
                w.wait(timeout_ms)

    def run(self):
        try:
            self.done.emit(self.fn(self, *self.args))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")


class TempModeCombo(QComboBox):
    """Where CHDs are unpacked while they're checked (RAM or SSD). A run already going keeps its choice; a change
    applies to the next run."""

    def __init__(self, is_running=lambda: False, notify=lambda msg: None, parent=None):
        super().__init__(parent)
        self.is_running, self.notify = is_running, notify
        self.addItem(QIcon.fromTheme("media-flash"), "Unpack CHDs in RAM (slower, spares the SSD)", "ram")
        self.addItem(QIcon.fromTheme("drive-harddisk-solidstate"), "Unpack CHDs on the SSD (faster)", "ssd")
        self.setToolTip("To check a CHD, its disc has to be unpacked. This picks where:\n\n"
                        "RAM: unpacked a piece at a time and hashed as it goes (about 35 MB of RAM per CHD). Nothing "
                        "is written to your SSD, so it doesn't wear it; a bit slower.\n"
                        "SSD: the whole disc is unpacked to the temp folder (Settings), hashed, then deleted. "
                        "Faster, but writes up to a full disc image (up to ~9 GB for a PS2 DVD) per CHD.\n\n"
                        "Also used by Fix CHD types. A change during a run applies to the next run.")
        self.setCurrentIndex(max(0, self.findData(get_config()["temp_mode"])))
        from ..compat import ram_mode_supported
        if not ram_mode_supported():  # Windows: no tmpfs streaming; temp files only
            self.model().item(0).setEnabled(False)
            self.setItemText(0, "Unpack CHDs in RAM (Linux only)")
            self.setCurrentIndex(1)
        self.currentIndexChanged.connect(self._changed)

    def _changed(self, _i):
        cfg = get_config()
        cfg["temp_mode"] = self.currentData()
        cfg.save()
        where = "in RAM" if self.currentData() == "ram" else "on the SSD"
        if self.is_running():
            self.notify(f"CHDs will be unpacked {where} from the next run (the current run keeps its choice).")
        else:
            self.notify(f"CHDs will be unpacked {where}.")

    def sync(self):
        self.blockSignals(True)
        self.setCurrentIndex(max(0, self.findData(get_config()["temp_mode"])))
        self.blockSignals(False)


class LibraryPicker(QWidget):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(get_config()["library_root"])
        self.edit.setPlaceholderText("Library folder containing one sub-folder per system")
        self.edit.setClearButtonEnabled(True)
        btn = QPushButton(QIcon.fromTheme("folder-open"), "Browse…")
        btn.clicked.connect(self._browse)
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.path_str()))
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def path_str(self) -> str:
        return self.edit.text().strip()

    def path(self) -> Path:
        return Path(self.path_str()).expanduser()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose ROM library folder", self.path_str())
        if d:
            self.edit.setText(d)
            self.changed.emit(d)


class SystemList(QTreeWidget):
    """Checkable list of the library's system folders, with their DAT status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["System folder", "DAT"])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.header().setStretchLastSection(True)
        self.root = Path()

    def load(self, root: Path) -> None:
        from .. import library
        self.root = root
        cfg = get_config()
        folders, others, loose = library.split(cfg, root)
        ensure_mapping(cfg, folders)
        checked = set(cfg["window_state"].get("checked_systems", folders + [library.OTHER]))
        self.clear()
        if others or loose:
            label, dat_text = library.other_label(cfg, root)
            it = QTreeWidgetItem([label, dat_text])
            it.setData(0, Qt.ItemDataRole.UserRole, library.OTHER)
            it.setIcon(0, QIcon.fromTheme("folder-open"))
            parts = []
            if loose:
                parts.append(f"{len(loose)} file(s) directly in this folder")
            if others:
                parts.append(f"{len(others)} other folder(s): " + ", ".join(o.name for o in others[:8])
                             + (" …" if len(others) > 8 else ""))
            tip = ("ROMs and archives that aren't inside a system folder. No system needs to be picked: each "
                   "file is identified against all your downloaded DATs.\n\n" + "\n".join(parts))
            it.setToolTip(0, tip)
            it.setToolTip(1, tip)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if (library.OTHER in checked or not folders)
                             else Qt.CheckState.Unchecked)
            self.addTopLevelItem(it)
        for f in folders:
            s = cfg.system(f)
            sysdef = system_for(cfg, f)
            dats = [d for d in s["dats"] if Path(d).is_file()]
            if sysdef and sysdef.source == "package":
                on = s.get("read_packages", True) is not False
                label, icon = ("package data" if on else "package data: off"), "dialog-information"
            elif dats:
                # the short system name ("Game Boy Color"), not the DAT file name that all start "Nintendo - …"
                label = sysdef.name if sysdef else ", ".join(Path(d).stem for d in dats)
                if len(dats) > 1:
                    label += f" + {len(dats) - 1} more DAT(s)"
                icon = "emblem-ok-symbolic"
            elif f in UNVERIFIABLE_HINT:
                label, icon = "no public DAT", "dialog-information"
            elif sysdef:
                label, icon = f"not downloaded ({sysdef.name})", "dialog-warning"
            else:
                label, icon = "no system mapped", "dialog-warning"
            it = QTreeWidgetItem([f, label])
            it.setData(0, Qt.ItemDataRole.UserRole, f)
            it.setIcon(1, QIcon.fromTheme(icon))
            tip = UNVERIFIABLE_HINT.get(f, "") or "\n".join(Path(d).name for d in dats)
            it.setToolTip(1, tip or label)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if f in checked else Qt.CheckState.Unchecked)
            if not dats and not (sysdef and sysdef.source == "package"):
                it.setForeground(1, QColor("#9a7b2f" if not is_dark() else "#e0b050"))
            self.addTopLevelItem(it)
        self.resizeColumnToContents(0)

    def checked(self) -> list[str]:
        """Keys of the ticked rows: system folder names, or library.OTHER."""
        out = []
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if it.checkState(0) == Qt.CheckState.Checked:
                out.append(it.data(0, Qt.ItemDataRole.UserRole) or it.text(0))
        return out

    def set_all(self, on: bool) -> None:
        for i in range(self.topLevelItemCount()):
            self.topLevelItem(i).setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def remember(self) -> None:
        cfg = get_config()
        cfg["window_state"]["checked_systems"] = self.checked()
        cfg.save()


def restore_geometry(win, key: str) -> None:
    s = QSettings("romtools", key)
    g = s.value("geometry")
    if g is not None:
        win.restoreGeometry(g)
    st = s.value("state")
    if st is not None and hasattr(win, "restoreState"):
        win.restoreState(st)


def save_geometry(win, key: str) -> None:
    s = QSettings("romtools", key)
    s.setValue("geometry", win.saveGeometry())
    if hasattr(win, "saveState"):
        s.setValue("state", win.saveState())



# ---------------------------------------------------------------- updates (romtools itself)

class UpdateButton(QToolButton):
    """Toolbar button: Updates ▸ Check for updates now / Check at launch / About. ``module`` is the app this window
    is (verifier / renamer / converter), so it can be restarted into the new version."""

    def __init__(self, parent, module: str):
        super().__init__(parent)
        from .. import __version__
        self.win, self.module = parent, module
        self.setText("Updates")
        self.setIcon(QIcon.fromTheme("system-software-update"))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolTip(f"romtools {__version__}: check for a newer version (shared by all three apps)")
        menu = QMenu(self)
        menu.addAction(QIcon.fromTheme("view-refresh"), "Check for updates now", lambda: check_updates(self.win,
                                                                                                    module, True))
        self.auto = menu.addAction("Check for updates when an app starts")
        self.auto.setCheckable(True)
        self.auto.setChecked(bool(get_config()["check_updates_at_launch"]))
        self.auto.toggled.connect(self._toggle)
        menu.addSeparator()
        menu.addAction(QIcon.fromTheme("help-about"), f"About Unified Rom Tools {__version__}", self._about)
        menu.addAction(QIcon.fromTheme("help-contents"), "Credits (tools, libraries, databases)",
                       lambda: show_credits(self.win))
        self.setMenu(menu)

    def _toggle(self, on: bool):
        cfg = get_config()
        cfg["check_updates_at_launch"] = on
        cfg.save()

    def _about(self):
        from .. import __version__
        from .. import selfupdate as su
        where = f"<br>Updates: <a href='https://github.com/{su.GITHUB_REPO}/releases'>github.com/{su.GITHUB_REPO}" \
                f"</a>" if su.GITHUB_REPO else ""
        QMessageBox.about(self.win, "About Unified Rom Tools",
                          f"<b>Unified Rom Tools (URT) {__version__}</b><br>ROM Verifier, ROM Renamer and ROM "
                          f"Optimizer.<br>Free software under the GNU GPL v3.0, without any warranty.{where}"
                          f"<br><br><b>Written by AI:</b> this software was written by Claude, an AI model made by "
                          f"Anthropic, directed and tested by its maintainer. Keep backups of anything you can't "
                          f"replace and check previews before applying changes.<br><br>Built on many open-source "
                          f"tools and databases: Updates ▸ <i>Credits</i>.")


def startup_update_check(win, module: str) -> None:
    """Called once a window is shown: quietly check for a newer release (if enabled and not checked recently)."""
    from PySide6.QtCore import QTimer
    from .. import selfupdate as su
    if su.GITHUB_REPO and su.due(get_config()):
        QTimer.singleShot(2500, lambda: check_updates(win, module, False))


def check_updates(win, module: str, manual: bool) -> None:
    """Look for a newer release in the background. ``manual``: also report "up to date" and errors."""
    from .. import __version__
    from .. import selfupdate as su

    def job(_w):
        return su.latest()

    def done(rel):
        cfg = get_config()
        su.mark_checked(cfg)
        skipped = cfg["window_state"].get("update_skip_version", "")
        if not su.newer(rel.version):
            if manual:
                QMessageBox.information(win, "Updates", f"romtools {__version__} is the newest version.")
            return
        if not manual and rel.version == skipped:
            return
        _offer(win, module, rel)

    def failed(err):
        su.mark_checked(get_config())
        if manual:
            QMessageBox.warning(win, "Updates", f"Couldn't check for updates:\n{err.split(': ', 1)[-1]}")

    w = Worker(job, parent=win)
    w.done.connect(done)
    w.failed.connect(failed)
    win._update_worker = w
    w.start()


def _offer(win, module: str, rel) -> None:
    from PySide6.QtWidgets import QProgressDialog
    from .. import __version__
    from .. import selfupdate as su
    kind = su.install_kind()
    box = QMessageBox(QMessageBox.Icon.Information, "Update available",
                      f"<b>romtools {rel.version}</b> is available (you have {__version__}).", parent=win)
    how = {"build": "It's downloaded, checked against its signed checksum and unpacked next to this folder; "
                    "your settings and DATs come along, and this version stays where it is.",
           "git": "Your copy is updated with git (git pull), then the bundled tools are fetched again.",
           "source": "This copy has no git history: open the release page to download it."}[kind]
    box.setInformativeText(how)
    if rel.notes:
        box.setDetailedText(rel.notes)
    upd = box.addButton("Update now" if kind != "source" else "Open release page", QMessageBox.ButtonRole.AcceptRole)
    later = box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
    skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
    box.setDefaultButton(upd)
    box.exec()
    clicked = box.clickedButton()
    if clicked is skip:
        cfg = get_config()
        cfg["window_state"]["update_skip_version"] = rel.version
        cfg.save()
        return
    if clicked is later or clicked is None:
        return
    if kind == "source":
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(rel.page))
        return
    dlg = QProgressDialog("Starting…", None, 0, 1000, win)
    dlg.setWindowTitle(f"Updating to {rel.version}")
    dlg.setMinimumDuration(0)
    dlg.setValue(0)

    w = Worker(lambda wk: su.apply(rel, lambda s, f: wk.progress.emit(int(f * 1000), 1000, s, f)), parent=win)
    w.progress.connect(lambda d, _t, s, _f: (dlg.setLabelText(s), dlg.setValue(d)))

    def finished(res):
        dlg.close()
        folder, msg = res
        if QMessageBox.question(win, "Updated", msg + "\n\nRestart now with the new version?") != \
                QMessageBox.StandardButton.Yes:
            return
        import subprocess
        import sys
        if folder is not None:
            subprocess.Popen(su.launcher(folder, module), cwd=str(folder))
        else:
            from ..compat import app_dir
            subprocess.Popen([sys.executable, "-m", f"romtools.gui.{module}"], cwd=str(app_dir()))
        win.close()

    w.done.connect(finished)
    w.failed.connect(lambda e: (dlg.close(), QMessageBox.warning(win, "Update failed",
                                                                 e.split(": ", 1)[-1] + "\n\nNothing was changed.")))
    win._update_worker = w
    w.start()


def show_credits(parent) -> None:
    """The bundled CREDITS.md (tools, libraries, databases, the AI note) in a window."""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout
    from ..compat import app_dir, resource_dir
    text = next((p.read_text(encoding="utf-8") for p in (resource_dir() / "CREDITS.md", app_dir() / "CREDITS.md")
                 if p.is_file()), "CREDITS.md is missing from this copy - see the project page on GitHub.")
    dlg = QDialog(parent)
    dlg.setWindowTitle("Credits - Unified Rom Tools")
    dlg.resize(900, 640)
    view = QTextBrowser()
    view.setOpenExternalLinks(True)
    view.setMarkdown(text)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(dlg.reject)
    lay = QVBoxLayout(dlg)
    lay.addWidget(view)
    lay.addWidget(bb)
    dlg.exec()


APPS = (("verifier", "ROM Verifier", "rom-verifier"), ("renamer", "ROM Renamer", "rom-renamer"),
        ("converter", "ROM Optimizer", "rom-optimizer"))


def add_app_buttons(tb, win, current: str) -> None:
    """'Open ROM …' buttons for the other two apps, in the same order and place in every app's toolbar."""
    from PySide6.QtGui import QAction
    from ..compat import launch_app, resource_dir
    tb.addSeparator()
    for module, name, icon in APPS:
        if module == current:
            continue
        act = QAction(QIcon.fromTheme(icon, QIcon(str(resource_dir() / "icons" / f"{icon}.svg"))), f"Open {name}", win)
        act.setToolTip(f"Start {name} (opens in its own window)")
        act.triggered.connect(lambda _=False, m=module: launch_app(m))
        tb.addAction(act)
    tb.addSeparator()

