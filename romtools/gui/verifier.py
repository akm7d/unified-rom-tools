"""ROM Verifier - checks a ROM library against No-Intro / Redump DATs. Read-only."""

from __future__ import annotations

import csv
import html
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter, QTableView, QTextBrowser, QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import __version__, chd
from .. import scanner as sc
from .. import tags as tg
from ..cache import HashCache
from ..config import get_config
from ..datmanager import IndexCache
from .common import (add_app_buttons, STATUS_COLORS, STATUS_HELP, LibraryPicker, SystemList, TempModeCombo, ThemeButton, UpdateButton, startup_update_check, Worker,
                     human, make_app,
                     restore_geometry, save_geometry)
from .datdialog import DatDialog

COLS = ["Status", "Tag", "System", "File", "DAT game", "Size", "CHD type", "CRC32", "SHA-1", "Notes"]
C_STATUS, C_TAG, C_SYSTEM, C_FILE, C_GAME, C_SIZE, C_MEDIA, C_CRC, C_SHA1, C_NOTE = range(len(COLS))
WRONG_MEDIA = "#d49a1b"


def media_text(r: sc.Result) -> str:
    if r.media is not None:
        return ("⚠ " if not r.media.ok else "") + r.media.label
    if r.chd_info and "disc" in r.chd_info:      # RVZ/CISO/... read with nodtool
        return str(r.chd_info["disc"].get("container", "")).upper()
    if r.chd_info and "package" in r.chd_info:   # PS4 PKG / Switch NSP·XCI
        p = r.chd_info["package"]
        return f"{'PS4' if p.get('platform') == 'ps4' else 'Switch'} · {p.get('kind', '')}"
    kind = r.chd_info.get("kind", "") if r.chd_info else ""
    return kind.upper()


def system_text(r: sc.Result) -> str:
    """The folder's system, and the one the data belongs to when that's another: "gb → snes"."""
    here = "?" if r.item.system == "(other)" else r.item.system
    if r.misplaced and r.found and r.found != here:
        return f"{here} → {r.found}"
    return here


class ResultsModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[sc.Result] = []
        self._sizes: list[int] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[s]
        return None

    def add(self, r: sc.Result):
        n = len(self.rows)
        self.beginInsertRows(QModelIndex(), n, n)
        self.rows.append(r)
        self._sizes.append(r.size)
        self.endInsertRows()

    def replace(self, r: sc.Result):
        for i, old in enumerate(self.rows):
            if old.item.path == r.item.path and old.item.member == r.item.member:
                self.rows[i] = r
                self._sizes[i] = r.size
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLS) - 1))
                return
        self.add(r)

    def remove_paths(self, paths: set):
        keep = [(r, s) for r, s in zip(self.rows, self._sizes) if r.item.path not in paths]
        self.beginResetModel()
        self.rows[:] = [r for r, _s in keep]
        self._sizes[:] = [s for _r, s in keep]
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.rows.clear()
        self._sizes.clear()
        self.endResetModel()

    @staticmethod
    def _first(r: sc.Result):
        return r.entries[0].entry.full if len(r.entries) == 1 else None

    def data(self, idx, role=Qt.ItemDataRole.DisplayRole):
        if not idx.isValid():
            return None
        r, c = self.rows[idx.row()], idx.column()
        if role == Qt.ItemDataRole.DisplayRole:
            h = self._first(r)
            if c == C_STATUS:
                return r.status
            if c == C_TAG:
                return (r.tag + (" ✎" if r.tag_source == "marked by you" else "")) if r.tag else ""
            if c == C_SYSTEM:
                return system_text(r)
            if c == C_FILE:
                return r.item.label
            if c == C_GAME:
                return r.game
            if c == C_SIZE:
                return human(self._sizes[idx.row()]) if self._sizes[idx.row()] else ""
            if c == C_MEDIA:
                return media_text(r)
            if c == C_CRC:
                return h.crc if h else (f"{len(r.entries)} tracks" if r.entries else "")
            if c == C_SHA1:
                return h.sha1 if h else ""
            if c == C_NOTE:
                return r.note + ("  (cached)" if r.cached else "")
        elif role == Qt.ItemDataRole.ForegroundRole and c == C_STATUS:
            return QColor(STATUS_COLORS.get(r.status, "#888"))
        elif role == Qt.ItemDataRole.ForegroundRole and c == C_TAG:
            return QColor(STATUS_COLORS[sc.MODIFIED])
        elif role == Qt.ItemDataRole.ForegroundRole and c == C_MEDIA and r.media_wrong:
            return QColor(WRONG_MEDIA)
        elif role == Qt.ItemDataRole.FontRole and c == C_STATUS:
            from PySide6.QtGui import QFont
            f = QFont()
            f.setBold(True)
            return f
        elif role == Qt.ItemDataRole.DecorationRole and c == C_STATUS:
            return _dot(STATUS_COLORS.get(r.status, "#888"))
        elif role == Qt.ItemDataRole.ToolTipRole:
            if c == C_STATUS:
                return STATUS_HELP.get(r.status, "")
            if c == C_TAG and r.tag:
                return f"{r.tag}: {tg.TAGS.get(r.tag, 'custom tag')}\n{r.tag_source}" + (
                    f"\nWithout the tag this would be: {r.orig_status}" if r.orig_status else "")
            if c == C_SYSTEM and r.found:
                by = {"hash": "its data matches that system's DAT", "header": "its own header",
                      "package": "its own package data"}.get(r.found_by, "")
                return f"{sc.system_name(r.found)} (known from: {by})" + (
                    f"\nIt lies in a {r.item.system} folder." if r.misplaced else "")
            if c == C_FILE:
                return str(r.item.path) + (f"\ninside the archive: {r.item.member}" if r.item.member else "")
            if c == C_NOTE:
                return r.note
            if c == C_MEDIA and r.media is not None:
                m = r.media
                return m.reason + (f"\n{'Fixable' if m.fixable else 'Not auto-fixable'}: {m.fix_note}"
                                   if not m.ok else "")
        elif role == Qt.ItemDataRole.UserRole:  # sort key
            if c == C_SIZE:
                return self._sizes[idx.row()]
            if c == C_STATUS:
                return sc.STATUS_ORDER.index(r.status) if r.status in sc.STATUS_ORDER else 99
            return self.data(idx)
        return None


def _csv_path(r: sc.Result) -> str:
    return f"{r.item.path} › {r.item.member}" if r.item.member else str(r.item.path)


_dots: dict[str, QIcon] = {}


def _dot(color: str) -> QIcon:
    if color not in _dots:
        from PySide6.QtGui import QPainter, QPixmap
        pm = QPixmap(12, 12)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, 10, 10)
        p.end()
        _dots[color] = QIcon(pm)
    return _dots[color]


class FilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.statuses: set[str] = set(sc.STATUS_ORDER)
        self.text = ""
        self.only_wrong_media = False
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set(self, statuses=None, text=None, only_wrong_media=None):
        if only_wrong_media is not None:
            self.only_wrong_media = only_wrong_media
        if statuses is not None:
            self.statuses = statuses
        if text is not None:
            self.text = text.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        r = self.sourceModel().rows[row]
        if self.only_wrong_media and not r.media_wrong:
            return False
        if r.status not in self.statuses:
            return False
        if self.text:
            hay = f"{r.item.label} {r.game} {r.item.system} {r.note} {r.tag}".lower()
            return all(t in hay for t in self.text.split())
        return True


class ScanWorker(Worker):
    result = Signal(object)


class SettingsDialog(QDialog):
    def __init__(self, parent=None, cache: HashCache | None = None):
        super().__init__(parent)
        self.setWindowTitle("Verifier settings")
        cfg = get_config()
        self.workers = QSpinBox(minimum=1, maximum=32, value=cfg["workers"])
        self.workers.setToolTip("Files hashed in parallel. 2-6 suits a network share; more helps on SSDs.")
        self.chd = QSpinBox(minimum=1, maximum=8, value=cfg["chd_workers"])
        self.chd.setToolTip("CHDs extracted at once. Each needs temp space equal to the disc size.")
        self.tmp = QLineEdit(cfg["tmp_dir"])
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(self, "Temp folder", self.tmp.text()))
                               and self.tmp.setText(d))
        tmprow = QHBoxLayout()
        tmprow.addWidget(self.tmp, 1)
        tmprow.addWidget(browse)
        from .. import packages as _pk
        self.keys = QLineEdit(str(_pk.switch_keys(cfg) or ""))
        self.keys.setPlaceholderText("your prod.keys (to read Switch NSP / XCI names and versions)")
        self.keys.setToolTip("Your own console keys. Used to read Switch packages' titles, IDs and versions (and by "
                             "ROM Optimizer for NSZ). Only read, never copied.")
        kbrowse = QPushButton("Browse…")
        kbrowse.clicked.connect(lambda: (f := QFileDialog.getOpenFileName(
            self, "Switch keys", self.keys.text(), "Keys (prod.keys keys.txt);;All files (*)")[0])
            and self.keys.setText(f))
        keyrow = QHBoxLayout()
        keyrow.addWidget(self.keys, 1)
        keyrow.addWidget(kbrowse)
        self.tkeys = QLineEdit(str(_pk.title_keys(cfg) or ""))
        self.tkeys.setPlaceholderText("your title.keys (optional: for packages without a ticket)")
        self.tkeys.setToolTip("Title keys (rights ID = key, hactool/nstool format). Only needed for Switch content "
                              "whose package has no ticket. Shared with ROM Optimizer.")
        tbrowse = QPushButton("Browse…")
        tbrowse.clicked.connect(lambda: (f := QFileDialog.getOpenFileName(
            self, "Title keys", self.tkeys.text(), "Title keys (title.keys);;All files (*)")[0])
            and self.tkeys.setText(f))
        tkeyrow = QHBoxLayout()
        tkeyrow.addWidget(self.tkeys, 1)
        tkeyrow.addWidget(tbrowse)
        clear = QPushButton(QIcon.fromTheme("edit-clear-history"), "Forget all remembered scans")
        clear.setToolTip("Deletes the stored hashes. The next Quick scan will re-hash everything.")
        clear.clicked.connect(self._forget)
        self.cache = cache
        form = QFormLayout(self)
        form.addRow("Parallel files:", self.workers)
        form.addRow("Parallel CHD extractions:", self.chd)
        form.addRow("SSD-mode temp folder:", tmprow)
        from ..compat import ram_mode_supported
        form.addRow("", QLabel("<small>" + ("Only used when CHDs are unpacked on the SSD (in RAM they stream through /dev/shm). "
                                             if ram_mode_supported() else "Used for unpacking CHDs. ") +
                               "PS2 DVDs need up to ~9 GB each here.</small>"))
        form.addRow("Switch prod.keys:", keyrow)
        form.addRow("Switch title.keys:", tkeyrow)
        form.addRow("Remembered scans:", clear)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _forget(self):
        if self.cache and QMessageBox.question(
                self, "Forget remembered scans",
                "Delete all remembered hashes? The next Quick scan will have to re-hash every ROM "
                "(hours for a large CHD library).") == QMessageBox.StandardButton.Yes:
            self.cache.clear()
            QMessageBox.information(self, "Remembered scans", "Forgotten.")

    def accept(self):
        cfg = get_config()
        cfg["workers"], cfg["chd_workers"] = self.workers.value(), self.chd.value()
        from ..compat import default_tmp_dir
        cfg["tmp_dir"] = self.tmp.text().strip() or default_tmp_dir()
        k, t = self.keys.text().strip(), self.tkeys.text().strip()
        for path, what in ((k, "prod.keys"), (t, "title.keys")):
            if path and not Path(path).is_file():
                QMessageBox.warning(self, "Switch keys", f"{what}: {path} doesn't exist.")
                return
        cfg["switch_keys"], cfg["title_keys"] = k, t
        cfg.save()
        super().accept()


class VerifierWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ROM Verifier")
        self.resize(1400, 820)
        self.cfg = get_config()
        self.cache = HashCache()
        self.marks = tg.MarkStore()
        self.targets = None
        self.setAcceptDrops(True)
        self.indexes = IndexCache()
        self.worker: ScanWorker | None = None
        self.started = 0.0
        chd.cleanup_stale(Path(self.cfg["tmp_dir"]))
        chd.cleanup_stale(chd.RAM_DIR)
        self.run_mode = ""

        # ---- toolbar
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setObjectName("main-toolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.act_scan = QAction(QIcon.fromTheme("media-playback-start"), "Quick scan", self)
        self.act_scan.setShortcut(QKeySequence("F5"))
        self.act_scan.setToolTip("Quick scan (F5): reuses results remembered from earlier scans and only hashes "
                                 "ROMs that are new or changed since (size/date differ).")
        self.act_scan.triggered.connect(lambda: self.start_scan(mode=sc.QUICK))
        self.act_full = QAction(QIcon.fromTheme("view-refresh"), "Full scan", self)
        self.act_full.setShortcut(QKeySequence("Shift+F5"))
        self.act_full.setToolTip("Full scan (Shift+F5): re-hashes every ROM from scratch, ignoring remembered "
                                 "results, and remembers the fresh results.")
        self.act_full.triggered.connect(lambda: self.start_scan(mode=sc.FULL))
        self.act_stop = QAction(QIcon.fromTheme("media-playback-stop"), "Stop", self)
        self.act_stop.setEnabled(False)
        self.act_stop.triggered.connect(self.stop_scan)
        act_dats = QAction(QIcon.fromTheme("view-list-details"), "Manage DATs…", self)
        act_dats.triggered.connect(self.manage_dats)
        act_export = QAction(QIcon.fromTheme("document-export"), "Export report…", self)
        act_export.triggered.connect(self.export)
        act_settings = QAction(QIcon.fromTheme("configure"), "Settings…", self)
        act_settings.triggered.connect(lambda: (SettingsDialog(self, self.cache).exec(), self.mode.sync()))
        for a in (self.act_scan, self.act_full):
            tb.addAction(a)
        self.check_btn = QToolButton()
        self.check_btn.setText("Check file/folder")
        self.check_btn.setIcon(QIcon.fromTheme("document-open"))
        self.check_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.check_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.check_btn.setToolTip("Verify any ROM, archive or folder right away - no library or system needed.\n"
                                  "You can also drag files or folders onto this window.")
        cm = QMenu(self.check_btn)
        a = cm.addAction(QIcon.fromTheme("document-open"), "ROM / archive file(s)…", self.check_files)
        a.setShortcut(QKeySequence("Ctrl+O"))
        a = cm.addAction(QIcon.fromTheme("folder-open"), "Folder…", self.check_folder)
        a.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.addActions(cm.actions())          # shortcuts work without opening the menu
        self.check_btn.setMenu(cm)
        tb.addWidget(self.check_btn)
        tb.addAction(self.act_stop)
        tb.addSeparator()
        self.mode = TempModeCombo(lambda: bool(self.worker and self.worker.isRunning()),
                                  lambda m: self.statusBar().showMessage(m, 8000))
        tb.addWidget(self.mode)
        tb.addSeparator()
        self.act_fix = QAction(QIcon.fromTheme("tools-wizard", QIcon.fromTheme("run-build")), "Fix CHD types…", self)
        self.act_fix.setToolTip("Convert CHDs stored as the wrong media type (CD ↔ DVD). Fixes the selected rows, "
                                "or every fixable one if none are selected. Originals go to the trash.")
        self.act_fix.setEnabled(False)
        self.act_fix.triggered.connect(lambda: self.fix_media(self._fix_candidates()))
        self.act_updates = QAction(QIcon.fromTheme("system-software-update"), "Check game updates", self)
        self.act_updates.setToolTip("Switch: compare your updates with the newest official version (public title "
                                    "database, downloaded once a day).\nPS4: right-click a package → Look up "
                                    "updates online.")
        self.act_updates.triggered.connect(self.check_updates)
        for a in (self.act_fix, self.act_updates, act_dats, act_export, act_settings):
            tb.addAction(a)
        add_app_buttons(tb, self, "verifier")
        tb.addWidget(ThemeButton(self))
        tb.addWidget(UpdateButton(self, "verifier"))

        # ---- left: library + systems
        self.picker = LibraryPicker()
        self.picker.changed.connect(self._library_changed)
        self.systems = SystemList()
        all_btn, none_btn = QPushButton("All"), QPushButton("None")
        all_btn.clicked.connect(lambda: self.systems.set_all(True))
        none_btn.clicked.connect(lambda: self.systems.set_all(False))
        selrow = QHBoxLayout()
        selrow.addWidget(QLabel("<b>Systems</b>"))
        selrow.addStretch(1)
        selrow.addWidget(all_btn)
        selrow.addWidget(none_btn)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("<b>Library</b>"))
        ll.addWidget(self.picker)
        ll.addLayout(selrow)
        ll.addWidget(self.systems, 1)

        # ---- center: filters + table
        self.model = ResultsModel()
        self.proxy = FilterProxy()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for c, w in ((C_STATUS, 110), (C_TAG, 100), (C_SYSTEM, 90), (C_FILE, 340), (C_GAME, 300), (C_SIZE, 80), (C_MEDIA, 150),
                     (C_CRC, 80), (C_SHA1, 120)):
            self.table.setColumnWidth(c, w)
        self.table.selectionModel().currentRowChanged.connect(self._show_detail)

        self.chips: dict[str, QToolButton] = {}
        chiprow = QHBoxLayout()
        for st in sc.STATUS_ORDER:
            b = QToolButton()
            b.setCheckable(True)
            b.setChecked(True)
            b.setIcon(_dot(STATUS_COLORS[st]))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setToolTip(STATUS_HELP[st] + "\n(click to show/hide, double-click to show only this)")
            b.toggled.connect(self._filter_changed)
            b.setText(f"{st}: 0")
            b.mouseDoubleClickEvent = lambda _e, s=st: self._only(s)
            self.chips[st] = b
            chiprow.addWidget(b)
        self.media_chip = QToolButton()
        self.media_chip.setCheckable(True)
        self.media_chip.setIcon(_dot(WRONG_MEDIA))
        self.media_chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.media_chip.setText("Wrong CHD type: 0")
        self.media_chip.setToolTip("CHDs stored as the wrong media type (e.g. a PS2 DVD game packed as a CD CHD).\n"
                                   "Click to show only these; use Fix CHD types… to convert them.")
        self.media_chip.toggled.connect(lambda on: self.proxy.set(only_wrong_media=on))
        chiprow.addSpacing(12)
        chiprow.addWidget(self.media_chip)
        chiprow.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, game, system…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda t: self.proxy.set(text=t))
        self.search.setMaximumWidth(320)
        chiprow.addWidget(self.search)

        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addLayout(chiprow)
        cl.addWidget(self.table, 1)

        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        self.detail.setPlaceholderText("Select a row to see per-file / per-track hashes.")

        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.addWidget(center)
        vsplit.addWidget(self.detail)
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        hsplit = QSplitter(Qt.Orientation.Horizontal)
        hsplit.addWidget(left)
        hsplit.addWidget(vsplit)
        hsplit.setStretchFactor(1, 1)
        hsplit.setSizes([300, 1100])
        self.setCentralWidget(hsplit)

        # ---- status bar
        # Left: what's happening. Right (only while working): "Current file" bar, then "All files" bar.
        self.current = QLabel("Ready")
        self.item_label = QLabel()
        self.item_progress = QProgressBar()
        self.item_progress.setMaximumWidth(150)
        self.item_progress.setRange(0, 1000)
        self.item_progress.setFormat("%p%")
        self.all_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setTextVisible(True)
        self.statusBar().addWidget(self.current, 1)
        for w in (self.item_label, self.item_progress, self.all_label, self.progress):
            w.setVisible(False)
            self.statusBar().addPermanentWidget(w)

        self._counts_timer = QTimer(self, interval=400, timeout=self._update_counts)
        restore_geometry(self, "verifier")
        self.systems.load(self.picker.path())
        self.scan_mode = ""
        self.last_stats: dict = {}
        QTimer.singleShot(0, lambda: self.start_scan(mode=sc.REMEMBERED))

    # ------------------------------------------------------------------ actions
    def _library_changed(self, path: str):
        self.cfg["library_root"] = path
        self.cfg.save()
        self.systems.load(Path(path).expanduser())

    def manage_dats(self):
        DatDialog(self.picker.path(), self).exec()
        self.systems.load(self.picker.path())

    def open_renamer(self):
        from ..compat import launch_app
        launch_app("renamer")

    def _filter_changed(self):
        self.proxy.set(statuses={s for s, b in self.chips.items() if b.isChecked()})

    def _only(self, status: str):
        for s, b in self.chips.items():
            b.blockSignals(True)
            b.setChecked(s == status)
            b.blockSignals(False)
        self._filter_changed()

    def _update_counts(self):
        counts = {s: 0 for s in sc.STATUS_ORDER}
        for r in self.model.rows:
            counts[r.status] = counts.get(r.status, 0) + 1
        for s, b in self.chips.items():
            b.setText(f"{s}: {counts[s]}")
            # only what there is (and what was switched off): all of them need a very wide window
            b.setVisible(bool(counts[s]) or not b.isChecked() or s == sc.VERIFIED)
        wrong = [r for r in self.model.rows if r.media_wrong]
        self.media_chip.setText(f"Wrong CHD type: {len(wrong)}")
        running = bool(self.worker and self.worker.isRunning())
        self.act_fix.setEnabled(not running and any(r.media.fixable for r in wrong))

    def start_scan(self, items: list[sc.Item] | None = None, mode: str = sc.QUICK,
                   targets: list[Path] | None = None):
        if self.worker and self.worker.isRunning():
            if targets:
                QMessageBox.information(self, "Busy", "Wait for the current scan to finish, or press Stop.")
            return
        root = self.picker.path()
        systems = self.systems.checked()
        if targets is not None:
            items = None
        elif items is None and not systems:
            if mode != sc.REMEMBERED:
                QMessageBox.information(self, "Scan", "Tick at least one system on the left.")
            return
        if mode == sc.FULL and items is None:
            box = QMessageBox(QMessageBox.Icon.Question, "Full scan",
                              f"Re-hash every ROM in {len(systems)} system(s) from scratch?",
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
            box.setInformativeText("Remembered results are ignored and replaced. CHDs are unpacked again, "
                                   "which can take hours for a large library. Quick scan (F5) only checks new "
                                   "or changed ROMs.")
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        if targets is None and items is None and mode in (sc.QUICK, sc.FULL) \
                and self._offer_missing_dats(systems, mode):
            return                      # downloading first; the scan starts when that's done
        if targets is None:
            self.systems.remember()
        if items is None:
            self.model.clear()
        else:
            self.model.remove_paths({it.path for it in items})
        self.targets = targets
        cfg = self.cfg
        scanner = sc.make_scanner(cfg, self.cache, mode)
        scanner.marks = self.marks
        self.scanner = scanner
        self.scan_mode = mode
        self.run_mode = "RAM" if scanner.ram_mode else "SSD"
        indexes_cache = self.indexes

        def job(w: ScanWorker):
            scanner.cancel = w.cancel
            from .. import library
            if targets is not None:
                todo, indexes = library.collect_paths(cfg, targets, indexes_cache,
                                                      lambda msg: w.progress.emit(0, 0, msg, 0))
            elif items is None:
                todo, indexes = library.collect(cfg, root, systems, indexes_cache,
                                                lambda msg: w.progress.emit(0, 0, msg, 0))
            else:   # re-check of selected rows: their systems may be detected ones or the "other" group
                todo = items
                keys = sorted({it.system for it in todo})
                indexes = {library.OTHER: library.shared_index(library.all_dats(cfg))} if library.OTHER in keys else {}
                for k in keys:
                    if k != library.OTHER:
                        indexes[k] = indexes_cache.get(cfg.system(k)["dats"]) if cfg.system(k)["dats"] else \
                            indexes_cache.get(library.all_dats(cfg))
            scanner.run(todo, indexes, w.result.emit, lambda d, t, msg, f: w.progress.emit(d, t, msg, f))
            return len(todo)

        self.worker = ScanWorker(job, parent=self)
        self.worker.result.connect(self.model.replace if items else self.model.add)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.started = time.monotonic()
        self._set_running(True)
        self.worker.start()

    def _offer_missing_dats(self, systems: list[str], mode: str) -> bool:
        """Ticked systems whose DAT isn't downloaded yet: offer to get them before scanning.
        Returns True when downloads were started (the scan restarts after them)."""
        from .. import library
        from ..datmanager import system_for
        from ..systems import UNVERIFIABLE_HINT
        cfg = self.cfg
        declined = set(cfg["window_state"].get("dat_prompt_declined", []))
        missing = []
        for f in systems:
            if f == library.OTHER or f in UNVERIFIABLE_HINT or f in declined:
                continue
            if any(Path(d).is_file() for d in cfg.system(f)["dats"]):
                continue
            sysdef = system_for(cfg, f)
            if sysdef and sysdef.url:
                missing.append((f, sysdef))
        if not missing:
            return False
        box = QMessageBox(QMessageBox.Icon.Question, "Missing DATs",
                          f"{len(missing)} ticked system(s) have no DAT yet, so their ROMs can't be verified:",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setInformativeText("\n".join(f"• {f}  ({d.name})" for f, d in missing) +
                               "\n\nDownload them now? (No-Intro / Redump, a few seconds each)")
        box.button(QMessageBox.StandardButton.Yes).setText("Download and scan")
        box.button(QMessageBox.StandardButton.No).setText("Scan without them")
        dont = QCheckBox("Don't ask again for these systems")
        box.setCheckBox(dont)
        if box.exec() != QMessageBox.StandardButton.Yes:
            if dont.isChecked():
                cfg["window_state"]["dat_prompt_declined"] = sorted(declined | {f for f, _ in missing})
                cfg.save()
            return False
        from ..datmanager import download
        from ..config import DAT_DIR
        self.statusBar().showMessage(f"Downloading {len(missing)} DAT(s)…")
        self.act_scan.setEnabled(False)
        self.act_full.setEnabled(False)

        def job(w):
            out = {}
            for i, (f, sysdef) in enumerate(missing):
                w.progress.emit(i, len(missing), f"Downloading {sysdef.name}…", 0)
                try:
                    out[f] = (str(download(sysdef)), None)
                except Exception as exc:  # noqa: BLE001
                    out[f] = (None, str(exc))
            return out

        def finished(results):
            self.act_scan.setEnabled(True)
            self.act_full.setEnabled(True)
            errs = []
            for f, (path, err) in results.items():
                if err:
                    errs.append(f"{f}: {err}")
                    continue
                st = cfg.system(f)
                st["dats"] = [path] + [d for d in st["dats"] if d != path and Path(d).parent != DAT_DIR]
            cfg.save()
            self.systems.load(self.picker.path())
            if errs:
                QMessageBox.warning(self, "Some DAT downloads failed", "\n".join(errs) +
                                    "\n\nThose systems are scanned without a DAT (Manage DATs to retry).")
            # remember the declined ones too, so the restarted scan doesn't ask again for a failed download
            cfg["window_state"]["dat_prompt_declined"] = sorted(declined | {f for f in results if results[f][1]})
            self.start_scan(None, mode)

        w = Worker(job, parent=self)
        w.progress.connect(lambda d, t, msg, _f: self.statusBar().showMessage(msg))
        w.done.connect(finished)
        w.failed.connect(lambda e: (self.act_scan.setEnabled(True), self.act_full.setEnabled(True),
                                    QMessageBox.critical(self, "Download DATs", e)))
        self._dat_worker = w
        w.start()
        return True

    def stop_scan(self):
        if self.worker:
            self.worker.cancel.set()
            self.current.setText("Stopping… (waiting for running files)")

    def _set_running(self, on: bool, fixing: bool = False):
        what = "CHD" if fixing else "file"
        self.item_label.setText(f"<b>This {what}:</b>" if fixing else f"<b>Current {what}:</b>")
        self.item_label.setToolTip(f"Progress of the {what} being {'converted' if fixing else 'hashed'} right now")
        self.item_progress.setToolTip(self.item_label.toolTip())
        self.all_label.setText(f"<b>All {what}s:</b>")
        self.all_label.setToolTip(f"How many {what}s of this run are finished")
        self.progress.setToolTip(self.all_label.toolTip())
        for w in (self.item_label, self.item_progress, self.all_label, self.progress):
            w.setVisible(on)
        self.act_scan.setEnabled(not on)
        self.act_full.setEnabled(not on)
        self.act_fix.setEnabled(not on and any(r.media_wrong and r.media.fixable for r in self.model.rows))
        self.act_stop.setEnabled(on)
        if on:
            self._counts_timer.start()
            self.progress.setRange(0, 0)
        else:
            self._counts_timer.stop()
            self._update_counts()

    def _on_progress(self, done: int, total: int, msg: str, frac: float):
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress.setFormat(f"{done} of {total} done")
        self.current.setText(msg)
        self.item_progress.setValue(int(frac * 1000))

    def _on_done(self, n):
        self._set_running(False)
        el = time.monotonic() - self.started
        cancelled = self.worker.cancel.is_set()
        took = f"{el / 60:.1f} min" if el > 90 else f"{el:.0f} s"
        st = self.scanner.stats
        if self.scan_mode == sc.REMEMBERED:
            msg = (f"Showing {len(self.model.rows)} results remembered from previous scans"
                   + (f" · {st['not_scanned']} ROM(s) never scanned or changed since — run a Quick scan (F5)"
                      if st["not_scanned"] else " · everything is up to date"))
            if not self.model.rows and not st["not_scanned"]:
                msg = "Ready — run a Quick scan (F5)"
        else:
            kind = "Full scan" if self.scan_mode == sc.FULL else "Quick scan"
            if getattr(self, "targets", None):
                names = ", ".join(p.name for p in self.targets[:3]) + (" …" if len(self.targets) > 3 else "")
                kind = f"Check of {names}"
            games = sum(1 for r in self.model.rows if r.item.member)
            count = (f"{n} files, {len(self.model.rows)} games" if games and self.scan_mode != sc.FULL
                     else f"{n} files")
            msg = (f"{kind} {'stopped' if cancelled else 'finished'} in {took}: {count} "
                   f"({st['remembered']} from earlier scans, {st['hashed']} hashed now; {self.run_mode} mode). "
                   f"Next run uses {'RAM' if self.cfg['temp_mode'] == 'ram' else 'SSD'} mode.")
        self.current.setText(msg)
        QApplication.alert(self)
        if not cancelled and self.scan_mode in (sc.QUICK, sc.FULL):
            QTimer.singleShot(0, self._offer_found_dats)

    def _offer_found_dats(self):
        """ROMs recognised by their header whose system has no DAT at all yet (in any folder): offer to get it and
        check just those again."""
        from .. import library
        from ..datmanager import dat_filename
        from ..systems import BY_ID
        have = {Path(d).name for d in library.all_dats(self.cfg)}
        declined = set(self.cfg["window_state"].get("dat_prompt_declined", []))
        rows: dict[str, list] = {}
        for r in self.model.rows:
            if r.found_by == "header" and r.status in (sc.UNKNOWN, sc.BAD, sc.NODAT, sc.MODIFIED) and r.found in BY_ID:
                d = BY_ID[r.found]
                if d.url and dat_filename(d) not in have and f"found:{r.found}" not in declined \
                        and (r.misplaced or not (self.cfg["systems"].get(r.item.system) or {}).get("dats")):
                    rows.setdefault(r.found, []).append(r)
        if not rows or (self.worker and self.worker.isRunning()):
            return
        box = QMessageBox(QMessageBox.Icon.Question, "DATs for the systems found",
                          f"ROMs of {len(rows)} system(s) were recognised by their own header, but there is no DAT to "
                          f"verify them with yet:",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setInformativeText("\n".join(f"• {BY_ID[k].name}: {len(v)} ROM(s)" for k, v in rows.items()) +
                               "\n\nDownload them and check those ROMs again?")
        box.button(QMessageBox.StandardButton.Yes).setText("Download and check")
        box.button(QMessageBox.StandardButton.No).setText("Not now")
        dont = QCheckBox("Don't ask again for these systems")
        box.setCheckBox(dont)
        if box.exec() != QMessageBox.StandardButton.Yes:
            if dont.isChecked():
                self.cfg["window_state"]["dat_prompt_declined"] = sorted(declined | {f"found:{k}" for k in rows})
                self.cfg.save()
            return
        from ..datmanager import download
        self.act_scan.setEnabled(False)
        self.act_full.setEnabled(False)

        def job(w):
            errs = {}
            for i, k in enumerate(rows):
                w.progress.emit(i, len(rows), f"Downloading {BY_ID[k].name}…", 0)
                try:
                    download(BY_ID[k])
                except Exception as exc:  # noqa: BLE001
                    errs[k] = str(exc)
            return errs

        def finished(errs):
            self.act_scan.setEnabled(True)
            self.act_full.setEnabled(True)
            if errs:
                QMessageBox.warning(self, "Some DAT downloads failed",
                                    "\n".join(f"{BY_ID[k].name}: {e}" for k, e in errs.items()))
            again = [r.item for k, v in rows.items() if k not in errs for r in v]
            if again:
                self.start_scan(again)

        w = Worker(job, parent=self)
        w.progress.connect(lambda d, t, msg, _f: self.statusBar().showMessage(msg))
        w.done.connect(finished)
        w.failed.connect(lambda e: (self.act_scan.setEnabled(True), self.act_full.setEnabled(True),
                                    QMessageBox.critical(self, "Download DATs", e)))
        self._dat_worker = w
        w.start()

    def _on_failed(self, msg: str):
        self._set_running(False)
        self.current.setText("Failed")
        QMessageBox.critical(self, "Verification failed", msg)

    # ------------------------------------------------------------------ detail / context
    def _selected_results(self) -> list[sc.Result]:
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return [self.model.rows[r] for r in sorted(rows)]

    def _show_detail(self, cur, _prev):
        if not cur.isValid():
            return
        r = self.model.rows[self.proxy.mapToSource(cur).row()]
        e = html.escape
        col = STATUS_COLORS.get(r.status, "#888")
        out = [f"<h3 style='margin:0'>{e(r.item.label)}</h3>",
               f"<p><span style='color:{col};font-weight:bold'>{e(r.status)}</span> — {e(STATUS_HELP.get(r.status, ''))}"
               f"<br>{e(r.note)}</p>"]
        if r.tag:
            out.append(f"<p><b>Tag:</b> <span style='color:{STATUS_COLORS[sc.MODIFIED]}'><b>{e(r.tag)}</b></span> — "
                       f"{e(tg.TAGS.get(r.tag, 'custom tag'))} <small>({e(r.tag_source)}; without the tag: "
                       f"{e(r.orig_status or '')})</small></p>")
        if r.game:
            out.append(f"<p><b>DAT game:</b> {e(r.game)} <small>({e(r.dat)})</small></p>")
        if r.found or r.ident is not None:
            said = []
            if r.found:
                said.append(f"{e(sc.system_name(r.found))} <small>(from "
                            f"{e({'hash': 'the DAT its data matches', 'header': 'its own header'}.get(r.found_by, 'its own data'))})"
                            f"</small>")
            if r.ident is not None:
                said.append(f"<small>{e(r.ident.what)}" + (f", titled “{e(r.ident.title)}”" if r.ident.title else "") +
                            "</small>")
            out.append("<p><b>What the file itself says:</b> " + "; ".join(said) + "</p>")
        if r.misplaced:
            out.append(f"<p style='color:{STATUS_COLORS[sc.MISPLACED]}'>Lies in another system's folder. ROM Renamer → "
                       f"<i>Sort into system folders</i> moves it.</p>")
        if r.chd_info and "disc" in r.chd_info:
            d = r.chd_info["disc"]
            out.append(f"<p><b>Disc image:</b> {e(str(d.get('format') or d.get('container', '')).upper())}"
                       f"{', ' + e(d['title']) if d.get('title') else ''}"
                       f"{' (' + e(d['game_id']) + ')' if d.get('game_id') else ''}, "
                       f"disc SHA-1 {e(d.get('sha1', ''))} <small>(read with nodtool)</small></p>")
        elif r.chd_info and "package" in r.chd_info:
            p = r.chd_info["package"]
            rows = [("Title", p.get("title")), ("Type", p.get("kind")), ("Title ID", p.get("title_id")),
                    ("Base game ID", p.get("base_id") if p.get("base_id") != p.get("title_id") else ""),
                    ("Version", p.get("version_str")), ("Display version", p.get("display_version")),
                    ("Region", p.get("region")), ("Content ID", p.get("content_id")),
                    ("Needs firmware", p.get("firmware")), ("Backport", "yes" if p.get("backport") else ""),
                    ("Note", p.get("note"))]
            out.append("<p><b>Package:</b><br>" + "<br>".join(f"&nbsp;&nbsp;{k}: {e(str(v))}" for k, v in rows if v)
                       + "</p>")
            if len(p.get("contents", [])) > 1:
                out.append("<p><b>Inside:</b><br>" + "<br>".join(
                    f"&nbsp;&nbsp;{e(c['kind'])} {e(c['title_id'])} {e(c['version_str'])}"
                    f"{' (' + e(c['display_version']) + ')' if c.get('display_version') else ''}"
                    for c in p["contents"]) + "</p>")
            if r.chd_info.get("update"):
                out.append(f"<p><b>Updates:</b> {e(r.chd_info['update'])}</p>")
            for msg in r.chd_info.get("problems", []):
                out.append(f"<p style='color:{STATUS_COLORS[sc.ATTENTION]}'>⚠ {e(msg)}</p>")
        elif r.chd_info:
            out.append(f"<p><b>CHD:</b> v{r.chd_info.get('version')} {e(str(r.chd_info.get('kind', '')).upper())}, "
                       f"SHA-1 {e(r.chd_info.get('sha1', ''))}</p>")
        if r.media is not None:
            m = r.media
            if m.ok:
                out.append(f"<p><b>CHD type:</b> {m.actual.upper()} ✔ <small>({e(m.reason)})</small></p>")
            else:
                out.append(f"<p><b>CHD type:</b> <span style='color:{WRONG_MEDIA}'><b>{m.actual.upper()}, should be "
                           f"{m.expected.upper()}</b></span> — {e(m.reason)}<br>"
                           f"{'<b>Fixable:</b> ' if m.fixable else '<b>Not auto-fixable:</b> '}{e(m.fix_note)}"
                           + ("<br>Right-click → <i>Fix CHD type</i>, or use <i>Fix CHD types…</i> on the toolbar."
                              if m.fixable else "") + "</p>")
        if r.entries:
            out.append("<table cellspacing=0 cellpadding=3 border=1 style='border-collapse:collapse'>"
                       "<tr><th>Track / file</th><th>Size</th><th>CRC32</th><th>MD5</th><th>SHA-1</th>"
                       "<th>DAT match</th></tr>")
            for er in r.entries:
                h = er.entry.headerless if er.headerless and er.entry.headerless else er.entry.full
                match = (f"<span style='color:{STATUS_COLORS[sc.VERIFIED]}'>✔ {e(er.rom)}</span>"
                         if er.rom else f"<span style='color:{STATUS_COLORS[sc.BAD]}'>✘ no match</span>")
                hdr = (f"<br><small>compared {e(er.entry.norm or f'without its {er.entry.header_len}-byte header')}"
                       f"</small>") if er.headerless else ""
                out.append(f"<tr><td>{e(er.entry.name)}{hdr}</td><td>{h.size:,}</td><td><tt>{h.crc}</tt></td>"
                           f"<td><tt>{h.md5}</tt></td><td><tt>{h.sha1}</tt></td><td>{match}</td></tr>")
            out.append("</table>")
        out.append(f"<p><small>{e(str(r.item.path))}</small></p>")
        self.detail.setHtml("".join(out))

    def _context_menu(self, pos):
        res = self._selected_results()
        if not res:
            return
        m = QMenu(self)
        m.addAction(QIcon.fromTheme("folder-open"), "Open containing folder",
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(res[0].item.path.parent))))
        m.addAction(QIcon.fromTheme("edit-copy"), "Copy path(s)",
                    lambda: QGuiApplication.clipboard().setText("\n".join(str(r.item.path) for r in res)))
        m.addAction(QIcon.fromTheme("edit-copy"), "Copy SHA-1(s)", lambda: QGuiApplication.clipboard().setText(
            "\n".join(er.entry.full.sha1 for r in res for er in r.entries)))
        m.addAction(QIcon.fromTheme("edit-copy"), "Copy DAT game name(s)",
                    lambda: QGuiApplication.clipboard().setText("\n".join(r.game for r in res if r.game)))
        m.addSeparator()
        m.addAction(QIcon.fromTheme("view-refresh"), "Full re-scan of selected", lambda: self._recheck(res))
        m.addSeparator()
        tagm = m.addMenu(QIcon.fromTheme("tag"), f"Mark {'as' if len(res) == 1 else f'{len(res)} files as'}…")
        current = {r.tag for r in res}
        for name, desc in tg.TAGS.items():
            a = tagm.addAction(name, lambda n=name: self.mark(res, n))
            a.setToolTip(desc)
            a.setStatusTip(desc)
            a.setCheckable(True)
            a.setChecked(current == {name})
        tagm.addAction("Custom tag…", lambda: self._custom_tag(res))
        tagm.setToolTipsVisible(True)
        tagm.addSeparator()
        tagm.addAction("Not modified (remove tag)", lambda: self.mark(res, ""))
        tagm.addAction("Automatic (tag from file name)", lambda: self.mark(res, None))
        ps4 = [r for r in res if r.chd_info and r.chd_info.get("package", {}).get("platform") == "ps4"]
        if ps4:
            from .. import updates as up
            ids = sorted({r.chd_info["package"]["title_id"] for r in ps4})[:5]
            m.addSeparator()
            m.addAction(QIcon.fromTheme("internet-web-browser"), "Look up updates online (orbispatches.com)",
                        lambda: [QDesktopServices.openUrl(QUrl(up.ps4_patch_page(t))) for t in ids])
        fixable = [r for r in res if r.media_wrong and r.media.fixable]
        if fixable:
            m.addSeparator()
            a = m.addAction(QIcon.fromTheme("tools-wizard", QIcon.fromTheme("run-build")),
                            f"Fix CHD type of {len(fixable)} selected…", lambda: self.fix_media(fixable))
            a.setEnabled(not (self.worker and self.worker.isRunning()))
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ game updates (Switch)
    def check_updates(self):
        sw = [r for r in self.model.rows if r.chd_info and r.chd_info.get("package", {}).get("platform") == "switch"]
        ps4 = [r for r in self.model.rows if r.chd_info and r.chd_info.get("package", {}).get("platform") == "ps4"]
        if not sw:
            QMessageBox.information(self, "Game updates", "Scan your Switch folder first (with your Switch keys set "
                                    "in Settings)." + ("\n\nFor PS4 games, right-click a package → Look up updates "
                                                       "online." if ps4 else ""))
            return
        from .. import updates as up
        self.act_updates.setEnabled(False)
        self.statusBar().showMessage("Getting the Switch title database (once a day, ~1.7 MB)…")
        w = Worker(lambda _w: up.switch_versions(), parent=self)
        w.done.connect(lambda db: self._show_updates(sw, db))
        w.failed.connect(lambda e: (self.act_updates.setEnabled(True),
                                    QMessageBox.warning(self, "Game updates", f"Couldn't get the title database:\n{e}")))
        self._upd_worker = w
        w.start()

    def _show_updates(self, rows: list[sc.Result], db: dict):
        from .. import updates as up
        self.act_updates.setEnabled(True)
        have: dict[str, int] = {}          # base id -> newest update version you have
        for r in rows:
            p = r.chd_info["package"]
            for c in p.get("contents", []):
                if c["kind"] == "Update":
                    have[p["base_id"]] = max(have.get(p["base_id"], 0), c["version"])
        newer = 0
        for r in rows:
            p = r.chd_info["package"]
            latest = up.switch_latest(p["base_id"], db)
            mine = have.get(p["base_id"], 0)
            if latest is None:
                msg = "no update information for this title"
            elif latest.version > mine:
                msg = (f"⬆ update v{latest.version} ({latest.date}) available - you have "
                       f"{'v' + str(mine) if mine else 'no update'}")
                newer += 1
            else:
                msg = f"✔ newest update (v{latest.version}, {latest.date})"
            r.chd_info["update"] = msg
            r.note = r.note.split("  ⟶ ")[0] + "  ⟶ " + msg
        self.model.dataChanged.emit(self.model.index(0, 0),
                                    self.model.index(len(self.model.rows) - 1, len(COLS) - 1))
        games = len({r.chd_info["package"]["base_id"] for r in rows})
        self.statusBar().showMessage(f"Game updates: checked {games} Switch game(s); {newer} file(s) have a newer "
                                     f"update available (see Notes).", 15000)

    def _recheck(self, res: list[sc.Result]):
        import dataclasses
        unique = {}
        for r in res:   # several games of one archive: re-check the archive once (all its games come back)
            unique.setdefault(r.item.path, dataclasses.replace(r.item, member=""))
        self.start_scan(list(unique.values()), mode=sc.FULL)

    # ------------------------------------------------------------------ check any file / folder
    def check_files(self):
        from ..hashing import ARCHIVE_EXTS
        from ..scanner import ROM_EXTS
        exts = " ".join(f"*{e}" for e in sorted(ROM_EXTS | ARCHIVE_EXTS))
        start = self.cfg["window_state"].get("last_check_dir") or self.picker.path_str()
        files, _ = QFileDialog.getOpenFileNames(self, "Check ROM / archive files", start,
                                                f"ROMs and archives ({exts});;All files (*)")
        if files:
            self.check_paths([Path(f) for f in files])

    def check_folder(self):
        start = self.cfg["window_state"].get("last_check_dir") or self.picker.path_str()
        d = QFileDialog.getExistingDirectory(self, "Check a folder (searched 3 levels deep)", start)
        if d:
            self.check_paths([Path(d)])

    def check_paths(self, paths: list[Path]):
        paths = [p for p in paths if p.exists()]
        if not paths:
            return
        self.cfg["window_state"]["last_check_dir"] = str(paths[0] if paths[0].is_dir() else paths[0].parent)
        self.cfg.save()
        self.start_scan(mode=sc.QUICK, targets=paths)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.check_paths([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])

    # ------------------------------------------------------------------ hack / translation tags
    def mark(self, res: list[sc.Result], tag: str | None):
        """tag: a tag name; "" = not modified; None = back to automatic (file-name) detection."""
        for r in res:
            self.marks.set(tg.content_key(r), tag, r.item.label)
            tg.apply(r, self.marks, sc.MODIFIED, sc.TAGGABLE)
            self.model.replace(r)
        self.proxy.invalidateFilter()
        self._update_counts()
        cur = self.table.currentIndex()
        if cur.isValid():
            self._show_detail(cur, None)
        what = {None: "set back to automatic", "": "marked as not modified"}.get(tag, f"tagged {tag}")
        self.statusBar().showMessage(f"{len(res)} file(s) {what}. Tags are remembered by file content, so they "
                                     f"survive renames and moves.", 8000)

    def _custom_tag(self, res: list[sc.Result]):
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Custom tag", "Tag (e.g. Kaizo, Randomizer, 60Hz patch):")
        if ok and text.strip():
            self.mark(res, text.strip()[:40])

    # ------------------------------------------------------------------ CHD type fixing
    def _fix_candidates(self) -> list[sc.Result]:
        sel = [r for r in self._selected_results() if r.media_wrong and r.media.fixable]
        return sel or [r for r in self.model.rows if r.media_wrong and r.media.fixable]

    def fix_media(self, res: list[sc.Result]):
        from .. import chdfix
        if not res or (self.worker and self.worker.isRunning()):
            return
        cfg = self.cfg
        ram = cfg["temp_mode"] == "ram"
        biggest = max((r.chd_info.get("frames", 0) * 2048 if r.media.actual == "cd"
                       else r.chd_info.get("logical_size", 0)) for r in res)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Fix CHD types")
        box.setText(f"Convert {len(res)} CHD(s) to the right media type?")
        box.setInformativeText(
            f"For each one, the disc image is unpacked to {'RAM' if ram else 'the SSD temp folder'} "
            f"(largest: {human(biggest)}), a new CHD is created next to the original, verified, and read back "
            f"to prove its data is identical. Only then is the original moved to the trash (restorable) and the "
            f"new CHD given its name. Any failure leaves the original untouched.\n\n"
            f"Takes about 1–3 minutes per GB. The 'Unpack CHDs in RAM / on the SSD' choice on the toolbar applies.")
        box.setDetailedText("\n".join(f"• {r.item.path.name}: {r.media.actual.upper()} → {r.media.expected.upper()}"
                                      f" ({r.media.reason})" for r in res))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        tmp_ssd, cache, indexes_cache = Path(cfg["tmp_dir"]), self.cache, self.indexes

        def job(w: ScanWorker):
            outcomes = []
            for i, r in enumerate(res):
                if w.cancel.is_set():
                    outcomes.append((r.item.path.name, False, "not started (stopped)"))
                    continue
                name = r.item.path.name
                fr = chdfix.fix(r.item.path, r.media.expected, ram, tmp_ssd, w.cancel,
                                log=lambda s, n=name: w.progress.emit(i, len(res), f"{n}: {s.strip('= ')}", -1),
                                progress=lambda f, stage, n=name: w.progress.emit(
                                    i, len(res), f"Fixing {n} — {stage}", f))
                outcomes.append((name, fr.ok, fr.message))
                if fr.ok:
                    st = r.item.path.stat()
                    cache.put(r.item.path, st.st_size, st.st_mtime, "chd", fr.info, fr.entries)
                    scanner = sc.make_scanner(cfg, cache, sc.QUICK)
                    scanner.marks = self.marks
                    index = indexes_cache.get(cfg.system(r.item.system)["dats"])
                    new = scanner.check(r.item, index) if index else None
                    if new is not None:
                        w.result.emit(new)
            return outcomes

        self.worker = ScanWorker(job, parent=self)
        self.worker.result.connect(self.model.replace)
        self.worker.progress.connect(self._on_fix_progress)
        self.worker.done.connect(self._fix_done)
        self.worker.failed.connect(self._on_failed)
        self.started = time.monotonic()
        self._set_running(True, fixing=True)
        self.worker.start()

    def _on_fix_progress(self, done: int, total: int, msg: str, frac: float):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{done} of {total} done")
        self.current.setText(msg)
        if frac >= 0:
            self.item_progress.setValue(int(frac * 1000))

    def _fix_done(self, outcomes):
        self._set_running(False)
        ok = sum(1 for _n, good, _m in outcomes if good)
        el = time.monotonic() - self.started
        self.current.setText(f"CHD type fix: {ok} of {len(outcomes)} converted in {el / 60:.1f} min. "
                             f"Originals are in the trash.")
        detail = "\n".join(f"{'✔' if good else '✘'} {n}: {m}" for n, good, m in outcomes)
        box = QMessageBox(QMessageBox.Icon.Information if ok == len(outcomes) else QMessageBox.Icon.Warning,
                          "CHD type fix", f"{ok} of {len(outcomes)} CHD(s) converted.", parent=self)
        box.setDetailedText(detail)
        box.exec()
        QApplication.alert(self)

    # ------------------------------------------------------------------ export
    def export(self):
        if not self.model.rows:
            QMessageBox.information(self, "Export", "Run a verification first.")
            return
        path, flt = QFileDialog.getSaveFileName(self, "Export report", str(Path.home() / "rom-verify-report.csv"),
                                                "CSV spreadsheet (*.csv);;Plain text summary (*.txt)")
        if not path:
            return
        try:
            if path.endswith(".txt") or "txt" in flt:
                self._export_txt(Path(path))
            else:
                self._export_csv(Path(path))
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.current.setText(f"Report written to {path}")

    def _export_csv(self, path: Path):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["status", "system", "path", "dat_game", "dat", "track", "size", "crc32", "md5", "sha1",
                        "dat_rom", "note", "chd_type", "tag"])
            for r in self.model.rows:
                if not r.entries:
                    w.writerow([r.status, r.item.system, _csv_path(r), r.game, r.dat, "", "", "", "", "", "", r.note,
                                media_text(r), r.tag])
                for er in r.entries:
                    h = er.entry.full
                    w.writerow([r.status, r.item.system, _csv_path(r), r.game, r.dat, er.entry.name, h.size, h.crc,
                                h.md5, h.sha1, er.rom or "", r.note, media_text(r), r.tag])

    def _export_txt(self, path: Path):
        by: dict[str, list[sc.Result]] = {}
        for r in self.model.rows:
            by.setdefault(r.status, []).append(r)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"ROM Verifier report - {time.strftime('%Y-%m-%d %H:%M')}\n\n")
            for s in sc.STATUS_ORDER:
                fh.write(f"{s:10} {len(by.get(s, []))}\n")
            for s in sc.STATUS_ORDER:
                if s in by:
                    fh.write(f"\n== {s} ==\n")
                    for r in sorted(by[s], key=lambda r: (r.item.system, r.item.label.lower())):
                        fh.write(f"[{r.item.system}] {r.item.label}"
                                 + (f"  ->  {r.game}" if r.game and r.game != r.item.stem else "")
                                 + (f"   ({r.note})" if r.note else "") + "\n")

    def closeEvent(self, ev):
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(self, "Quit", "A verification is running. Stop it and quit?") != \
                    QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.worker.cancel.set()
            self.worker.wait(15000)
        self.systems.remember()
        save_geometry(self, "verifier")
        super().closeEvent(ev)


def main():
    app = make_app("ROM Verifier", "rom-verifier")
    app.setApplicationVersion(__version__)
    w = VerifierWindow()
    w.show()
    startup_update_check(w, "verifier")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
