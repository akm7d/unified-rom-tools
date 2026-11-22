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
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter, QTableView,
                               QTextBrowser, QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import __version__, chd
from .. import scanner as sc
from ..cache import HashCache
from ..config import get_config
from ..datmanager import IndexCache
from .common import (STATUS_COLORS, STATUS_HELP, LibraryPicker, SystemList, TempModeCombo, Worker, human, make_app,
                     restore_geometry, save_geometry)
from .datdialog import DatDialog

COLS = ["Status", "System", "File", "DAT game", "Size", "CRC32", "SHA-1", "Notes"]
C_STATUS, C_SYSTEM, C_FILE, C_GAME, C_SIZE, C_CRC, C_SHA1, C_NOTE = range(len(COLS))


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
            if old.item.path == r.item.path:
                self.rows[i] = r
                self._sizes[i] = r.size
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLS) - 1))
                return
        self.add(r)

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
            if c == C_SYSTEM:
                return r.item.system
            if c == C_FILE:
                return r.item.path.name
            if c == C_GAME:
                return r.game
            if c == C_SIZE:
                return human(self._sizes[idx.row()]) if self._sizes[idx.row()] else ""
            if c == C_CRC:
                return h.crc if h else (f"{len(r.entries)} tracks" if r.entries else "")
            if c == C_SHA1:
                return h.sha1 if h else ""
            if c == C_NOTE:
                return r.note + ("  (cached)" if r.cached else "")
        elif role == Qt.ItemDataRole.ForegroundRole and c == C_STATUS:
            return QColor(STATUS_COLORS.get(r.status, "#888"))
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
            if c == C_FILE:
                return str(r.item.path)
            if c == C_NOTE:
                return r.note
        elif role == Qt.ItemDataRole.UserRole:  # sort key
            if c == C_SIZE:
                return self._sizes[idx.row()]
            if c == C_STATUS:
                return sc.STATUS_ORDER.index(r.status) if r.status in sc.STATUS_ORDER else 99
            return self.data(idx)
        return None


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
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set(self, statuses=None, text=None):
        if statuses is not None:
            self.statuses = statuses
        if text is not None:
            self.text = text.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        r = self.sourceModel().rows[row]
        if r.status not in self.statuses:
            return False
        if self.text:
            hay = f"{r.item.path.name} {r.game} {r.item.system} {r.note}".lower()
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
        clear = QPushButton(QIcon.fromTheme("edit-clear-history"), "Forget all remembered scans")
        clear.setToolTip("Deletes the stored hashes. The next Quick scan will re-hash everything.")
        clear.clicked.connect(self._forget)
        self.cache = cache
        form = QFormLayout(self)
        form.addRow("Parallel files:", self.workers)
        form.addRow("Parallel CHD extractions:", self.chd)
        form.addRow("SSD-mode temp folder:", tmprow)
        form.addRow("", QLabel("<small>Only used in SSD mode (RAM mode streams through /dev/shm). "
                               "PS2 DVDs need up to ~9 GB each here.</small>"))
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
        cfg["tmp_dir"] = self.tmp.text().strip() or "/var/tmp/romtools"
        cfg.save()
        super().accept()


class VerifierWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ROM Verifier")
        self.resize(1400, 820)
        self.cfg = get_config()
        self.cache = HashCache()
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
        act_rename = QAction(QIcon.fromTheme("edit-rename"), "Open ROM Renamer", self)
        act_rename.triggered.connect(self.open_renamer)
        act_settings = QAction(QIcon.fromTheme("configure"), "Settings…", self)
        act_settings.triggered.connect(lambda: (SettingsDialog(self, self.cache).exec(), self.mode.sync()))
        for a in (self.act_scan, self.act_full, self.act_stop):
            tb.addAction(a)
        tb.addSeparator()
        self.mode = TempModeCombo(lambda: bool(self.worker and self.worker.isRunning()),
                                  lambda m: self.statusBar().showMessage(m, 8000))
        tb.addWidget(self.mode)
        tb.addSeparator()
        for a in (act_dats, act_export, act_rename, act_settings):
            tb.addAction(a)

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
        for c, w in ((C_STATUS, 110), (C_SYSTEM, 70), (C_FILE, 380), (C_GAME, 330), (C_SIZE, 80), (C_CRC, 80),
                     (C_SHA1, 120)):
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
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.progress.setTextVisible(True)
        self.current = QLabel("Ready")
        self.item_progress = QProgressBar()
        self.item_progress.setMaximumWidth(140)
        self.item_progress.setRange(0, 1000)
        self.item_progress.setTextVisible(False)
        self.item_progress.setVisible(False)
        self.statusBar().addWidget(self.current, 1)
        self.statusBar().addPermanentWidget(self.item_progress)
        self.statusBar().addPermanentWidget(self.progress)

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
        subprocess.Popen([sys.executable, "-m", "romtools.gui.renamer"],
                         cwd=str(Path(__file__).resolve().parents[2]))

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

    def start_scan(self, items: list[sc.Item] | None = None, mode: str = sc.QUICK):
        if self.worker and self.worker.isRunning():
            return
        root = self.picker.path()
        systems = self.systems.checked()
        if items is None and not systems:
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
        self.systems.remember()
        if items is None:
            self.model.clear()
        cfg = self.cfg
        scanner = sc.make_scanner(cfg, self.cache, mode)
        self.scanner = scanner
        self.scan_mode = mode
        self.run_mode = "RAM" if scanner.ram_mode else "SSD"
        indexes_cache = self.indexes

        def job(w: ScanWorker):
            scanner.cancel = w.cancel
            todo = items
            if todo is None:
                todo = []
                for s in systems:
                    w.progress.emit(0, 0, f"Listing {s}…", 0)
                    todo += sc.enumerate_system(s, root / s)
            indexes = {}
            for s in sorted({it.system for it in todo}):
                dats = cfg.system(s)["dats"]
                if dats:
                    w.progress.emit(0, 0, f"Loading DAT for {s}…", 0)
                indexes[s] = indexes_cache.get(dats)
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

    def stop_scan(self):
        if self.worker:
            self.worker.cancel.set()
            self.current.setText("Stopping… (waiting for running files)")

    def _set_running(self, on: bool):
        self.act_scan.setEnabled(not on)
        self.act_full.setEnabled(not on)
        self.act_stop.setEnabled(on)
        self.item_progress.setVisible(on)
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
            self.progress.setFormat(f"{done}/{total}")
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
            msg = (f"{kind} {'stopped' if cancelled else 'finished'} in {took}: {n} items "
                   f"({st['remembered']} from earlier scans, {st['hashed']} hashed now; {self.run_mode} mode). "
                   f"Next run uses {'RAM' if self.cfg['temp_mode'] == 'ram' else 'SSD'} mode.")
        self.current.setText(msg)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat("done")
        QApplication.alert(self)

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
        out = [f"<h3 style='margin:0'>{e(r.item.path.name)}</h3>",
               f"<p><span style='color:{col};font-weight:bold'>{e(r.status)}</span> — {e(STATUS_HELP.get(r.status, ''))}"
               f"<br>{e(r.note)}</p>"]
        if r.game:
            out.append(f"<p><b>DAT game:</b> {e(r.game)} <small>({e(r.dat)})</small></p>")
        if r.chd_info:
            out.append(f"<p><b>CHD:</b> v{r.chd_info.get('version')} {e(str(r.chd_info.get('kind', '')).upper())}, "
                       f"SHA-1 {e(r.chd_info.get('sha1', ''))}</p>")
        if r.entries:
            out.append("<table cellspacing=0 cellpadding=3 border=1 style='border-collapse:collapse'>"
                       "<tr><th>Track / file</th><th>Size</th><th>CRC32</th><th>MD5</th><th>SHA-1</th>"
                       "<th>DAT match</th></tr>")
            for er in r.entries:
                h = er.entry.headerless if er.headerless and er.entry.headerless else er.entry.full
                match = (f"<span style='color:{STATUS_COLORS[sc.VERIFIED]}'>✔ {e(er.rom)}</span>"
                         if er.rom else f"<span style='color:{STATUS_COLORS[sc.BAD]}'>✘ no match</span>")
                hdr = f"<br><small>header {er.entry.header_len} B skipped</small>" if er.headerless else ""
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
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _recheck(self, res: list[sc.Result]):
        self.start_scan([r.item for r in res], mode=sc.FULL)

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
                        "dat_rom", "note"])
            for r in self.model.rows:
                if not r.entries:
                    w.writerow([r.status, r.item.system, r.item.path, r.game, r.dat, "", "", "", "", "", "", r.note])
                for er in r.entries:
                    h = er.entry.full
                    w.writerow([r.status, r.item.system, r.item.path, r.game, r.dat, er.entry.name, h.size, h.crc,
                                h.md5, h.sha1, er.rom or "", r.note])

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
                    for r in sorted(by[s], key=lambda r: (r.item.system, r.item.path.name.lower())):
                        fh.write(f"[{r.item.system}] {r.item.path.name}"
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
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
