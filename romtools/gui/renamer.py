"""ROM Renamer - preview-first renaming from DAT matches or cleanup rules, with undo."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar,
                               QVBoxLayout, QWidget, QListWidget, QListWidgetItem, QSizePolicy)

from .. import __version__
from .. import renamer as rn
from .. import scanner as sc
from ..cache import HashCache
from ..config import get_config
from ..datmanager import IndexCache
from .common import LibraryPicker, SystemList, TempModeCombo, Worker, is_dark, make_app, restore_geometry, save_geometry
from .datdialog import DatDialog

C_ON, C_SYS, C_OLD, C_NEW, C_WHY, C_PROBLEM = range(6)


class HistoryDialog(QDialog):
    def __init__(self, parent, cache):
        super().__init__(parent)
        self.setWindowTitle("Rename history")
        self.resize(760, 460)
        self.cache = cache
        self.list = QListWidget()
        self.view = QTableWidget(0, 2)
        self.view.setHorizontalHeaderLabels(["Old name", "New name"])
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.view.verticalHeader().hide()
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.currentRowChanged.connect(self._show)
        self.undo_btn = QPushButton(QIcon.fromTheme("edit-undo"), "Undo this batch")
        self.undo_btn.clicked.connect(self._undo)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.view)
        split.setSizes([240, 520])
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.addButton(self.undo_btn, QDialogButtonBox.ButtonRole.ActionRole)
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addWidget(bb)
        self._load()

    def _load(self):
        self.journals = rn.list_journals()
        self.list.clear()
        for p, d in self.journals:
            n = sum(1 for e in d["entries"] if e.get("done"))
            label = f"{d.get('created', p.stem)}  —  {n} change(s)" + ("  [undone]" if d.get("undone") else "")
            QListWidgetItem(label, self.list)
        if self.journals:
            self.list.setCurrentRow(0)
        self.undo_btn.setEnabled(bool(self.journals))

    def _show(self, row):
        self.view.setRowCount(0)
        if row < 0 or row >= len(self.journals):
            return
        _p, d = self.journals[row]
        rows = [e for e in d["entries"]]
        self.view.setRowCount(len(rows))
        for i, e in enumerate(rows):
            old = QTableWidgetItem(Path(e["src"]).name)
            new = QTableWidgetItem(Path(e["dst"]).name + ("  (playlist edited)" if e["kind"] == rn.M3U_EDIT else ""))
            if not e.get("done"):
                for it in (old, new):
                    it.setForeground(QColor("#888"))
            self.view.setItem(i, 0, old)
            self.view.setItem(i, 1, new)
        self.undo_btn.setEnabled(any(e.get("done") for e in rows))

    def _undo(self):
        row = self.list.currentRow()
        if row < 0:
            return
        p, d = self.journals[row]
        n = sum(1 for e in d["entries"] if e.get("done"))
        if QMessageBox.question(self, "Undo", f"Revert {n} change(s) from {d.get('created')}?") != \
                QMessageBox.StandardButton.Yes:
            return
        errs = rn.undo(p, self.cache)
        if errs:
            QMessageBox.warning(self, "Undo finished with problems", "\n".join(errs[:30]))
        else:
            QMessageBox.information(self, "Undo", "All changes reverted.")
        self._load()


class RenamerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ROM Renamer")
        self.resize(1400, 820)
        self.cfg = get_config()
        self.cache = HashCache()
        self.indexes = IndexCache()
        self.ops: list[rn.Op] = []
        self.base_ops: list[rn.Op] = []
        self.worker: Worker | None = None
        self._filling = False

        tb = QToolBar("Main")
        tb.setObjectName("main-toolbar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.act_preview = QAction(QIcon.fromTheme("document-preview"), "Build preview", self)
        self.act_preview.setShortcut(QKeySequence("F5"))
        self.act_preview.triggered.connect(self.build_preview)
        self.act_stop = QAction(QIcon.fromTheme("media-playback-stop"), "Stop", self)
        self.act_stop.setEnabled(False)
        self.act_stop.triggered.connect(lambda: self.worker and self.worker.cancel.set())
        self.act_apply = QAction(QIcon.fromTheme("dialog-ok-apply"), "Apply…", self)
        self.act_apply.setEnabled(False)
        self.act_apply.triggered.connect(self.apply)
        act_hist = QAction(QIcon.fromTheme("edit-undo"), "History / Undo…", self)
        act_hist.triggered.connect(lambda: HistoryDialog(self, self.cache).exec())
        act_dats = QAction(QIcon.fromTheme("view-list-details"), "Manage DATs…", self)
        act_dats.triggered.connect(self.manage_dats)
        act_ver = QAction(QIcon.fromTheme("security-high"), "Open ROM Verifier", self)
        act_ver.triggered.connect(lambda: subprocess.Popen([sys.executable, "-m", "romtools.gui.verifier"],
                                                           cwd=str(Path(__file__).resolve().parents[2])))
        for a in (self.act_preview, self.act_stop, self.act_apply):
            tb.addAction(a)
        tb.addSeparator()
        self.mode = TempModeCombo(lambda: bool(self.worker and self.worker.isRunning()),
                                  lambda m: self.statusBar().showMessage(m, 8000))
        tb.addWidget(self.mode)
        tb.addSeparator()
        for a in (act_hist, act_dats, act_ver):
            tb.addAction(a)

        # ---- left
        self.picker = LibraryPicker()
        self.picker.changed.connect(self._library_changed)
        self.systems = SystemList()
        a, n = QPushButton("All"), QPushButton("None")
        a.clicked.connect(lambda: self.systems.set_all(True))
        n.clicked.connect(lambda: self.systems.set_all(False))
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Systems</b>"))
        row.addStretch(1)
        row.addWidget(a)
        row.addWidget(n)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("<b>Library</b>"))
        ll.addWidget(self.picker)
        ll.addLayout(row)
        ll.addWidget(self.systems, 1)

        # ---- mode tabs
        self.tabs = QTabWidget()
        dat_tab = QWidget()
        dl = QVBoxLayout(dat_tab)
        info = QLabel("Renames <b>verified-good</b> ROMs whose file name differs from the No-Intro / Redump DAT. "
                      "Files are identified by hash, so the result is always the official name. Hashes are shared "
                      "with ROM Verifier, so anything already verified is instant.")
        info.setWordWrap(True)
        self.use_dat_ext = QCheckBox("Use the DAT's file extension (e.g. .smc → .sfc)")
        self.use_dat_ext.setChecked(self.cfg["rename_use_dat_ext"])
        self.skip_uncached = QCheckBox("Skip CHDs that haven't been verified yet (avoids long extractions)")
        self.skip_uncached.setChecked(True)
        dl.addWidget(info)
        dl.addWidget(self.use_dat_ext)
        dl.addWidget(self.skip_uncached)
        dl.addStretch(1)
        self.tabs.addTab(dat_tab, QIcon.fromTheme("checkmark"), "Match DAT names")

        rules_tab = QWidget()
        rg = QHBoxLayout(rules_tab)
        box = QGroupBox("Rules")
        bl = QVBoxLayout(box)
        self.r_dup = QCheckBox("Remove duplicate markers:  “Game (USA) (1).sfc” → “Game (USA).sfc”")
        self.r_dup.setChecked(True)
        self.r_us = QCheckBox("Underscores to spaces:  “Bayonetta_3” → “Bayonetta 3”")
        self.r_dots = QCheckBox("Dots between words to spaces:  “Some.Game” → “Some Game”")
        self.r_the = QCheckBox("Move leading “The”:  “The Legend of X - Y” → “Legend of X, The - Y”")
        self.r_safe = QCheckBox("Remove characters that break SMB/Windows ( : ? * \" < > | \\ )")
        self.r_safe.setChecked(True)
        self.r_space = QCheckBox("Collapse repeated spaces / trim")
        self.r_space.setChecked(True)
        self.r_ext = QCheckBox("Lower-case extensions (.ISO → .iso)")
        self.r_dirs = QCheckBox("Also rename folders (Switch / PS3 / extracted games)")
        for w in (self.r_dup, self.r_us, self.r_dots, self.r_the, self.r_safe, self.r_space, self.r_ext, self.r_dirs):
            bl.addWidget(w)
        fbox = QGroupBox("Find && replace")
        fl = QFormLayout(fbox)
        self.r_find = QLineEdit()
        self.r_repl = QLineEdit()
        self.r_regex = QCheckBox("Regular expression (Python syntax, \\1 for groups)")
        fl.addRow("Find:", self.r_find)
        fl.addRow("Replace with:", self.r_repl)
        fl.addRow("", self.r_regex)
        rg.addWidget(box, 1)
        rg.addWidget(fbox, 1)
        self.tabs.addTab(rules_tab, QIcon.fromTheme("tools"), "Cleanup rules")

        # ---- preview table
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "System", "Current name", "New name (double-click to edit)",
                                              "Reason", "Problem"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.EditKeyPressed)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_ON, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(C_SYS, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(C_OLD, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_NEW, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_WHY, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_PROBLEM, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(C_WHY, 200)
        self.table.setColumnWidth(C_PROBLEM, 180)
        self.table.itemChanged.connect(self._item_changed)

        sel_all, sel_none = QPushButton("Check all"), QPushButton("Check none")
        sel_all.clicked.connect(lambda: self._check_all(True))
        sel_none.clicked.connect(lambda: self._check_all(False))
        self.summary = QLabel("Build a preview to see proposed renames. Nothing is changed until you press Apply.")
        bottom = QHBoxLayout()
        bottom.addWidget(sel_all)
        bottom.addWidget(sel_none)
        bottom.addWidget(self.summary, 1)
        self.apply_btn = QPushButton(QIcon.fromTheme("dialog-ok-apply"), "Apply checked renames…")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
        bottom.addWidget(self.apply_btn)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.tabs.setSizePolicy(self.tabs.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Maximum)
        rl.addWidget(self.tabs)
        rl.addWidget(self.table, 1)
        rl.addLayout(bottom)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 1100])
        self.setCentralWidget(split)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.progress.setVisible(False)
        self.status = QLabel("Ready")
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.progress)
        restore_geometry(self, "renamer")
        self.systems.load(self.picker.path())

    # ------------------------------------------------------------------
    def _library_changed(self, path):
        self.cfg["library_root"] = path
        self.cfg.save()
        self.systems.load(Path(path).expanduser())

    def manage_dats(self):
        DatDialog(self.picker.path(), self).exec()
        self.systems.load(self.picker.path())

    def _rules(self) -> rn.Rules:
        return rn.Rules(strip_dup_suffix=self.r_dup.isChecked(), underscores=self.r_us.isChecked(),
                        dots_to_spaces=self.r_dots.isChecked(), collapse_spaces=self.r_space.isChecked(),
                        the_suffix=self.r_the.isChecked(), cifs_safe=self.r_safe.isChecked(),
                        find=self.r_find.text(), replace=self.r_repl.text(), regex=self.r_regex.isChecked(),
                        include_folders=self.r_dirs.isChecked(), extensions_lower=self.r_ext.isChecked())

    def build_preview(self):
        if self.worker and self.worker.isRunning():
            return
        root = self.picker.path()
        systems = self.systems.checked()
        if not systems:
            QMessageBox.information(self, "Preview", "Tick at least one system on the left.")
            return
        self.systems.remember()
        mode_dat = self.tabs.currentIndex() == 0
        self.cfg["rename_use_dat_ext"] = self.use_dat_ext.isChecked()
        self.cfg.save()
        rules = self._rules()
        use_ext, skip_unc = self.use_dat_ext.isChecked(), self.skip_uncached.isChecked()
        cfg, cache, idxc = self.cfg, self.cache, self.indexes

        def job(w: Worker):
            if not mode_dat:
                return rn.plan_from_rules({s: root / s for s in systems}, rules), []
            results, skipped, nodat = [], [], []
            items, indexes = [], {}
            for s in systems:
                dats = cfg.system(s)["dats"]
                if not dats:
                    nodat.append(s)
                    continue
                w.progress.emit(0, 0, f"Loading DAT for {s}…", 0)
                indexes[s] = idxc.get(dats)
                for it in sc.enumerate_system(s, root / s):
                    if it.kind == "folder":
                        continue
                    if skip_unc and it.kind == "chd":
                        try:
                            st = it.path.stat()
                            if not cache.get(it.path, st.st_size, st.st_mtime):
                                skipped.append(it)
                                continue
                        except OSError:
                            continue
                    items.append(it)
            scanner = sc.make_scanner(cfg, cache, sc.QUICK)
            scanner.cancel = w.cancel
            scanner.run(items, indexes, results.append, lambda d, t, m, f: w.progress.emit(d, t, m, f))
            notes = []
            if nodat:
                notes.append(f"No DAT for: {', '.join(nodat)} (use Manage DATs, or the Cleanup rules tab)")
            if skipped:
                notes.append(f"{len(skipped)} unverified CHD(s) skipped — run ROM Verifier on them first, "
                             f"or untick the skip option")
            return rn.plan_from_results(results, use_ext), notes

        self.worker = Worker(job, parent=self)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._preview_ready)
        self.worker.failed.connect(lambda e: (self._busy(False), QMessageBox.critical(self, "Preview failed", e)))
        self._busy(True)
        self.worker.start()

    def _on_progress(self, d, t, msg, _f):
        if t:
            self.progress.setRange(0, t)
            self.progress.setValue(d)
        self.status.setText(msg)

    def _busy(self, on):
        self.act_preview.setEnabled(not on)
        self.act_stop.setEnabled(on)
        self.progress.setVisible(on)
        self.progress.setRange(0, 0)
        if on:
            self.act_apply.setEnabled(False)
            self.apply_btn.setEnabled(False)

    def _preview_ready(self, res):
        ops, notes = res
        self._busy(False)
        self.base_ops = ops
        self.ops = rn.validate(rn.add_playlist_ops(ops))
        self._fill()
        self.status.setText("  ·  ".join(notes) if notes else "Preview ready — nothing has been changed yet.")

    # ------------------------------------------------------------------ table
    def _fill(self):
        self._filling = True
        self.table.setRowCount(len(self.ops))
        warn = QColor("#e0b050" if is_dark() else "#9a6b00")
        for r, op in enumerate(self.ops):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            if not op.ok:
                op.enabled = False
            chk.setCheckState(Qt.CheckState.Checked if op.enabled else Qt.CheckState.Unchecked)
            self.table.setItem(r, C_ON, chk)
            for c, text in ((C_SYS, op.system or op.src.parent.name), (C_OLD, op.src.name), (C_WHY, op.reason)):
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                it.setToolTip(str(op.src) if c == C_OLD else text)
                self.table.setItem(r, c, it)
            new = QTableWidgetItem(op.dst.name)
            if op.kind == rn.M3U_EDIT:
                new.setFlags(new.flags() & ~Qt.ItemFlag.ItemIsEditable)
                new.setToolTip("Playlist contents:\n" + (op.new_text or ""))
            self.table.setItem(r, C_NEW, new)
            prob = QTableWidgetItem(op.problem or "ok")
            prob.setFlags(prob.flags() & ~Qt.ItemFlag.ItemIsEditable)
            prob.setForeground(warn if op.problem else QColor("#2e9d53"))
            self.table.setItem(r, C_PROBLEM, prob)
        self._filling = False
        self._update_summary()

    def _item_changed(self, item: QTableWidgetItem):
        if self._filling:
            return
        r = item.row()
        op = self.ops[r]
        if item.column() == C_ON:
            op.enabled = item.checkState() == Qt.CheckState.Checked
        elif item.column() == C_NEW:
            op.dst = op.src.with_name(item.text().strip())
            op.enabled = True
        else:
            return
        # Playlist edits depend on which discs are renamed: rebuild them.
        enabled_before = {id(o): o.enabled for o in self.ops}
        self.ops = rn.validate(rn.add_playlist_ops(self.base_ops))
        for o in self.ops:
            if o.ok and id(o) in enabled_before:
                o.enabled = enabled_before[id(o)]
        self._fill()

    def _check_all(self, on: bool):
        for op in self.ops:
            op.enabled = on and op.ok
        self._fill()

    def _update_summary(self):
        n_on = sum(1 for o in self.ops if o.enabled and o.ok)
        n_bad = sum(1 for o in self.ops if not o.ok)
        self.summary.setText(f"{len(self.ops)} proposed · {n_on} checked · {n_bad} with problems (not applied)"
                             if self.ops else "No renames needed for the checked systems.")
        self.act_apply.setEnabled(n_on > 0)
        self.apply_btn.setEnabled(n_on > 0)
        self.apply_btn.setText(f"Apply {n_on} change(s)…")

    # ------------------------------------------------------------------ apply
    def apply(self):
        todo = [o for o in rn.validate(self.ops) if o.enabled and o.ok]
        if not todo:
            return
        sample = "\n".join(f"• {o.src.name}  →  {o.dst.name}" for o in todo[:12])
        more = f"\n… and {len(todo) - 12} more" if len(todo) > 12 else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Apply renames")
        box.setText(f"Rename {len(todo)} item(s) in your library?")
        box.setInformativeText("Existing files are never overwritten. Every change is logged and can be reverted "
                               "from History / Undo.")
        box.setDetailedText(sample + more)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        jpath, errors = rn.apply(self.ops, self.cache)
        done = len(todo) - len(errors)
        if errors:
            QMessageBox.warning(self, "Some renames failed", f"{done} done, {len(errors)} failed:\n\n"
                                + "\n".join(errors[:30]))
        self.status.setText(f"Renamed {done} item(s). Undo is available in History (log: {jpath.name}).")
        self.ops = []
        self._fill()

    def closeEvent(self, ev):
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set()
            self.worker.wait(15000)
        self.systems.remember()
        save_geometry(self, "renamer")
        super().closeEvent(ev)


def main():
    app = make_app("ROM Renamer", "rom-renamer")
    app.setApplicationVersion(__version__)
    w = RenamerWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
