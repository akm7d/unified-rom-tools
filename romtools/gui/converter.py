"""ROM Optimizer (formerly ROM Converter): task queue (convert / compress / trim / fix / playlists), ROM patching,
the Save space advisor, and advanced chdman tools (every command, version & updates)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QKeySequence, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionProgressBar, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QDialog)

from .. import __version__
from .. import chdman_tool as ct
from .. import converter as cv
from .. import updater as up
from ..config import get_config
from ..compat import FROZEN, IS_WIN, find_tool, launch_app, resource_dir
from .common import ThemeButton, Worker, human, is_dark, make_app, restore_geometry, save_geometry

STATUS_COLOR = {"Queued": "#7f8790", "Running": "#2f7fd1", "Done": "#2e9d53", "Failed": "#d1452f",
                "Skipped": "#d49a1b", "Cancelled": "#8a6fc9"}
Q_TITLE, Q_SOURCE, Q_OUTPUT, Q_DETECT, Q_STATUS, Q_PROGRESS, Q_NOTE = range(7)


def mono() -> QFont:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        val = index.data(Qt.ItemDataRole.UserRole)
        if val is None:
            return super().paint(painter, option, index)
        opt = QStyleOptionProgressBar()
        opt.rect = QRect(option.rect.adjusted(3, 4, -3, -4))
        opt.minimum, opt.maximum = 0, 1000
        opt.progress = int(val * 1000)
        opt.text = f"{val * 100:.0f}%"
        opt.textVisible = True
        QApplication.style().drawControl(QStyle.ControlElement.CE_ProgressBar, opt, painter)


class RunnerWorker(Worker):
    job_changed = Signal(object)


def file_row(edit: QLineEdit, save: bool, parent, flt: str = "All files (*)", directory: bool = False) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    icon = QIcon.fromTheme("folder-open" if directory else "document-open" if not save else "document-save")
    btn = QPushButton(icon, "" if not icon.isNull() else "…")
    btn.setToolTip("Browse…")

    def pick():
        start = edit.text() or str(Path(get_config()["library_root"]))
        if directory:
            p = QFileDialog.getExistingDirectory(parent, "Choose folder", start)
        elif save:
            p, _ = QFileDialog.getSaveFileName(parent, "Output file", start, flt)
        else:
            p, _ = QFileDialog.getOpenFileName(parent, "Input file", start, flt)
        if p:
            edit.setText(p)

    btn.clicked.connect(pick)
    lay.addWidget(edit, 1)
    lay.addWidget(btn)
    return w


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}" if m else f"{s} s"


class JobCard(QFrame):
    """One running job: what it is, which step, chdman's live phase, progress, speed and ETA."""

    def __init__(self, job: cv.Job):
        super().__init__()
        self.job_id = job.id
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.title = QLabel()
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.step = QLabel()
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        self.phase = QLabel()
        self.timing = QLabel()
        self.timing.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        g = QGridLayout(self)
        g.setContentsMargins(8, 6, 8, 6)
        g.setVerticalSpacing(2)
        g.addWidget(self.title, 0, 0, 1, 2)
        g.addWidget(self.step, 1, 0)
        g.addWidget(self.timing, 1, 1)
        g.addWidget(self.bar, 2, 0, 1, 2)
        g.addWidget(self.phase, 3, 0, 1, 2)

    def refresh(self, j: cv.Job):
        now = time.time()
        self.title.setText(f"{j.title}")
        self.title.setToolTip(str(j.source))
        self.step.setText(j.step_label or "Starting…")
        self.bar.setValue(int(j.step_progress * 1000))
        self.bar.setFormat(f"this step {j.step_progress * 100:.0f}%  ·  whole job {j.progress * 100:.0f}%")
        bits = [f"elapsed {fmt_dur(now - j.started)}"] if j.started else []
        if j.step_started and j.step_progress > 0.02:
            el = now - j.step_started
            bits.append(f"step ETA {fmt_dur(el * (1 - j.step_progress) / j.step_progress)}")
        if j.started and j.progress > 0.02 and j.weight > 1:
            bits.append(f"{human(int(j.weight * j.progress / max(now - j.started, 0.1)))}/s")
        self.timing.setText("  ·  ".join(bits))
        out = ", ".join(o.name for o in j.outputs)
        self.phase.setText(f"chdman: {j.phase or '…'}" + (f"   →  {out}" if out else ""))


class NowRunning(QGroupBox):
    def __init__(self):
        super().__init__("Now running")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(6, 6, 6, 6)
        self.idle = QLabel("Nothing running. Press Start queue.")
        self.idle.setEnabled(False)
        self.lay.addWidget(self.idle)
        self.cards: dict[int, JobCard] = {}

    def sync(self, running: list[cv.Job]):
        ids = {j.id for j in running}
        for jid in [k for k in self.cards if k not in ids]:
            card = self.cards.pop(jid)
            self.lay.removeWidget(card)
            card.deleteLater()
        for j in running:
            if j.id not in self.cards:
                self.cards[j.id] = JobCard(j)
                self.lay.addWidget(self.cards[j.id])
            self.cards[j.id].refresh(j)
        self.idle.setVisible(not running)


class QueueStatusBar(QFrame):
    """Overall queue progress (weighted by file size), counts, elapsed time and ETA."""

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setMinimumWidth(260)
        self.counts = QLabel()
        self.counts.setTextFormat(Qt.TextFormat.RichText)
        self.timing = QLabel()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        title = QLabel("<b>Task queue — all jobs:</b>")
        title.setToolTip("Progress of the whole task queue. Each running job's own progress is in the "
                         "Now running panel above.")
        self.bar.setToolTip("All jobs in this run, weighted by file size (a 4 GB disc counts more than a 50 MB one)")
        self.timing.setToolTip("Time since the queue started, estimated time left for the whole queue, and "
                               "average throughput")
        lay.addWidget(title)
        lay.addWidget(self.bar, 2)
        lay.addWidget(self.counts, 3)
        lay.addWidget(self.timing)
        self.show_counts({}, 0)

    def show_counts(self, counts: dict[str, int], total: int):
        parts = []
        for st in ("Running", "Queued", "Done", "Failed", "Skipped", "Cancelled"):
            n = counts.get(st, 0)
            if n or st in ("Queued", "Done"):
                parts.append(f"<span style='color:{STATUS_COLOR[st]}'>●</span> {st} {n}")
        self.counts.setText("&nbsp;&nbsp;".join(parts) + (f"&nbsp;&nbsp;<small>({total} jobs)</small>"
                                                         if total else ""))


# ====================================================================== Batch tab

class BatchTab(QWidget):
    job_updated = Signal(object)     # a job's status/progress changed (Save space mirrors it per file)
    queue_finished = Signal()

    def __init__(self, win: "ConverterWindow"):
        super().__init__()
        self.win = win
        self.jobs: list[cv.Job] = []
        self.rows: dict[int, int] = {}
        self.worker: RunnerWorker | None = None
        self.runner: cv.Runner | None = None
        cfg = get_config()
        saved = cfg["window_state"].get("converter_opts", {})

        # ---- what to do
        self.preset = QComboBox()
        groups = [[cv.TO_CHD, cv.FROM_CHD, cv.RECOMPRESS, cv.FIX_CHD, cv.VERIFY, cv.INFO],
                  [cv.TO_RVZ, cv.FROM_RVZ, cv.TO_NSZ, cv.FROM_NSZ, cv.SWITCH_FS],
                  [cv.M3U, cv.TRIM, cv.UNTRIM]]
        for gi, group in enumerate(groups):
            if gi:
                self.preset.insertSeparator(self.preset.count())
            for key in group:
                self.preset.addItem(cv.PRESETS[key][0], key)
        self.preset.setCurrentIndex(max(0, self.preset.findData(saved.get("preset", cv.TO_CHD))))
        self.preset.currentIndexChanged.connect(self._preset_changed)

        self.out_mode = QComboBox()
        self.out_mode.addItems(["Next to each source file", "Into this folder:"])
        self.out_dir = QLineEdit(saved.get("output_dir", ""))
        self.out_dir.setPlaceholderText("Output folder")
        self.out_dir_row = file_row(self.out_dir, False, self, directory=True)
        self.out_mode.setCurrentIndex(1 if saved.get("output_dir") else 0)
        self.out_mode.currentIndexChanged.connect(lambda i: self.out_dir_row.setEnabled(i == 1))
        self.out_dir_row.setEnabled(self.out_mode.currentIndex() == 1)

        self.cd_codec = QComboBox()
        for k, (label, _v) in cv.CD_CODECS.items():
            self.cd_codec.addItem(label, k)
        self.dvd_codec = QComboBox()
        for k, (label, _v) in cv.DVD_CODECS.items():
            self.dvd_codec.addItem(label, k)
        self.cd_codec.setCurrentIndex(max(0, self.cd_codec.findData(saved.get("cd_codec", "default"))))
        self.dvd_codec.setCurrentIndex(max(0, self.dvd_codec.findData(saved.get("dvd_codec", "default"))))
        self.custom_codec = QLineEdit(saved.get("custom_codec", ""))
        self.custom_codec.setPlaceholderText("optional, e.g. lzma,zlib,huff,flac (overrides the above)")
        self.hunk = QComboBox()
        self.hunk.setEditable(True)
        self.hunk.addItem("chdman default", "")
        self.hunk.addItem("2048 — PSP / PS2 DVDs (faster seeking, slightly bigger)", "2048")
        self.hunk.addItem("19584 — CD default", "19584")
        self.hunk.setCurrentIndex(max(0, self.hunk.findData(saved.get("hunk", ""))))
        self.iso_as = QComboBox()
        self.iso_as.addItem("Detect from the disc, DAT and system folder (recommended)", "detect")
        self.iso_as.addItem("Always DVD", "dvd")
        self.iso_as.addItem("Always CD", "cd")
        self.iso_as.setToolTip("Detect reads a few KB of each .iso: PSP discs, PS2 (BOOT2) vs PS1 (BOOT) SYSTEM.CNF, "
                               "Xbox / GameCube / Wii / Saturn / Dreamcast headers. For PS2 it checks the Redump "
                               "DAT for CD vs DVD. Otherwise it uses the system folder, then size.\n"
                               "The Notes column shows what was detected for each file.")
        # "iso_mode" replaces the old "iso_as" setting so earlier saved choices don't switch detection off.
        self.iso_as.setCurrentIndex(max(0, self.iso_as.findData(saved.get("iso_mode", "detect"))))
        self.split_bin = QCheckBox("One .bin per track for multi-track discs (Redump layout)")
        self.split_bin.setChecked(saved.get("split_bin", True))
        self.overwrite = QCheckBox("Overwrite existing outputs")
        self.verify_after = QCheckBox("Verify each new CHD after creating it")
        self.verify_after.setChecked(saved.get("verify_after", True))
        self.trash = QCheckBox("Move source files to the trash after a verified success")
        self.trash.setToolTip("Only after chdman verify passes on the new CHD. For .cue/.gdi, their track files "
                              "are trashed too. Uses your desktop trash (restorable).")
        self.trash.setChecked(False)
        self.trash.toggled.connect(lambda on: on and self.verify_after.setChecked(True))
        self.parallel = QSpinBox(minimum=1, maximum=8, value=saved.get("parallel", 1))
        self.parallel.setToolTip("Jobs at once. chdman already uses all CPU cores per job; 1-2 is usually best.")
        self.procs = QSpinBox(minimum=0, maximum=os.cpu_count() or 64, value=saved.get("procs", 0))
        self.procs.setSpecialValueText("all cores")
        self.procs.setToolTip("Limit CPU threads chdman uses per job (--numprocessors)")

        opts = QGroupBox("Options")
        g = QFormLayout(opts)
        outrow = QHBoxLayout()
        outrow.addWidget(self.out_mode)
        outrow.addWidget(self.out_dir_row, 1)
        g.addRow("Output:", outrow)
        g.addRow("CD compression:", self.cd_codec)
        g.addRow("DVD compression:", self.dvd_codec)
        g.addRow("Custom codecs:", self.custom_codec)
        g.addRow("Hunk size:", self.hunk)
        g.addRow(".iso files are:", self.iso_as)
        g.addRow("Extract CDs:", self.split_bin)
        flags = QVBoxLayout()
        for w in (self.verify_after, self.trash, self.overwrite):
            flags.addWidget(w)
        g.addRow("Safety:", flags)
        from .. import tasks as _tasks
        self.keys = QLineEdit(get_config()["switch_keys"] or saved.get("switch_keys") or str(
            _tasks.find_switch_keys([Path(get_config()["library_root"])]) or ""))
        self.keys.setPlaceholderText("your prod.keys (needed for NSZ and RomFS / ExeFS)")
        self.keys.setToolTip("Your own console keys (prod.keys). Only passed to nsz / nstool, never copied. "
                             "Shared with ROM Verifier and Renamer.")
        self.keys_row = file_row(self.keys, False, self, "Keys (prod.keys keys.txt);;All files (*)")
        g.addRow("Switch keys:", self.keys_row)
        self.tkeys = QLineEdit(get_config()["title_keys"] or "")
        self.tkeys.setPlaceholderText("your title.keys (only for games whose package has no ticket)")
        self.tkeys.setToolTip("Title keys (rights ID = key, the hactool/nstool title.keys format). Most NSPs carry "
                              "their own ticket and don't need this. Shared with ROM Verifier and Renamer.")
        self.tkeys_row = file_row(self.tkeys, False, self, "Title keys (title.keys);;All files (*)")
        g.addRow("Title keys:", self.tkeys_row)
        par = QHBoxLayout()
        par.addWidget(QLabel("Jobs at once"))
        par.addWidget(self.parallel)
        par.addSpacing(16)
        par.addWidget(QLabel("CPU threads per job"))
        par.addWidget(self.procs)
        par.addStretch(1)
        g.addRow("Performance:", par)
        self.opts_rows = {"codec": [self.cd_codec, self.dvd_codec, self.custom_codec, self.hunk],
                          "iso": [self.iso_as], "split": [self.split_bin], "verify": [self.verify_after],
                          "trash": [self.trash], "keys": [self.keys_row], "tkeys": [self.tkeys_row]}
        self.opts_form = g

        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Task:</b>"))
        top.addWidget(self.preset, 1)
        add_files = QPushButton(QIcon.fromTheme("list-add"), "Add files…")
        add_files.clicked.connect(self.add_files)
        add_dir = QPushButton(QIcon.fromTheme("folder-new"), "Add folder…")
        add_dir.clicked.connect(self.add_folder)
        top.addWidget(add_files)
        top.addWidget(add_dir)

        # ---- queue
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Job", "Source", "Output", "Detected as", "Status", "Progress",
                                              "Notes"])
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setItemDelegateForColumn(Q_PROGRESS, ProgressDelegate(self.table))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for c, wdt in ((Q_TITLE, 250), (Q_SOURCE, 170), (Q_OUTPUT, 200), (Q_DETECT, 330), (Q_STATUS, 80),
                       (Q_PROGRESS, 110)):
            self.table.setColumnWidth(c, wdt)
        self.table.currentCellChanged.connect(lambda r, *_: self._show_log(r))
        self.setAcceptDrops(True)

        self.btn_start = QPushButton(QIcon.fromTheme("media-playback-start"), "Start queue")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton(QIcon.fromTheme("media-playback-stop"), "Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        btn_remove = QPushButton(QIcon.fromTheme("list-remove"), "Remove selected")
        btn_remove.clicked.connect(self.remove_selected)
        btn_clear = QPushButton(QIcon.fromTheme("edit-clear"), "Clear finished")
        btn_clear.clicked.connect(self.clear_finished)
        btn_retry = QPushButton(QIcon.fromTheme("view-refresh"), "Retry failed")
        btn_retry.clicked.connect(self.retry_failed)
        self.summary = QLabel("Queue is empty — add files or a folder, or drop them here.")
        self.follow = QCheckBox("Follow running job")
        self.follow.setToolTip("Keep the running job selected so its live chdman output shows below")
        self.follow.setChecked(True)
        qrow = QHBoxLayout()
        for b in (self.btn_start, self.btn_stop, btn_remove, btn_clear, btn_retry):
            qrow.addWidget(b)
        qrow.addWidget(self.summary, 1)
        qrow.addWidget(self.follow)
        self.now = NowRunning()
        self.qbar = QueueStatusBar()
        self.run_ids: list[int] = []
        self.started = 0.0
        self._followed = -1
        self.tick = QTimer(self, interval=500, timeout=self._tick)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono())
        self.log.setPlaceholderText("Select a job to see its chdman output.")
        self.log.setMaximumBlockCount(5000)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(opts)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        ll.addWidget(scroll, 1)

        right = QSplitter(Qt.Orientation.Vertical)
        qw = QWidget()
        ql = QVBoxLayout(qw)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.addWidget(self.table, 1)
        ql.addLayout(qrow)
        right.addWidget(qw)
        right.addWidget(self.now)
        right.addWidget(self.log)
        right.setSizes([430, 130, 170])

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([470, 930])
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addWidget(self.qbar)
        self._preset_changed()

    # -------------------------------------------------------------- options
    def _preset_changed(self):
        p = self.preset.currentData()
        conversions = (cv.TO_CHD, cv.RECOMPRESS, cv.TO_RVZ, cv.FROM_RVZ, cv.TO_NSZ, cv.FROM_NSZ, cv.TRIM, cv.UNTRIM)
        show = {"codec": p in (cv.TO_CHD, cv.RECOMPRESS), "iso": p == cv.TO_CHD, "split": p == cv.FROM_CHD,
                "verify": p in (cv.TO_CHD, cv.RECOMPRESS), "trash": p in conversions,
                "keys": p in (cv.TO_NSZ, cv.FROM_NSZ, cv.SWITCH_FS), "tkeys": p == cv.SWITCH_FS}
        self.trash.setText("Move source files to the trash after a verified success"
                           if p in (cv.TO_CHD, cv.RECOMPRESS) else
                           "Move the originals to the trash once the result is verified")
        self.trash.setToolTip("Trim / untrim with output next to the source always replace the file (the original "
                              "goes to the trash after the check)." if p in (cv.TRIM, cv.UNTRIM) else
                              "Only after the result was checked against the original.")
        for key, widgets in self.opts_rows.items():
            for w in widgets:
                self.opts_form.setRowVisible(w, show[key]) if self.opts_form.indexOf(w) >= 0 else w.setVisible(
                    show[key])

    def options(self) -> cv.Options:
        return cv.Options(
            output_dir=self.out_dir.text().strip() if self.out_mode.currentIndex() == 1 else "",
            overwrite=self.overwrite.isChecked(), cd_codec=self.cd_codec.currentData(),
            dvd_codec=self.dvd_codec.currentData(), custom_codec=self.custom_codec.text(),
            hunk_size=(self.hunk.currentData() if self.hunk.currentIndex() >= 0 and
                       self.hunk.currentText() == self.hunk.itemText(self.hunk.currentIndex())
                       else self.hunk.currentText()) or "",
            iso_as=self.iso_as.currentData(), split_bin=self.split_bin.isChecked(),
            processors=self.procs.value(), verify_after=self.verify_after.isChecked(),
            trash_sources=self.trash.isChecked(), dat_lookup=self._dat_for, all_dats=self._all_dats,
            switch_keys=self.keys.text().strip(), title_keys=self.tkeys.text().strip(),
            tmp_dir=get_config()["tmp_dir"],
            ram=get_config()["temp_mode"] == "ram")

    def _all_dats(self):
        from ..datmanager import IndexCache
        from ..library import all_dats
        if not hasattr(self, "_indexes"):
            self._indexes = IndexCache()
        dats = all_dats(get_config())
        return self._indexes.get(dats) if dats else None

    def _dat_for(self, path: Path):
        """DAT index of the library system a file belongs to (by folder name), for PS2 CD/DVD checks."""
        from ..datmanager import IndexCache
        from ..systems import guess_system
        if not hasattr(self, "_indexes"):
            self._indexes = IndexCache()
        cfg = get_config()
        for folder in (path.parent.name, "ps2"):
            dats = cfg.system(folder)["dats"] if folder in cfg["systems"] else []
            sysdef = guess_system(folder)
            if dats and sysdef and sysdef.id == "ps2":
                try:
                    return self._indexes.get(dats)
                except Exception:  # noqa: BLE001
                    return None
        return None

    def save_state(self):
        o = self.options()
        cfg = get_config()
        cfg["window_state"]["converter_opts"] = {
            "preset": self.preset.currentData(), "output_dir": o.output_dir, "cd_codec": o.cd_codec,
            "dvd_codec": o.dvd_codec, "custom_codec": o.custom_codec, "hunk": o.hunk_size, "iso_mode": o.iso_as,
            "split_bin": o.split_bin, "verify_after": o.verify_after, "parallel": self.parallel.value(),
            "procs": o.processors, "switch_keys": o.switch_keys}
        if o.switch_keys and Path(o.switch_keys).is_file():
            cfg["switch_keys"] = o.switch_keys      # shared with ROM Verifier / Renamer (reading Switch packages)
        if o.title_keys and Path(o.title_keys).is_file():
            cfg["title_keys"] = o.title_keys
        cfg.save()

    # -------------------------------------------------------------- queue
    def add_files(self):
        preset = self.preset.currentData()
        exts = " ".join(f"*{e}" for e in cv.PRESETS[preset][1])
        files, _ = QFileDialog.getOpenFileNames(self, "Add files", get_config()["library_root"],
                                                f"Supported ({exts});;All files (*)")
        if files:
            self.enqueue([Path(f) for f in files])

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder (searched recursively)", get_config()["library_root"])
        if d:
            self.enqueue([Path(d)])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.enqueue([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])

    def enqueue(self, paths: list[Path]):
        preset = self.preset.currentData()
        if preset == cv.M3U:
            jobs = cv.plan_playlists(paths, self.options())
            if not jobs:
                QMessageBox.information(self, "Playlists", "No multi-disc games (files with “(Disc N)” in the name) "
                                                           "were found there.")
                return
            self.add_jobs(jobs)
            return
        if preset in (cv.TO_NSZ, cv.FROM_NSZ, cv.SWITCH_FS) and not Path(self.keys.text().strip()).is_file():
            if not self.ask_for_keys("prod"):
                return
        files = cv.collect(paths, preset)
        if not files:
            QMessageBox.information(self, "Nothing to add",
                                    f"No matching files ({', '.join(cv.PRESETS[preset][1])}) were found.")
            return
        queued = {(j.source, j.steps[0][1] if j.steps else "") for j in self.jobs if j.status == "Queued"}
        jobs = cv.plan(preset, files, self.options())
        if preset == cv.SWITCH_FS and any(cv.NEEDS_TITLE_KEY in j.message for j in jobs) \
                and self.ask_for_keys("title", sum(cv.NEEDS_TITLE_KEY in j.message for j in jobs)):
            jobs = cv.plan(preset, files, self.options())          # again, with the title.keys just set
        jobs = [j for j in jobs if (j.source, j.steps[0][1] if j.steps else "") not in queued]
        self.add_jobs(jobs)
        self.save_state()

    def ask_for_keys(self, which: str, count: int = 0) -> bool:
        """Missing keys: offer to pick the file now (saved for all three apps). True if one was set."""
        if which == "prod":
            text = ("This task needs your Switch <b>prod.keys</b> (your console's keys - romtools never includes "
                    "any). Locate the file now?")
            title, flt, edit, key = "Switch keys needed", "Keys (prod.keys keys.txt);;All files (*)", self.keys, \
                "switch_keys"
        else:
            text = (f"{count} game(s) have no ticket in their package, so their <b>title key</b> must come from "
                    f"your <b>title.keys</b> file. Locate it now?<br><small>Without it those games are skipped; "
                    f"the others are added either way.</small>")
            title, flt, edit, key = "Title keys needed", "Title keys (title.keys);;All files (*)", self.tkeys, \
                "title_keys"
        if QMessageBox.question(self, title, text) != QMessageBox.StandardButton.Yes:
            return False
        f, _ = QFileDialog.getOpenFileName(self, title, str(Path(edit.text()).parent) if edit.text() else
                                           str(Path.home()), flt)
        if not f:
            return False
        edit.setText(f)
        cfg = get_config()
        cfg[key] = f
        cfg.save()
        return True

    def enqueue_preset(self, preset: str, files: list[Path], **overrides) -> int:
        """Queue files for a task chosen elsewhere (Save space), using the current options plus overrides."""
        import dataclasses
        opts = dataclasses.replace(self.options(), **overrides)
        jobs = cv.plan(preset, files, opts)
        self.add_jobs(jobs)
        return len(jobs)

    def add_jobs(self, jobs: list[cv.Job]):
        for j in jobs:
            self.jobs.append(j)
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.rows[j.id] = r
            for c, text in ((Q_TITLE, j.title), (Q_SOURCE, str(j.source)),
                            (Q_OUTPUT, ", ".join(o.name for o in j.outputs) or "—")):
                it = QTableWidgetItem(text)
                it.setToolTip(text if c != Q_OUTPUT else "\n".join(map(str, j.outputs)))
                self.table.setItem(r, c, it)
            det = QTableWidgetItem(j.detail)
            det.setToolTip(j.detail)
            if "⚠" in j.detail:
                det.setForeground(QColor("#d49a1b"))
            self.table.setItem(r, Q_DETECT, det)
            self.table.setItem(r, Q_STATUS, QTableWidgetItem())
            self.table.setItem(r, Q_PROGRESS, QTableWidgetItem())
            self.table.setItem(r, Q_NOTE, QTableWidgetItem())
            self.update_job(j)
        self._summary()

    def is_running(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def start_when_free(self):
        """Start the queue now, or right after the current run if one is going."""
        if self.is_running():
            self._start_again = True
            return False
        return self.start()

    def update_job(self, j: cv.Job):
        self.job_updated.emit(j)
        r = self.rows.get(j.id)
        if r is None:
            return
        st = self.table.item(r, Q_STATUS)
        running = j.status == "Running"
        st.setText(f"▶ {j.step_index + 1}/{len(j.steps)}" if running and j.steps else j.status)
        st.setToolTip(j.step_label if running else j.status)
        st.setForeground(QColor(STATUS_COLOR.get(j.status, "#888")))
        f = st.font()
        f.setBold(True)
        st.setFont(f)
        self.table.item(r, Q_PROGRESS).setData(Qt.ItemDataRole.UserRole, j.progress)
        note = (f"{j.step_name}: {j.phase}" if j.phase else j.step_label) if running else j.message
        self.table.item(r, Q_NOTE).setText(note)
        self.table.item(r, Q_NOTE).setToolTip(note)
        tint = QColor(47, 127, 209, 45) if running else QColor(0, 0, 0, 0)
        for c in range(self.table.columnCount()):
            it = self.table.item(r, c)
            if it is not None:
                it.setBackground(tint)
        if running and self.follow.isChecked() and self._followed != j.id:
            self._followed = j.id
            self.table.selectRow(r)
            self.table.scrollToItem(self.table.item(r, Q_TITLE))
        if self.table.currentRow() == r:
            self._show_log(r, keep=True)

    def _show_log(self, r: int, keep: bool = False):
        j = next((x for x in self.jobs if self.rows.get(x.id) == r), None)
        if not j:
            return
        text = "\n".join(j.log)
        if keep and self.log.toPlainText() == text:
            return
        self.log.setPlainText(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _rebuild(self):
        keep = list(self.jobs)
        self.jobs, self.rows = [], {}
        self.table.setRowCount(0)
        self.add_jobs(keep)

    def remove_selected(self):
        if self.worker and self.worker.isRunning():
            return
        rows = {i.row() for i in self.table.selectedIndexes()}
        self.jobs = [j for j in self.jobs if self.rows.get(j.id) not in rows]
        self._rebuild()

    def clear_finished(self):
        if self.worker and self.worker.isRunning():
            return
        self.jobs = [j for j in self.jobs if j.status in ("Queued", "Running")]
        self._rebuild()

    def retry_failed(self):
        if self.worker and self.worker.isRunning():
            return
        for j in self.jobs:
            if j.status in ("Failed", "Cancelled", "Skipped") and j.steps:
                j.status, j.progress, j.message, j.log = "Queued", 0.0, "", []
                self.update_job(j)
        self._summary()

    def _summary(self):
        counts: dict[str, int] = {}
        for j in self.jobs:
            counts[j.status] = counts.get(j.status, 0) + 1
        self.summary.setText("" if self.jobs else "Queue is empty — add files or a folder, or drop them here.")
        self.qbar.show_counts(counts, len(self.jobs))
        if not (self.worker and self.worker.isRunning()):
            done = sum(counts.get(s, 0) for s in ("Done", "Failed", "Skipped", "Cancelled"))
            self.qbar.bar.setValue(int(1000 * done / len(self.jobs)) if self.jobs else 0)
            self.qbar.bar.setFormat(f"{done} / {len(self.jobs)} jobs finished" if self.jobs else "idle")

    # -------------------------------------------------------------- run
    def start(self) -> bool:
        todo = [j for j in self.jobs if j.status == "Queued"]
        if not todo:
            QMessageBox.information(self, "Start", "Nothing queued.")
            return False
        if not ct.chdman_bin():
            QMessageBox.critical(self, "chdman missing", "chdman was not found. See the chdman (advanced) tab.")
            return False
        # free-space check per output folder
        need: dict[str, int] = {}
        for j in todo:
            if j.outputs:
                d = str(j.outputs[0].parent)
                need[d] = need.get(d, 0) + cv.estimate_output(j)
        short = [(d, n, cv.free_space(Path(d))) for d, n in need.items() if n > cv.free_space(Path(d))]
        if short:
            msg = "\n".join(f"{d}: needs up to {human(n)}, {human(f)} free" for d, n, f in short)
            if QMessageBox.warning(self, "Low disk space", f"Outputs may not fit:\n\n{msg}\n\nStart anyway?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != \
                    QMessageBox.StandardButton.Yes:
                return False
        if any(j.trash for j in todo):
            if QMessageBox.question(self, "Move sources to trash?",
                                    f"After each CHD is created and verified, its source files will be moved to "
                                    f"the trash ({sum(1 for j in todo if j.trash)} job(s)). Continue?") != \
                    QMessageBox.StandardButton.Yes:
                return False
        self.save_state()
        runner = cv.Runner(self.jobs, self.parallel.value(), lambda j: self.worker.job_changed.emit(j))
        self.runner = runner
        self.worker = RunnerWorker(lambda w: runner.run(), parent=self)
        self.worker.job_changed.connect(self.update_job)
        self.worker.done.connect(self._finished)
        self.worker.failed.connect(lambda e: (self._finished(None), QMessageBox.critical(self, "Task queue failed", e)))
        self.run_ids = [j.id for j in todo]
        for j in todo:
            j.measure()
        self.started = time.monotonic()
        self._followed = -1
        self._running(True)
        self.worker.start()
        return True

    def _tick(self):
        """Twice a second while running: overall bar, ETA, counts, live cards, window title."""
        by_id = {j.id: j for j in self.jobs}
        run = [by_id[i] for i in self.run_ids if i in by_id]
        total_w = sum(j.weight for j in run) or 1
        done_w = 0.0
        for j in run:
            if j.status in ("Done", "Failed", "Skipped", "Cancelled"):
                done_w += j.weight
            elif j.status == "Running":
                done_w += j.weight * j.progress
        p = done_w / total_w
        finished = sum(1 for j in run if j.status in ("Done", "Failed", "Skipped", "Cancelled"))
        el = time.monotonic() - self.started
        self.qbar.bar.setValue(int(p * 1000))
        self.qbar.bar.setFormat(f"{p * 100:.1f}%  ·  {finished} / {len(run)} jobs")
        bits = [f"elapsed {fmt_dur(el)}"]
        if p > 0.01:
            bits.append(f"ETA ~{fmt_dur(el * (1 - p) / p)}")
            bits.append(f"{human(int(total_w * p / max(el, 0.1)))}/s")
        self.qbar.timing.setText("  ·  ".join(bits))
        counts: dict[str, int] = {}
        for j in self.jobs:
            counts[j.status] = counts.get(j.status, 0) + 1
        self.qbar.show_counts(counts, len(self.jobs))
        running = [j for j in run if j.status == "Running"]
        self.now.sync(running)
        for j in running:
            self.update_job(j)
        cur = running[0] if running else None
        self.win.show_queue_progress(p, finished, len(run),
                                     f"{cur.title} — {cur.step_label}" if cur else "", bits)

    def stop(self):
        if self.runner:
            self.runner.stop()

    def _running(self, on: bool):
        self.btn_start.setEnabled(not on)
        self.btn_stop.setEnabled(on)
        self.win.set_busy(on)
        if on:
            self.tick.start()
            self._tick()
        else:
            self.tick.stop()

    def _finished(self, _):
        self._tick()
        self._running(False)
        self.now.sync([])
        self._summary()
        el = time.monotonic() - self.started
        by_id = {j.id: j for j in self.jobs}
        run = [by_id[i] for i in self.run_ids if i in by_id]
        counts: dict[str, int] = {}
        for j in run:
            counts[j.status] = counts.get(j.status, 0) + 1
        what = ", ".join(f"{v} {k.lower()}" for k, v in counts.items())
        self.qbar.bar.setValue(1000)
        self.qbar.bar.setFormat(f"finished · {what}")
        self.qbar.timing.setText(f"took {fmt_dur(el)}")
        self.win.show_queue_progress(None, 0, 0, "", [])
        self.win.statusBar().showMessage(f"Queue finished in {fmt_dur(el)} — {what}")
        QApplication.alert(self.win)
        self.queue_finished.emit()
        if getattr(self, "_start_again", False):   # jobs were added (from Save space) while this run was going
            self._start_again = False
            if any(j.status == "Queued" for j in self.jobs):
                QTimer.singleShot(0, self.start)


# ====================================================================== Patch tab

class AutoMatchDialog(QDialog):
    """Pick a folder of patches; each one is matched to a ROM (checksum / name the patch records / name) and all
    ticked rows are patched in one go."""

    COLS = ["", "Patch", "Original ROM", "Matched by", "Save as"]

    def __init__(self, tab: "PatchTab"):
        super().__init__(tab)
        self.tab, self.pt = tab, tab.pt
        self.setWindowTitle("Auto-match patches")
        self.resize(1250, 640)
        self.matches = []
        self.worker: Worker | None = None
        cfg = get_config()
        self.patch_dir = QLineEdit(cfg["window_state"].get("last_patch_dir", ""))
        self.search_dir = QLineEdit(cfg["window_state"].get("patch_search_dir") or cfg["library_root"])
        self.search_dir.setToolTip("Where your original ROMs are (searched 3 levels deep). Remembered scans from ROM "
                                   "Verifier make checksum matching instant.")
        find = QPushButton(QIcon.fromTheme("edit-find"), "Find matches")
        find.clicked.connect(self.find)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for c, wdt in ((0, 28), (1, 280), (2, 380), (3, 170)):
            self.table.setColumnWidth(c, wdt)
        self.status = QLabel("Pick the folder with your patches, then Find matches.")
        self.status.setWordWrap(True)
        self.go = QPushButton(QIcon.fromTheme("document-save"), "Patch ticked")
        self.go.clicked.connect(self.run)
        self.go.setEnabled(False)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        form = QFormLayout()
        form.addRow("Patches folder:", file_row(self.patch_dir, False, self, directory=True))
        form.addRow("Search for originals in:", file_row(self.search_dir, False, self, directory=True))
        top = QHBoxLayout()
        top.addLayout(form, 1)
        top.addWidget(find)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.bar)
        bottom.addWidget(self.go)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addWidget(QLabel("<small>Ticked automatically: checksum matches, and patches that check the original "
                             "themselves (BPS, UPS, xdelta, PPF2/3). Name-only matches for IPS/PPF1 can't be "
                             "checked — confirm them before ticking. Output goes next to each original, tagged as "
                             "chosen on the Patch tab. Originals are never changed.</small>"))
        lay.addLayout(bottom)

    def find(self):
        pdir, sdir = Path(self.patch_dir.text().strip()), Path(self.search_dir.text().strip())
        if not pdir.is_dir():
            QMessageBox.information(self, "Auto-match", "Choose the folder that holds your patches.")
            return
        cfg = get_config()
        cfg["window_state"]["last_patch_dir"], cfg["window_state"]["patch_search_dir"] = str(pdir), str(sdir)
        cfg.save()
        patches = sorted(p for p in pdir.rglob("*") if p.is_file() and p.suffix.lower() in self.pt.PATCH_EXTS)
        if not patches:
            QMessageBox.information(self, "Auto-match", "No patch files (IPS, BPS, UPS, PPF, xdelta) in that folder.")
            return
        self.status.setText(f"Matching {len(patches)} patch(es) against the ROMs in {sdir}…")
        pt = self.pt

        def job(_w):
            import copy as _copy
            c = _copy.copy(cfg)
            c.data = dict(cfg.data)
            c.data["library_root"] = str(sdir)
            return pt.match_patches(patches, pt.library_candidates(c))

        self.worker = Worker(job, parent=self)
        self.worker.done.connect(self._show)
        self.worker.failed.connect(lambda e: self.status.setText(f"Failed: {e}"))
        self.worker.start()

    def _show(self, matches):
        self.matches = matches
        self.table.setRowCount(len(matches))
        checks = ("BPS", "UPS", "xdelta", "PPF2", "PPF3")
        for r, m in enumerate(matches):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            auto = bool(m.rom) and (m.sure or (m.info and m.info.format in checks))
            chk.setCheckState(Qt.CheckState.Checked if auto else Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, chk)
            pi = QTableWidgetItem(m.patch.name + (f"  ({m.info.format})" if m.info else ""))
            pi.setToolTip(str(m.patch))
            self.table.setItem(r, 1, pi)
            combo = QComboBox()
            if m.rom:
                for p in [m.rom] + m.alternatives:
                    combo.addItem(p.name, str(p))
            else:
                combo.addItem("— no match —", "")
            combo.addItem("Choose file…", "__pick__")
            combo.activated.connect(lambda _i, row=r, cb=combo: self._pick(row, cb))
            self.table.setCellWidget(r, 2, combo)
            how = QTableWidgetItem(m.how or m.error)
            how.setForeground(QColor("#2e9d53" if m.sure else "#d49a1b" if m.rom else "#d1452f"))
            how.setToolTip(m.error or m.how)
            self.table.setItem(r, 3, how)
            self.table.setItem(r, 4, QTableWidgetItem(self._out_name(m.rom, m.patch) if m.rom else ""))
        sure = sum(1 for m in matches if m.sure)
        found = sum(1 for m in matches if m.rom)
        self.status.setText(f"{found} of {len(matches)} patch(es) matched ({sure} by checksum). Check the orange "
                            f"ones, then Patch ticked.")
        self.go.setEnabled(found > 0)

    def _out_name(self, rom: Path, patch: Path) -> str:
        return self.pt.default_output_name(rom, [patch]) + self.pt.output_suffix(rom)

    def _pick(self, row: int, combo: QComboBox):
        if combo.currentData() != "__pick__":
            self.table.item(row, 4).setText(self._out_name(Path(combo.currentData()), self.matches[row].patch)
                                            if combo.currentData() else "")
            return
        f, _ = QFileDialog.getOpenFileName(self, "Original ROM", self.search_dir.text(), PatchTab.ROM_FILTER)
        if f:
            combo.insertItem(0, Path(f).name, f)
            combo.setCurrentIndex(0)
            self.table.item(row, 4).setText(self._out_name(Path(f), self.matches[row].patch))
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        else:
            combo.setCurrentIndex(0)

    def run(self):
        todo = []
        for r, m in enumerate(self.matches):
            rom = self.table.cellWidget(r, 2).currentData()
            name = self.table.item(r, 4).text().strip()
            if self.table.item(r, 0).checkState() == Qt.CheckState.Checked and rom and rom != "__pick__" and name:
                todo.append((r, m.patch, Path(rom), Path(rom).parent / name))
        if not todo:
            return
        cfg = get_config()
        tmp_root = Path(cfg["tmp_dir"]) / "patch"
        tag, header, pt = self.tab.tag.currentData(), self.tab.header.currentData(), self.pt
        self.go.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, len(todo))
        self.bar.setValue(0)

        def job(w):
            results = []
            for i, (r, patch, rom, out) in enumerate(todo):
                if w.cancel.is_set():
                    break
                w.progress.emit(i, len(todo), f"{rom.name} + {patch.name}", 0)
                try:
                    if out.exists():
                        raise pt.PatchError(f"{out.name} already exists")
                    pt.patch_rom(rom, [patch], out, tmp_root, header, tag)
                    results.append((r, True, f"✔ saved {out.name}"))
                except Exception as exc:  # noqa: BLE001
                    results.append((r, False, f"✘ {str(exc).split(': ', 1)[-1]}"))
                w.message.emit(f"{r}	{results[-1][1]}	{results[-1][2]}")
            return results

        self.worker = Worker(job, parent=self)
        self.worker.progress.connect(lambda d, t, msg, f: (self.bar.setValue(d), self.status.setText(msg)))
        self.worker.message.connect(self._row_result)
        self.worker.done.connect(self._finished)
        self.worker.start()

    def _row_result(self, s: str):
        r, ok, msg = s.split("\t", 2)
        it = self.table.item(int(r), 3)
        it.setText(msg)
        it.setToolTip(msg)
        it.setForeground(QColor("#2e9d53" if ok == "True" else "#d1452f"))
        self.table.item(int(r), 0).setCheckState(Qt.CheckState.Unchecked)

    def _finished(self, results):
        ok = sum(1 for _r, good, _m in results if good)
        self.bar.setVisible(False)
        self.go.setEnabled(True)
        self.status.setText(f"Done: {ok} of {len(results)} patched. Originals unchanged.")
        QApplication.alert(self)

    def closeEvent(self, ev):
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set()
            self.worker.wait(30000)
        super().closeEvent(ev)


class PatchTab(QWidget):
    """Apply IPS / BPS / UPS / PPF / xdelta patches to a ROM, archive, .cue set or CHD. Writes a new file."""

    ROM_FILTER = ("ROMs, archives and disc images (*.nes *.fds *.sfc *.smc *.gb *.gbc *.gba *.nds *.n64 *.z64 *.v64 "
                  "*.md *.gen *.bin *.sms *.gg *.pce *.a26 *.a78 *.lnx *.ws *.wsc *.ngp *.ngc *.iso *.cue *.chd *.zip "
                  "*.7z *.rar);;All files (*)")

    def __init__(self, win: "ConverterWindow"):
        super().__init__()
        from .. import patching as pt
        from .. import tags as tg
        self.pt, self.win = pt, win
        self.worker: Worker | None = None
        self.base_info = None
        self.setAcceptDrops(True)

        self.rom = QLineEdit()
        self.rom.setPlaceholderText("ROM, zip/7z with one ROM, .cue, .iso or .chd — or drop it here")
        self.rom.editingFinished.connect(self._changed)
        self.patches = QListWidget()
        self.patches.setToolTip("Applied top to bottom")
        self.patches.setMaximumHeight(110)
        add_p = QPushButton(QIcon.fromTheme("list-add"), "Add patch…")
        add_p.clicked.connect(self._add_patches)
        rm_p = QPushButton(QIcon.fromTheme("list-remove"), "Remove")
        rm_p.clicked.connect(lambda: (self.patches.takeItem(self.patches.currentRow()), self._changed()))
        up_p = QPushButton(QIcon.fromTheme("go-up"), "")
        up_p.setToolTip("Apply earlier")
        up_p.clicked.connect(lambda: self._move(-1))
        down_p = QPushButton(QIcon.fromTheme("go-down"), "")
        down_p.setToolTip("Apply later")
        down_p.clicked.connect(lambda: self._move(1))
        for b, t in ((up_p, "↑"), (down_p, "↓")):
            if b.icon().isNull():
                b.setText(t)

        self.out_name = QLineEdit()
        self.out_name.setPlaceholderText("name of the patched file")
        self._name_auto = True   # follow the picked ROM/patch until the user types a name of their own
        self.out_name.textEdited.connect(lambda t: setattr(self, "_name_auto", not t.strip()))
        self.out_where = QComboBox()
        self.out_where.addItems(["Next to the original", "Into this folder:"])
        self.out_dir = QLineEdit()
        self.out_dir_row = file_row(self.out_dir, False, self, directory=True)
        self.out_dir_row.setEnabled(False)
        self.out_where.currentIndexChanged.connect(lambda i: self.out_dir_row.setEnabled(i == 1))
        self.header = QComboBox()
        self.header.addItem("Automatic (BPS/UPS pick the right one by checksum)", "auto")
        self.header.addItem("Keep the ROM as it is", "keep")
        self.header.addItem("Remove the copier header first", "strip")
        self.header.setToolTip("SNES/NES/Lynx/7800 ROMs can carry a copier header. Most modern patches expect "
                               "No-Intro ROMs without one.")
        self.tag = QComboBox()
        self.tag.addItem("Don't tag", "")
        for name in tg.TAGS:
            self.tag.addItem(name, name)
        self.tag.setCurrentIndex(self.tag.findData("Translation"))
        self.tag.setToolTip("Tags the new file in ROM Verifier, so it shows as Modified instead of a bad dump")

        self.details = QTextBrowser()
        self.details.setPlaceholderText("Pick a ROM and a patch to see whether they belong together.")
        self.apply_btn = QPushButton(QIcon.fromTheme("document-save"), "Create patched copy")
        self.apply_btn.clicked.connect(self.apply)
        self.auto_btn = QPushButton(QIcon.fromTheme("edit-find"), "Auto-match a folder of patches…")
        self.auto_btn.setToolTip("Finds the right ROM for every patch in a folder and patches them in one go")
        self.auto_btn.clicked.connect(lambda: AutoMatchDialog(self).exec())
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono())
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("Progress appears here. The original is never changed.")

        form = QFormLayout()
        form.addRow("<b>1. Original ROM</b>", file_row(self.rom, False, self, self.ROM_FILTER))
        prow = QHBoxLayout()
        prow.addWidget(self.patches, 1)
        pbtn = QVBoxLayout()
        for b in (add_p, rm_p, up_p, down_p):
            pbtn.addWidget(b)
        pbtn.addStretch(1)
        prow.addLayout(pbtn)
        form.addRow("<b>2. Patch(es)</b>", prow)
        orow = QHBoxLayout()
        orow.addWidget(self.out_name, 2)
        orow.addWidget(self.out_where)
        orow.addWidget(self.out_dir_row, 2)
        form.addRow("<b>3. Save as</b>", orow)
        form.addRow("Copier header:", self.header)
        form.addRow("Tag the result as:", self.tag)
        brow = QHBoxLayout()
        brow.addWidget(self.apply_btn)
        brow.addWidget(self.auto_btn)
        brow.addWidget(self.bar, 1)
        lay = QVBoxLayout(self)
        intro = QLabel("Apply <b>IPS, BPS, UPS, PPF</b> or <b>xdelta</b> patches (translations, hacks, fixes). "
                       "Works on plain ROMs, single-game zip/7z/rar, .cue sets, .iso and <b>CHDs</b> (unpacked, "
                       "patched and rebuilt as a new CHD). The original is never modified.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lay.addLayout(form)
        lay.addLayout(brow)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.details)
        split.addWidget(self.log)
        split.setSizes([260, 160])
        lay.addWidget(split, 1)

    # ------------------------------------------------------------------ inputs
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.suffix.lower() in self.pt.PATCH_EXTS:
                self._append_patch(p)
            elif p.is_file():
                self.rom.setText(str(p))
        self._changed()

    def _add_patches(self):
        start = get_config()["window_state"].get("last_patch_dir") or str(Path.home())
        exts = " ".join(f"*{e}" for e in self.pt.PATCH_EXTS)
        files, _ = QFileDialog.getOpenFileNames(self, "Choose patch file(s)", start,
                                                f"Patches ({exts});;All files (*)")
        for f in files:
            self._append_patch(Path(f))
        if files:
            cfg = get_config()
            cfg["window_state"]["last_patch_dir"] = str(Path(files[0]).parent)
            cfg.save()
        self._changed()

    def _append_patch(self, p: Path):
        it = QListWidgetItem(p.name)
        it.setData(Qt.ItemDataRole.UserRole, str(p))
        it.setToolTip(str(p))
        self.patches.addItem(it)

    def _move(self, d: int):
        r = self.patches.currentRow()
        if r < 0 or not 0 <= r + d < self.patches.count():
            return
        it = self.patches.takeItem(r)
        self.patches.insertItem(r + d, it)
        self.patches.setCurrentRow(r + d)
        self._changed()

    def patch_paths(self) -> list[Path]:
        return [Path(self.patches.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.patches.count())]

    def _changed(self):
        src = Path(self.rom.text().strip()) if self.rom.text().strip() else None
        pats = self.patch_paths()
        if src and src.is_file() and pats and self._name_auto:
            self.out_name.setText(self.pt.default_output_name(src, pats) + self.pt.output_suffix(src))
        self._describe(src, pats)

    def _describe(self, src, pats):
        import html
        e = html.escape
        rows = []
        for p in pats:
            try:
                i = self.pt.inspect(p)
                line = f"<b>{e(p.name)}</b> — {i.format}"
                if i.description:
                    line += f" · “{e(i.description[:80])}”"
                if i.source_crc:
                    line += (f"<br>&nbsp;&nbsp;expects an original of {i.source_size:,} bytes, CRC32 {i.source_crc}; "
                             f"result CRC32 {i.target_crc} (both checked automatically)")
                elif i.format.startswith("PPF"):
                    line += "<br>&nbsp;&nbsp;checks the disc image's identity block before patching" \
                        if i.format != "PPF1" else ""
                else:
                    line += ("<br>&nbsp;&nbsp;this format can't confirm the original — make sure it's the exact "
                             "version the patch's readme names (see the identification below)")
                for n in i.notes:
                    line += f"<br>&nbsp;&nbsp;{e(n)}"
                rows.append(line)
            except Exception as exc:  # noqa: BLE001
                rows.append(f"<b>{e(p.name)}</b> — <span style='color:#d1452f'>{e(str(exc))}</span>")
        if src and src.is_file():
            rows.append(f"<b>Original:</b> {e(src.name)} — press <i>Create patched copy</i>; it's identified against "
                        f"your DATs first, and the details appear in the log.")
        self.details.setHtml("<br><br>".join(rows) or "")

    # ------------------------------------------------------------------ apply
    def apply(self):
        if self.worker and self.worker.isRunning():
            return
        src = Path(self.rom.text().strip())
        pats = self.patch_paths()
        if not src.is_file() or not pats:
            QMessageBox.information(self, "Patch", "Pick the original ROM and at least one patch.")
            return
        name = self.out_name.text().strip() or (self.pt.default_output_name(src, pats) + self.pt.output_suffix(src))
        if not Path(name).suffix:
            name += self.pt.output_suffix(src)
        folder = Path(self.out_dir.text().strip()) if self.out_where.currentIndex() == 1 and self.out_dir.text().strip() \
            else src.parent
        out = folder / name
        if out.exists():
            QMessageBox.warning(self, "Patch", f"{out.name} already exists there — choose another name.")
            return
        from .. import compat, chd
        from ..config import get_config as _cfg
        cfg = _cfg()
        size = src.stat().st_size
        if src.suffix.lower() == ".chd":
            try:
                ci = chd.info(src)
                size = ci.frames * 2352 if ci.kind == "cd" else ci.logical_size
            except Exception:  # noqa: BLE001
                pass
        ram = (cfg["temp_mode"] == "ram" and compat.ram_mode_supported() and compat.mem_available() > 3 * size + (1 << 30))
        tmp_root = Path("/dev/shm/romtools-patch") if ram else Path(cfg["tmp_dir"]) / "patch"
        header, tag = self.header.currentData(), self.tag.currentData()
        self.log.clear()
        self.log.appendPlainText(f"Patching {src.name} → {out}" + (f"  (temp: {'RAM' if ram else tmp_root})"
                                                                  if src.suffix.lower() == ".chd" else ""))
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.apply_btn.setEnabled(False)
        self.win.set_busy(True)
        pt = self.pt

        def job(w: Worker):
            info, res = pt.patch_rom(src, pats, out, tmp_root, header, tag,
                                     progress=lambda s, f=0.0: w.progress.emit(0, 0, s, f))
            return info, res

        self.worker = Worker(job, parent=self)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(lambda r: self._done(r, out, tag))
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _progress(self, _d, _t, msg, frac):
        if msg and (self.log.toPlainText().splitlines() or [""])[-1] != msg:
            self.log.appendPlainText(msg)
        if frac > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))

    def _finish_ui(self):
        self.bar.setVisible(False)
        self.apply_btn.setEnabled(True)
        self.win.set_busy(False)

    def _done(self, result, out: Path, tag: str):
        self._finish_ui()
        info, res = result
        ident = (f"identified as <b>{info.dat_game}</b> ({info.dat_name})" if info.dat_game else
                 "<span style='color:#d49a1b'>not found in your DATs</span> — if the result doesn't work, the "
                 "patch was probably made for a different version")
        ver = {True: "<span style='color:#2e9d53'>result matches the patch's checksum ✔</span>",
               None: "result CRC32 " + res.target_crc}.get(res.verified, "")
        notes = "".join(f"<br>• {n}" for n in res.notes)
        orig = info.path.name if info.kind in ("chd", "file") else f"{info.target_name} (from {info.path.name})"
        self.details.setHtml(f"<h3 style='margin:0'>Done: {out.name}</h3>Original {orig}: {ident}"
                             f"<br>{ver}{notes}<br>Saved in {out.parent}" + (f"<br>Tagged as <b>{tag}</b> for ROM "
                                                                          f"Verifier" if tag else ""))
        self.log.appendPlainText(f"Done: {out}")
        self._name_auto = True   # next ROM/patch gets a fresh suggestion
        QApplication.alert(self.win)

    def _failed(self, msg: str):
        self._finish_ui()
        msg = msg.split(": ", 1)[-1] if msg.startswith("PatchError") else msg
        self.log.appendPlainText(f"Failed: {msg}")
        self.details.setHtml(f"<h3 style='margin:0;color:#d1452f'>Not patched</h3>{msg}<br>The original was not "
                             f"changed and no output was left behind.")


# ====================================================================== Save space tab

class SpaceTab(QWidget):
    COLS = ["Suggestion / file", "Status", "Folder", "Files", "Now", "After (est.)", "Saves", "Readable by", "Note"]
    C_NAME, C_STATUS, C_FOLDER, C_FILES, C_NOW, C_AFTER, C_SAVES, C_COMPAT, C_NOTE = range(9)
    PATH_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, win: "ConverterWindow"):
        super().__init__()
        self.win = win
        self.worker: Worker | None = None
        self.suggestions = []
        self.row_of: dict[str, QTreeWidgetItem] = {}     # file path -> its row
        self.job_of: dict[str, cv.Job] = {}              # file path -> latest job started from here
        cfg = get_config()
        self.root = QLineEdit(cfg["window_state"].get("advisor_dir") or cfg["library_root"])
        self.go = QPushButton(QIcon.fromTheme("edit-find"), "Analyse")
        self.go.clicked.connect(self.analyse)
        self.table = QTreeWidget()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHeaderLabels(self.COLS)
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(False)
        hh = self.table.header()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for c, wdt in ((0, 420), (1, 190), (2, 80), (3, 45), (4, 80), (5, 90), (6, 80), (7, 260)):
            self.table.setColumnWidth(c, wdt)
        self.table.itemChanged.connect(lambda *_: self._count_ticked())
        expand = QPushButton("Expand all")
        expand.clicked.connect(self.table.expandAll)
        collapse = QPushButton("Collapse all")
        collapse.clicked.connect(self.table.collapseAll)
        self.total = QLabel("Analyse a folder to see where space can be saved without losing anything.")
        self.total.setWordWrap(True)
        self.trash = QCheckBox("Move the originals to the trash once the new file is verified (this is what frees "
                               "the space)")
        self.trash.setToolTip("Only after the new file has been checked (chdman verify / same disc SHA-1 / nsz "
                              "verify). Off: the new files are written next to the originals and both are kept.\n"
                              "Trimming in place always replaces the file, after checking it can be untrimmed back.")
        self.trash.setChecked(bool(cfg["window_state"].get("advisor_trash", False)))
        self.trash.toggled.connect(self._save_trash)
        self.run = QPushButton(QIcon.fromTheme("media-playback-start"), "Run ticked now")
        self.run.setToolTip("Convert the ticked files right here; each row shows its progress. Uses the Tasks "
                            "options (codecs, jobs at once, output folder).")
        self.run.clicked.connect(self.run_now)
        self.stop_btn = QPushButton(QIcon.fromTheme("media-playback-stop"), "Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self.win.batch.stop())
        self.queue = QPushButton(QIcon.fromTheme("list-add"), "Add ticked to Tasks")
        self.queue.setToolTip("Only add them to the Tasks queue (to check options or run later)")
        for b in (self.run, self.queue):
            b.setEnabled(False)
        self.queue.clicked.connect(self.to_batch)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Folder:</b>"))
        top.addWidget(file_row(self.root, False, self, directory=True), 1)
        top.addWidget(self.go)
        opts = QHBoxLayout()
        opts.addWidget(self.trash, 1)
        opts.addWidget(expand)
        opts.addWidget(collapse)
        bottom = QHBoxLayout()
        bottom.addWidget(self.total, 1)
        bottom.addWidget(self.queue)
        bottom.addWidget(self.stop_btn)
        bottom.addWidget(self.run)
        lay = QVBoxLayout(self)
        intro = QLabel("Every suggestion is lossless and verified before anything is replaced. Estimates come from "
                       "compressing samples of your actual files. Orange notes matter: e.g. Xenia can't play CHDs, "
                       "and NSZ needs your keys. Tick what you want and press <b>Run ticked now</b>.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(opts)
        lay.addLayout(bottom)
        win.batch.job_updated.connect(self._job_updated)
        win.batch.queue_finished.connect(self._queue_finished)

    @staticmethod
    def _size(n: int) -> str:
        if n >= 2**30:
            return f"{n / 2**30:.1f} GB"
        return f"{n / 2**20:.0f} MB" if n >= 2**20 else f"{n / 2**10:.0f} KB"

    def _save_trash(self, on: bool):
        cfg = get_config()
        cfg["window_state"]["advisor_trash"] = on
        cfg.save()

    def analyse(self):
        if self.worker and self.worker.isRunning():
            return
        root = Path(self.root.text().strip()).expanduser()
        if not root.is_dir():
            QMessageBox.information(self, "Save space", "Choose a folder to analyse.")
            return
        cfg = get_config()
        cfg["window_state"]["advisor_dir"] = str(root)
        cfg.save()
        self.go.setEnabled(False)
        self.total.setText("Analysing (reads ~12 MB from each large file)…")
        from .. import advisor

        def job(w):
            return advisor.analyse(cfg, root, lambda s: w.message.emit(s), w.cancel)

        self.worker = Worker(job, parent=self)
        self.worker.message.connect(self.total.setText)
        self.worker.done.connect(self._show)
        self.worker.failed.connect(lambda e: (self.go.setEnabled(True), self.total.setText(f"Failed: {e}")))
        self.worker.start()

    def _show(self, suggestions):
        self.go.setEnabled(True)
        self.suggestions = suggestions
        self.row_of = {}
        self.table.blockSignals(True)
        self.table.clear()
        warn = QColor("#d49a1b")
        root = Path(self.root.text().strip()).expanduser()
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for s in suggestions:
            space = s.kind == "space"
            tick = space and s.saving > 50 * 2**20 and not s.warning
            parent = QTreeWidgetItem([s.title + ("  (speed)" if not space else ""), "", s.folder, str(len(s.files)),
                                      self._size(s.size_now), self._size(s.size_after),
                                      self._size(s.saving) if space else "—", s.compat, s.warning])
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            f0 = parent.font(0)
            f0.setBold(True)
            parent.setFont(0, f0)
            if s.warning:
                parent.setForeground(self.C_NOTE, warn)
                parent.setToolTip(self.C_NOTE, s.warning)
            per = s.per_file or {f: (0, 0) for f in s.files}
            # biggest saving first, so the files worth converting are at the top
            for f, (now, after) in sorted(per.items(), key=lambda kv: kv[1][1] - kv[1][0]):
                try:
                    shown = str(f.relative_to(root / s.folder)) if (root / s.folder) in f.parents else \
                        str(f.relative_to(root))
                except ValueError:
                    shown = f.name
                child = QTreeWidgetItem([""] * len(self.COLS))
                child.setText(self.C_NAME, shown)
                child.setText(self.C_NOW, self._size(now))
                child.setText(self.C_AFTER, self._size(after))
                child.setText(self.C_SAVES, self._size(max(0, now - after)) if space else "—")
                child.setToolTip(self.C_NAME, str(f))
                child.setData(0, self.PATH_ROLE, str(f))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if tick else Qt.CheckState.Unchecked)
                for c in (self.C_NOW, self.C_AFTER, self.C_SAVES):
                    child.setTextAlignment(c, right)
                parent.addChild(child)
                self.row_of[str(f)] = child
                if str(f) in self.job_of:            # re-analysed while a job from before is known
                    self._paint(child, self.job_of[str(f)])
            for c in (self.C_FILES, self.C_NOW, self.C_AFTER, self.C_SAVES):
                parent.setTextAlignment(c, right)
            self.table.addTopLevelItem(parent)
            parent.setExpanded(True)
        self.table.blockSignals(False)
        self._count_ticked()

    def _ticked(self) -> list[tuple[object, list[Path]]]:
        out = []
        for i, s in enumerate(self.suggestions):
            parent = self.table.topLevelItem(i)
            files = [Path(parent.child(k).data(0, self.PATH_ROLE)) for k in range(parent.childCount())
                     if parent.child(k).checkState(0) == Qt.CheckState.Checked]
            if files:
                out.append((s, files))
        return out

    def _count_ticked(self):
        running = self.win.batch.is_running()
        self.stop_btn.setEnabled(running)
        if not self.suggestions:
            self.total.setText("Nothing to suggest — already compact.")
            self.queue.setEnabled(False)
            self.run.setEnabled(False)
            return
        total = sum(s.saving for s in self.suggestions if s.kind == "space")
        ticked = self._ticked()
        n = sum(len(f) for _s, f in ticked)
        saves = sum(max(0, s.per_file.get(f, (0, 0))[0] - s.per_file.get(f, (0, 0))[1])
                    for s, files in ticked if s.kind == "space" for f in files)
        nfiles = sum(len(s.files) for s in self.suggestions)
        self.total.setText(f"{len(self.suggestions)} suggestion(s) for {nfiles} file(s), up to {self._size(total)} "
                           f"could be saved. Ticked: {n} file(s), about {self._size(saves)}.")
        self.queue.setEnabled(n > 0)
        self.run.setEnabled(n > 0)

    # -- queueing / running ------------------------------------------------
    def _enqueue(self) -> list[cv.Job]:
        b = self.win.batch
        added: list[cv.Job] = []
        busy = {p for p, j in self.job_of.items() if j.status in ("Queued", "Running")}
        for s, files in self._ticked():
            files = [f for f in files if str(f) not in busy]
            if not files:
                continue
            if s.preset in (cv.TO_NSZ, cv.FROM_NSZ) and not Path(b.keys.text().strip()).is_file() \
                    and not b.ask_for_keys("prod"):
                continue
            over = {"cd_codec": "zstd", "dvd_codec": "zstd"} if s.kind == "speed" else {}
            if self.trash.isChecked():
                over.update(trash_sources=True, verify_after=True)
            before = len(b.jobs)
            b.enqueue_preset(s.preset, files, **over)
            added += b.jobs[before:]
        for j in added:
            key = str(j.source)
            self.job_of[key] = j
            if key in self.row_of:
                self._paint(self.row_of[key], j)
        return added

    def run_now(self):
        jobs = self._enqueue()
        if not jobs:
            QMessageBox.information(self, "Run", "The ticked files are already queued or running.")
            return
        if self.win.batch.start_when_free() is False and self.win.batch.is_running():
            self.win.statusBar().showMessage(f"{len(jobs)} file(s) will start as soon as the current queue "
                                             f"finishes.", 10000)
        self._count_ticked()

    def to_batch(self):
        jobs = self._enqueue()
        if jobs:
            self.win.tabs.setCurrentWidget(self.win.batch)
            self.win.statusBar().showMessage(f"{len(jobs)} job(s) added to Tasks - check the options, then Start "
                                             f"queue.", 10000)

    # -- live status per file ------------------------------------------------
    def _paint(self, row: QTreeWidgetItem, j: cv.Job):
        st = j.status
        if st == "Running":
            text = f"▶ {j.progress * 100:.0f}% · {j.step_name or 'working'}"
        elif st == "Done":
            text = "✔ Done"
        elif st == "Failed":
            text = "✖ Failed"
        else:
            text = st
        row.setText(self.C_STATUS, text)
        row.setToolTip(self.C_STATUS, (j.message or j.step_label or st))
        row.setForeground(self.C_STATUS, QColor(STATUS_COLOR.get(st, "#888")))
        if st in ("Failed", "Skipped") and j.message:
            row.setText(self.C_NOTE, j.message)
            row.setToolTip(self.C_NOTE, j.message)
        if st == "Done":                      # don't redo it by accident
            self.table.blockSignals(True)
            row.setCheckState(0, Qt.CheckState.Unchecked)
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.table.blockSignals(False)
            if j.message:
                row.setText(self.C_NOTE, j.message)
                row.setToolTip(self.C_NOTE, j.message)

    def _job_updated(self, j: cv.Job):
        key = str(j.source)
        if self.job_of.get(key) is j and key in self.row_of:
            self._paint(self.row_of[key], j)
        self.stop_btn.setEnabled(self.win.batch.is_running())

    def _queue_finished(self):
        mine = [j for j in self.job_of.values() if j.finished or j.status not in ("Queued", "Running")]
        done = sum(1 for j in mine if j.status == "Done")
        failed = sum(1 for j in mine if j.status == "Failed")
        self._count_ticked()
        self.stop_btn.setEnabled(False)
        if mine:
            self.win.statusBar().showMessage(f"Save space: {done} done" + (f", {failed} failed (hover the status "
                                             f"for why)" if failed else "") + ". Analyse again to see what's left.",
                                             15000)


# ====================================================================== Commands tab

class CommandsTab(QWidget):
    def __init__(self, win: "ConverterWindow"):
        super().__init__()
        self.win = win
        self.widgets: dict[str, QWidget] = {}
        self.proc: ct.Process | None = None
        self.worker: Worker | None = None

        self.list = QListWidget()
        self.list.setMaximumWidth(260)
        self.list.currentRowChanged.connect(self._select)
        self.desc = QLabel()
        self.desc.setWordWrap(True)
        self.form_box = QGroupBox("Options")
        self.form = QFormLayout(self.form_box)
        self.preview = QLineEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono())
        copy = QPushButton(QIcon.fromTheme("edit-copy"), "Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.preview.text()))
        self.run_btn = QPushButton(QIcon.fromTheme("media-playback-start"), "Run now")
        self.run_btn.clicked.connect(self.run_now)
        self.stop_btn = QPushButton(QIcon.fromTheme("media-playback-stop"), "Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self.proc and self.proc.cancel())
        queue_btn = QPushButton(QIcon.fromTheme("list-add"), "Add to task queue")
        queue_btn.clicked.connect(self.to_queue)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFormat("this command: %p%")
        self.bar.setToolTip("Progress of the command run with Run now (separate from the task queue)")
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(mono())
        self.out.setMaximumBlockCount(20000)

        prow = QHBoxLayout()
        prow.addWidget(QLabel("Command:"))
        prow.addWidget(self.preview, 1)
        prow.addWidget(copy)
        brow = QHBoxLayout()
        brow.addWidget(self.run_btn)
        brow.addWidget(self.stop_btn)
        brow.addWidget(queue_btn)
        brow.addWidget(self.bar, 1)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.desc)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_box)
        rl.addWidget(scroll, 2)
        rl.addLayout(prow)
        rl.addLayout(brow)
        rl.addWidget(self.out, 3)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(right)
        split.setSizes([240, 1100])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        self.reload()

    def reload(self):
        self.cmds = ct.commands()
        self.list.clear()
        icons = {"create": "archive-insert", "extract": "archive-extract", "meta": "tag", "inspect": "document-preview"}
        for c in self.cmds:
            it = QListWidgetItem(QIcon.fromTheme(icons.get(c.group, "system-run")), c.name)
            it.setToolTip(c.desc)
            self.list.addItem(it)
        if self.cmds:
            self.list.setCurrentRow(0)
        else:
            self.desc.setText("<b>chdman not found.</b> Install mame-tools or pick a chdman in the chdman tab.")

    def _select(self, row: int):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.widgets = {}
        if row < 0 or row >= len(self.cmds):
            return
        c = self.cmds[row]
        self.desc.setText(f"<h3 style='margin:0'>chdman {c.name}</h3>{c.desc.capitalize()}."
                          + (" Fields marked * are required." if any(o.required for o in c.options) else ""))
        for o in c.options:
            label = QLabel(f"{'<b>' if o.required else ''}--{o.long}{' *</b>' if o.required else ''}")
            label.setToolTip(o.desc)
            if o.kind == "flag":
                w = QCheckBox(o.desc)
                w.toggled.connect(self._update_preview)
                self.form.addRow(label, w)
            else:
                e = QLineEdit()
                e.setPlaceholderText(f"<{o.arg}>  {o.desc}")
                e.setToolTip(o.desc)
                e.textChanged.connect(self._update_preview)
                w = e
                if o.kind == "compression":
                    combo = QComboBox()
                    combo.setEditable(True)
                    combo.addItems(["", "cdlz,cdzl,cdfl", "cdzs,cdzl,cdfl", "lzma,zlib,huff,flac",
                                    "zstd,zlib,huff,flac", "lzma", "none"])
                    combo.lineEdit().setPlaceholderText("default — or pick / type codecs (up to 4)")
                    combo.currentTextChanged.connect(self._update_preview)
                    w = combo
                    self.form.addRow(label, combo)
                elif o.kind in ("infile", "outfile"):
                    self.form.addRow(label, file_row(e, o.kind == "outfile", self))
                else:
                    self.form.addRow(label, e)
            self.widgets[o.long] = w
        self._update_preview()

    def values(self) -> dict:
        vals = {}
        for name, w in self.widgets.items():
            if isinstance(w, QCheckBox):
                vals[name] = w.isChecked()
            elif isinstance(w, QComboBox):
                vals[name] = w.currentText().strip()
            else:
                vals[name] = w.text().strip()
        return vals

    def argv(self) -> list[str]:
        c = self.cmds[self.list.currentRow()]
        return ct.build_argv(c.name, self.values())

    def _update_preview(self, *_):
        if self.list.currentRow() >= 0 and self.cmds:
            self.preview.setText(" ".join(ct._quote(a) for a in self.argv()))

    def _missing(self) -> list[str]:
        c = self.cmds[self.list.currentRow()]
        v = self.values()
        return [o.long for o in c.options if o.required and not v.get(o.long)]

    def run_now(self):
        if not self.cmds:
            return
        miss = self._missing()
        if miss:
            QMessageBox.warning(self, "Missing options", "Required: " + ", ".join("--" + m for m in miss))
            return
        argv = self.argv()
        self.out.clear()
        self.bar.setValue(0)
        self.proc = ct.Process(argv, lambda s: self.worker.message.emit(s),
                               lambda f: self.worker.progress.emit(0, 0, "", f))
        self.worker = Worker(lambda w: self.proc.run(), parent=self)
        self.worker.message.connect(self.out.appendPlainText)
        self.worker.progress.connect(lambda *_a: self.bar.setValue(int(_a[3] * 1000)))
        self.worker.done.connect(self._done)
        self.worker.failed.connect(lambda e: (self.out.appendPlainText(e), self._done(-1)))
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.win.set_busy(True)
        self.worker.start()

    def _done(self, rc):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.win.set_busy(False)
        self.out.appendPlainText(f"\n[exit code {rc}]" if rc else "\n[finished OK]")
        if rc == 0:
            self.bar.setValue(1000)

    def to_queue(self):
        if not self.cmds:
            return
        miss = self._missing()
        if miss:
            QMessageBox.warning(self, "Missing options", "Required: " + ", ".join("--" + m for m in miss))
            return
        self.win.batch.add_jobs([cv.custom_job(self.argv())])
        self.win.tabs.setCurrentWidget(self.win.batch)


# ====================================================================== chdman / updater tab

class ChdmanTab(QWidget):
    def __init__(self, win: "ConverterWindow"):
        super().__init__()
        self.win = win
        self.worker: Worker | None = None
        self.proc: ct.Process | None = None
        self.release: up.Release | None = None

        self.using = QLabel()
        self.using.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.choice = QComboBox()
        self.choice.activated.connect(self._choose)
        self.latest = QLabel("—")
        self.pkg = QLabel("—")
        self.verdict = QLabel()
        self.verdict.setWordWrap(True)

        check = QPushButton(QIcon.fromTheme("view-refresh"), "Check for updates")
        check.clicked.connect(self.check)
        self.pac_btn = QPushButton(QIcon.fromTheme("system-software-update"), "Update system package…")
        self.pac_btn.setToolTip("Opens a terminal running: sudo pacman -Syu mame-tools")
        self.pac_btn.clicked.connect(self.pacman_update)
        self.build_btn = QPushButton(QIcon.fromTheme("run-build"), "Build latest from official source…")
        self.build_btn.setToolTip("Downloads MAME's official source for the latest release from GitHub and builds "
                                  "only the tools (no emulator). Installed for your user only.")
        self.build_btn.clicked.connect(self.build)
        self.cancel_btn = QPushButton(QIcon.fromTheme("process-stop"), "Cancel build")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(lambda: self.proc and self.proc.cancel())
        self.jobs = QSpinBox(minimum=1, maximum=os.cpu_count() or 16, value=max(1, (os.cpu_count() or 4) - 2))
        self.in_ram = QCheckBox("Build in RAM (no SSD writes; needs ~6 GB free)")
        self.in_ram.setChecked(str(up.build_dir_default()).startswith("/dev/shm"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono())
        self.log.setMaximumBlockCount(4000)
        self.log.setPlaceholderText("Build output appears here. Updating via pacman opens its own terminal window.")
        self.bar = QProgressBar()
        self.bar.setVisible(False)

        info = QGroupBox("chdman")
        f = QFormLayout(info)
        f.addRow("Use:", self.choice)
        f.addRow("Selected binary:", self.using)
        f.addRow("Latest MAME release:", self.latest)
        f.addRow("System package:", self.pkg)
        f.addRow("", self.verdict)
        actions = QGroupBox("Update")
        a = QGridLayout(actions)
        a.addWidget(check, 0, 0)
        a.addWidget(self.pac_btn, 0, 1)
        a.addWidget(self.build_btn, 1, 0)
        a.addWidget(self.cancel_btn, 1, 1)
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Build jobs:"))
        opts.addWidget(self.jobs)
        opts.addWidget(self.in_ram)
        opts.addStretch(1)
        a.addLayout(opts, 2, 0, 1, 2)
        if IS_WIN:  # no pacman / compiler: update by downloading MAME's official Windows chdman.exe
            self.pac_btn.setVisible(False)
            for w in (self.jobs, self.in_ram):
                w.setVisible(False)
            opts.itemAt(0).widget().setVisible(False)
            self.build_btn.setText("Download latest official chdman.exe…")
            self.build_btn.setToolTip("Downloads MAME's official Windows release from GitHub, checks it against "
                                      "MAME's published SHA-256, and keeps only chdman.exe.")
            self.cancel_btn.setText("Cancel download")
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(info, 3)
        top.addWidget(actions, 2)
        lay.addLayout(top)
        lay.addWidget(self.bar)
        lay.addWidget(self.log, 1)
        self.refresh()
        QTimer.singleShot(300, self.check)

    def refresh(self):
        cfg = get_config()
        self.choice.blockSignals(True)
        self.choice.clear()
        sysbin = find_tool("chdman")
        sv = ct.version(sysbin) if sysbin else None
        label = "Bundled" if (IS_WIN or FROZEN) else "System"
        self.choice.addItem(f"{label}: {sysbin or 'not installed'}" + (f"  (v{sv.number})" if sv else ""), "system")
        for tag, exe in up.built_versions():
            v = ct.version(str(exe))
            self.choice.addItem(f"Built by updater: {tag}" + (f"  (v{v.number})" if v else ""), str(exe))
        sel = cfg["chdman_path"] or "system"
        if sel != "system" and self.choice.findData(sel) < 0:
            self.choice.addItem(f"Custom: {sel}", sel)
        self.choice.addItem("Choose another chdman binary…", "__pick__")
        self.choice.setCurrentIndex(max(0, self.choice.findData(sel)))
        self.choice.blockSignals(False)
        exe = ct.chdman_bin()
        v = ct.version(exe) if exe else None
        self.using.setText(f"{exe or 'none'}" + (f" — {v.raw.replace('manager ', 'version ')}" if v else ""))
        self.win.base_title = f"ROM Optimizer — chdman {v.number if v else 'missing'}"
        self.win.setWindowTitle(self.win.base_title)
        self._verdict()

    def _choose(self, i):
        data = self.choice.itemData(i)
        cfg = get_config()
        if data == "__pick__":
            p, _ = QFileDialog.getOpenFileName(self, "Choose chdman binary", str(Path.home()))
            if not p or not ct.version(p):
                if p:
                    QMessageBox.warning(self, "Not chdman", f"{p} doesn't look like chdman.")
                self.refresh()
                return
            data = p
        cfg["chdman_path"] = data
        cfg.save()
        self.refresh()
        self.win.commands.reload()
        self.win.statusBar().showMessage("chdman selection saved — used by ROM Optimizer, Verifier and Renamer.",
                                         6000)

    def check(self):
        self.latest.setText("checking…")

        def job(_w):
            rel = None
            err = ""
            try:
                rel = up.latest_release()
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            return rel, err, up.package_info()

        w = Worker(job, parent=self)

        def done(res):
            rel, err, pkg = res
            self.release = rel
            self.latest.setText(f"{rel.number} ({rel.tag}, {rel.date})" if rel else f"could not check: {err}")
            if pkg.owner:
                self.pkg.setText(f"{pkg.owner} {pkg.installed} installed · {pkg.repo or '?'} in your pacman "
                                 f"database (as of your last sync)")
            else:
                self.pkg.setText("not managed by pacman")
            self.pac_btn.setEnabled(bool(pkg.owner))
            self._verdict()

        w.done.connect(done)
        w.start()
        self._check_worker = w

    def _verdict(self):
        exe = ct.chdman_bin()
        v = ct.version(exe) if exe else None
        rel = self.release
        if not v:
            self.verdict.setText("<span style='color:#d1452f'><b>chdman not found.</b></span> Update the system "
                                 "package or build it from source.")
        elif rel and v.key >= rel.key:
            self.verdict.setText(f"<span style='color:#2e9d53'><b>Up to date</b></span> — the selected chdman is "
                                 f"{v.number}, the latest MAME release is {rel.number}.")
        elif rel:
            self.verdict.setText(f"<span style='color:#d49a1b'><b>Update available:</b></span> {v.number} → "
                                 f"{rel.number}. Arch/CachyOS packages usually follow within days; building from "
                                 f"source gets it now.")
        else:
            self.verdict.setText("")

    def pacman_update(self):
        pkg = up.package_info().owner or "mame-tools"
        if not up.open_in_terminal(up.pacman_update_command(pkg)):
            QMessageBox.information(self, "Update", f"No terminal found. Run this yourself:\n\nsudo pacman -Syu {pkg}")

    def build(self):
        if not self.release:
            QMessageBox.information(self, "Build", "Check for updates first.")
            return
        tag = self.release.tag
        if any(t == tag for t, _ in up.built_versions()):
            if QMessageBox.question(self, "Build", f"{tag} is already built. Build it again?") != \
                    QMessageBox.StandardButton.Yes:
                return
        if IS_WIN:
            return self._download_windows(tag)
        root = Path("/dev/shm/romtools-build") if self.in_ram.isChecked() else Path.home() / ".cache/romtools/build"
        if QMessageBox.question(
                self, "Build chdman from source",
                f"Download the official MAME {self.release.number} source package (~160 MB, checked against MAME's published SHA-256) from github.com/mamedev/mame and build "
                f"chdman with {self.jobs.value()} jobs in {root}?\n\nThis takes a few minutes (about 2 on a 16-thread CPU) and uses a lot "
                f"of CPU. The build folder is deleted afterwards; only chdman is kept.") != \
                QMessageBox.StandardButton.Yes:
            return
        script = up.build_script(tag, root, self.jobs.value())
        self.log.clear()
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.proc = ct.Process(["nice", "-n", "10", "bash", "-c", script], lambda s: self.worker.message.emit(s))
        self.worker = Worker(lambda w: self.proc.run(), parent=self)
        self.worker.message.connect(self._log_line)
        self.worker.done.connect(lambda rc: self._built(rc, tag))
        self.build_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.win.set_busy(True)
        self.worker.start()

    def _download_windows(self, tag: str):
        if QMessageBox.question(self, "Download chdman", f"Download MAME {self.release.number}'s official Windows "
                                f"release (~90 MB) from github.com/mamedev/mame, check its SHA-256 and keep only "
                                f"chdman.exe?") != QMessageBox.StandardButton.Yes:
            return
        self.log.clear()
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)

        def job(w):
            try:
                up.install_official_windows(tag, w.message.emit, w.cancel)
                return 0
            except Exception as exc:  # noqa: BLE001
                w.message.emit(f"== Failed: {exc}")
                return 1

        self.worker = Worker(job, parent=self)
        self.proc = None
        self.cancel_btn.clicked.disconnect()
        self.cancel_btn.clicked.connect(lambda: self.worker and self.worker.cancel.set())
        self.worker.message.connect(self._log_line)
        self.worker.done.connect(lambda rc: self._built(rc, tag))
        self.build_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.win.set_busy(True)
        self.worker.start()

    def _log_line(self, s: str):
        self.log.appendPlainText(s)

    def _built(self, rc, tag):
        self.bar.setVisible(False)
        self.build_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.win.set_busy(False)
        exe = next((e for t, e in up.built_versions() if t == tag), None)
        if rc == 0 and exe:
            if QMessageBox.question(self, "chdman ready", f"chdman {tag} is ready:\n{exe}\n\nUse it from now on "
                                    f"(in all three tools)?") == QMessageBox.StandardButton.Yes:
                cfg = get_config()
                cfg["chdman_path"] = str(exe)
                cfg.save()
                self.win.commands.reload()
            self.refresh()
        else:
            QMessageBox.warning(self, "Build failed", "The build did not finish — see the log for details.")


# ====================================================================== window

class ConverterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ROM Optimizer")
        self.resize(1400, 860)
        self._busy = 0
        tb = QToolBar("Main")
        tb.setObjectName("main-toolbar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        for label, mod, icon in (("Open ROM Verifier", "verifier", "rom-verifier"),
                                 ("Open ROM Renamer", "renamer", "rom-renamer")):
            act = QAction(QIcon.fromTheme(icon, QIcon(str(resource_dir() / "icons" / f"{icon}.svg"))), label, self)
            act.triggered.connect(lambda _=False, m=mod: launch_app(m))
            tb.addAction(act)
        tb.addSeparator()
        tb.addWidget(ThemeButton(self))

        self.tabs = QTabWidget()
        self.batch = BatchTab(self)
        self.patch = PatchTab(self)
        self.space = SpaceTab(self)
        self.commands = CommandsTab(self)
        self.chdman = ChdmanTab(self)
        # chdman's own tools are for advanced use: one tab, two sub-tabs
        self.advanced = QTabWidget()
        self.advanced.setDocumentMode(True)
        self.advanced.addTab(self.commands, QIcon.fromTheme("utilities-terminal"), "Run a chdman command")
        self.advanced.addTab(self.chdman, QIcon.fromTheme("system-software-update"), "chdman version && updates")
        for w, icon, label, tip in (
                (self.batch, "view-list-details", "Tasks",
                 "Pick a task (convert to CHD/RVZ/NSZ, extract, trim, fix CHD types, playlists…), add files and "
                 "run the queue"),
                (self.space, "drive-harddisk", "Save space",
                 "Analyse a folder: which files can be stored smaller (or load faster) without losing anything"),
                (self.patch, "document-edit", "Patch",
                 "Apply IPS / BPS / UPS / PPF / xdelta patches (translations, hacks), with auto-matching"),
                (self.advanced, "utilities-terminal", "chdman (advanced)",
                 "Run any chdman command by hand, choose which chdman is used, and update it")):
            self.tabs.setTabToolTip(self.tabs.addTab(w, QIcon.fromTheme(icon), label), tip)
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("Ready")
        self.mini_label = QLabel()
        self.mini_label.setToolTip("The task queue is still running. Switch to the Tasks tab for details.")
        self.mini_bar = QProgressBar()
        self.mini_bar.setToolTip("Task queue progress (all jobs). Shown here only while you're on another tab.")
        self.mini_bar.setRange(0, 1000)
        self.mini_bar.setMaximumWidth(220)
        self.mini_bar.setMaximumHeight(16)
        self.mini_bar.setTextVisible(True)
        for w in (self.mini_label, self.mini_bar):
            w.setVisible(False)
            self.statusBar().addPermanentWidget(w)
        self._queue_state = None
        self.tabs.currentChanged.connect(lambda _i: self._apply_queue_state())
        restore_geometry(self, "converter")

    def _apply_queue_state(self):
        """The bottom-right copy of the queue progress only appears when the Batch tab (and its own queue bar)
        isn't visible, so there's never two identical bars on screen."""
        st = self._queue_state
        off_batch = self.tabs.currentWidget() is not self.batch
        show = st is not None and off_batch
        self.mini_bar.setVisible(show)
        self.mini_label.setVisible(show)
        if st is None:
            return
        p, finished, total, current, bits = st
        self.mini_bar.setValue(int(p * 1000))
        self.mini_bar.setFormat(f"{p * 100:.0f}% · {finished}/{total} jobs")
        eta = next((b for b in bits if b.startswith("ETA")), "")
        self.mini_label.setText(f"<b>Task queue:</b> {finished} of {total} jobs done" + (f" · {eta}" if eta else ""))
        if current:
            self.statusBar().showMessage(f"Now running: {current}" if off_batch else f"Now running: {current}")

    def show_queue_progress(self, p, finished: int, total: int, current: str, bits: list[str]):
        """Mirror queue progress in the window status bar, tab label and title (visible from any tab)."""
        active = p is not None
        self._queue_state = (p, finished, total, current, bits) if active else None
        self._apply_queue_state()
        if active:
            self.tabs.setTabText(0, f"Tasks ({finished}/{total})")
            self.setWindowTitle(f"ROM Optimizer — {p * 100:.0f}% · {finished}/{total} jobs")
        else:
            self.tabs.setTabText(0, "Tasks")
            self.setWindowTitle(getattr(self, "base_title", "ROM Optimizer"))

    def set_busy(self, on: bool):
        self._busy += 1 if on else -1

    def closeEvent(self, ev):
        if self._busy > 0:
            if QMessageBox.question(self, "Quit", "A job is still running. Stop it and quit?") != \
                    QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            if self.batch.runner:
                self.batch.runner.stop()
            for p in (self.commands.proc, self.chdman.proc):
                if p:
                    p.cancel()
            for w in (self.batch.worker, self.commands.worker, self.chdman.worker, self.patch.worker,
                      self.space.worker):
                if w:
                    w.wait(10000)
        self.batch.save_state()
        save_geometry(self, "converter")
        super().closeEvent(ev)


def main():
    app = make_app("ROM Optimizer", "rom-optimizer")
    app.setApplicationVersion(__version__)
    w = ConverterWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
