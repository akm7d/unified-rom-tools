"""ROM Renamer - preview-first renaming from DAT matches, console package data and cleanup rules, with undo."""

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
from .common import LibraryPicker, SystemList, TempModeCombo, ThemeButton, UpdateButton, startup_update_check, Worker, is_dark, make_app, restore_geometry, save_geometry
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
        act_ver.triggered.connect(lambda: __import__("romtools.compat", fromlist=["x"]).launch_app("verifier"))
        for a in (self.act_preview, self.act_stop, self.act_apply):
            tb.addAction(a)
        tb.addSeparator()
        for a in (act_hist, act_dats, act_ver):
            tb.addAction(a)
        tb.addWidget(ThemeButton(self))
        tb.addWidget(UpdateButton(self, "renamer"))

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

        # ---- what to rename: one panel, a section per kind of renaming (each switched on/off by its title)
        st = self.cfg["window_state"].get("renamer_sections", {})

        sec_dat = QGroupBox("Official names from DATs (No-Intro / Redump)")
        sec_dat.setCheckable(True)
        sec_dat.setChecked(st.get("dat", True))
        sec_dat.setToolTip("ROMs are identified by hash, so the new name is always the official one. Hashes are shared "
                           "with ROM Verifier, so anything it has checked is instant.")
        dl = QVBoxLayout(sec_dat)
        self.use_dat_ext = QCheckBox("Change the extension to the one the DAT uses (e.g. .smc → .sfc)")
        self.use_dat_ext.setToolTip("Off: only the name changes and your extension stays (Super Mario World (USA).smc).\n"
                                    "On: the extension becomes the DAT's too (Super Mario World (USA).sfc).\n"
                                    "Never applies to CHDs, archives, .cue sets, or ROMs matched without their "
                                    "copier header.")
        self.use_dat_ext.setChecked(self.cfg["rename_use_dat_ext"])
        self.skip_uncached = QCheckBox("Skip CHDs ROM Verifier hasn't checked yet (checking unpacks each disc)")
        self.skip_uncached.setChecked(st.get("skip_uncached", True))
        # where unpacked CHDs go while being checked: only matters when unchecked CHDs are included
        self.mode = TempModeCombo(lambda: bool(self.worker and self.worker.isRunning()),
                                  lambda m: self.statusBar().showMessage(m, 8000))
        self.mode.setToolTip("Where CHDs are unpacked while they're checked: RAM (no SSD writes, slower) or SSD")
        mrow = QHBoxLayout()
        mrow.setContentsMargins(22, 0, 0, 0)
        mrow.addWidget(QLabel("Unpack them in:"))
        mrow.addWidget(self.mode)
        mrow.addStretch(1)
        self.skip_uncached.toggled.connect(lambda on: self.mode.setEnabled(not on))
        self.mode.setEnabled(not self.skip_uncached.isChecked())
        for w in (self.use_dat_ext, self.skip_uncached):
            dl.addWidget(w)
        dl.addLayout(mrow)
        dl.addStretch(1)

        sec_pkg = QGroupBox("Console packages (PS4 / Switch)")
        sec_pkg.setCheckable(True)
        sec_pkg.setChecked(st.get("pkg", True))
        sec_pkg.setToolTip("PS4 PKG and Switch NSP / NSZ / XCI / XCZ named from their own data: “Title [CUSA00000] "
                           "(Update v1.09)”, “Title [0100…][v0]”. Titles you already use for a game are kept. Switch "
                           "packages need your prod.keys (ROM Verifier → Settings).")
        pl = QVBoxLayout(sec_pkg)
        self.p_rename = QCheckBox("Name them from the package data")
        self.p_rename.setChecked(st.get("p_rename", True))
        self.p_org = QCheckBox("One folder per game (base game, updates, DLC)")
        self.p_org.setToolTip("Uses the game's existing folder if it has one")
        self.p_org.setChecked(st.get("p_org", False))
        self.p_multi = QCheckBox("…only for games with more than one file")
        self.p_multi.setChecked(st.get("p_multi", True))
        self.p_org.toggled.connect(self.p_multi.setEnabled)
        self.p_multi.setEnabled(self.p_org.isChecked())
        mi = QHBoxLayout()
        mi.setContentsMargins(22, 0, 0, 0)
        mi.addWidget(self.p_multi)
        for w in (self.p_rename, self.p_org):
            pl.addWidget(w)
        pl.addLayout(mi)
        pl.addStretch(1)

        sec_rules = QGroupBox("Cleanup rules (any file)")
        sec_rules.setCheckable(True)
        sec_rules.setChecked(st.get("rules", False))
        sec_rules.setToolTip("Tidy names that no DAT or package can name: duplicate markers, underscores, characters "
                             "that break Samba / Windows, and your own find & replace")
        rg = QVBoxLayout(sec_rules)
        self.r_dup = QCheckBox("Remove duplicate markers: “Game (1)” → “Game”")
        self.r_dup.setChecked(True)
        self.r_us = QCheckBox("Underscores → spaces")
        self.r_dots = QCheckBox("Dots between words → spaces")
        self.r_the = QCheckBox("Leading “The” → “…, The”")
        self.r_safe = QCheckBox("Remove characters that break Samba / Windows ( : ? * \" < > | \\ )")
        self.r_safe.setChecked(True)
        self.r_space = QCheckBox("Collapse repeated spaces")
        self.r_space.setChecked(True)
        self.r_ext = QCheckBox("Lower-case extensions (.ISO → .iso)")
        self.r_dirs = QCheckBox("Also rename folders")
        self._rule_boxes = {"dup": self.r_dup, "us": self.r_us, "dots": self.r_dots, "the": self.r_the,
                            "safe": self.r_safe, "space": self.r_space, "ext": self.r_ext, "dirs": self.r_dirs}
        for k, box in self._rule_boxes.items():          # remembered like the other choices
            if k in st.get("rule_boxes", {}):
                box.setChecked(st["rule_boxes"][k])
        grid = QHBoxLayout()
        c1, c2 = QVBoxLayout(), QVBoxLayout()
        for i, w in enumerate((self.r_dup, self.r_us, self.r_dots, self.r_the, self.r_safe, self.r_space, self.r_ext,
                               self.r_dirs)):
            (c1 if i < 4 else c2).addWidget(w)
        grid.addLayout(c1)
        grid.addLayout(c2)
        rg.addLayout(grid)
        self.r_find = QLineEdit()
        self.r_find.setPlaceholderText("find")
        self.r_repl = QLineEdit()
        self.r_repl.setPlaceholderText("replace with")
        self.r_regex = QCheckBox("Regex")
        self.r_regex.setToolTip("Regular expression (Python syntax, \\1 for groups)")
        fr = QHBoxLayout()
        fr.addWidget(QLabel("Find & replace:"))
        fr.addWidget(self.r_find, 1)
        fr.addWidget(self.r_repl, 1)
        fr.addWidget(self.r_regex)
        rg.addLayout(fr)

        self.sec_dat, self.sec_pkg, self.sec_rules = sec_dat, sec_pkg, sec_rules
        for g in (sec_dat, sec_pkg, sec_rules):
            g.toggled.connect(lambda _on: self._sections_changed())
        self.sections = QWidget()
        secl = QHBoxLayout(self.sections)
        secl.setContentsMargins(0, 0, 0, 0)
        secl.addWidget(sec_dat, 3)
        secl.addWidget(sec_pkg, 3)
        secl.addWidget(sec_rules, 5)
        self.order_note = QLabel("<small>One preview for all ticked sections. If two would rename the same file, the "
                                 "DAT name wins, then the package name, then the cleanup rules.</small>")
        self.order_note.setWordWrap(True)

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
        self.sections.setSizePolicy(self.sections.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Maximum)
        rl.addWidget(self.sections)
        rl.addWidget(self.order_note)
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

    def _save_sections(self):
        self.cfg["window_state"]["renamer_sections"] = {
            "dat": self.sec_dat.isChecked(), "pkg": self.sec_pkg.isChecked(), "rules": self.sec_rules.isChecked(),
            "skip_uncached": self.skip_uncached.isChecked(), "p_rename": self.p_rename.isChecked(),
            "p_org": self.p_org.isChecked(), "p_multi": self.p_multi.isChecked(),
            "rule_boxes": {k: b.isChecked() for k, b in self._rule_boxes.items()}}
        self.cfg.save()

    def _sections_changed(self):
        on = self.sec_dat.isChecked() or self.sec_pkg.isChecked() or self.sec_rules.isChecked()
        self.act_preview.setEnabled(on and not (self.worker and self.worker.isRunning()))
        self._save_sections()

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
        do_dat, do_pkg, do_rules = (self.sec_dat.isChecked(), self.sec_pkg.isChecked(), self.sec_rules.isChecked())
        if not (do_dat or do_pkg or do_rules):
            QMessageBox.information(self, "Preview", "Tick at least one section: official names, console packages "
                                                     "or cleanup rules.")
            return
        self._save_sections()
        p_opts = (self.p_rename.isChecked(), self.p_org.isChecked(), self.p_multi.isChecked())
        self.cfg["rename_use_dat_ext"] = self.use_dat_ext.isChecked()
        self.cfg.save()
        rules = self._rules()
        use_ext, skip_unc = self.use_dat_ext.isChecked(), self.skip_uncached.isChecked()
        cfg, cache, idxc = self.cfg, self.cache, self.indexes

        def job(w: Worker):
            from .. import library
            from .. import packages as pk
            from ..datmanager import system_for
            say = lambda m: w.progress.emit(0, 0, m, 0)       # noqa: E731
            ops: list[rn.Op] = []
            notes: list[str] = []
            if do_dat:
                results, skipped, items = [], [], []
                listed, indexes = library.collect(cfg, root, systems, idxc, say)
                pkg_sys = {s for s in systems if (system_for(cfg, s) or None) and system_for(cfg, s).source == "package"}
                nodat = sorted({it.system for it in listed if indexes.get(it.system) is None} - pkg_sys
                               - {library.OTHER})
                for it in listed:
                    if indexes.get(it.system) is None or it.kind in ("folder", "package"):
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
                for op in rn.plan_from_results(results, use_ext):
                    op.reason = op.reason if op.reason.startswith("DAT") else "DAT: " + op.reason
                    ops.append(op)
                if nodat:
                    notes.append(f"No DAT for: {', '.join(nodat)} (Manage DATs, or use the cleanup rules)")
                if skipped:
                    notes.append(f"{len(skipped)} CHD(s) not checked yet were skipped (check them in ROM Verifier, "
                                 f"or untick “Skip CHDs…”)")
            if do_pkg and not w.cancel.is_set():
                pops, pnotes = rn.plan_packages(library.rule_folders(cfg, root, systems), pk.switch_keys(cfg),
                                                *p_opts, progress=say)
                for op in pops:
                    op.reason = "Package: " + op.reason
                ops += pops
                notes += pnotes
            if do_rules and not w.cancel.is_set():
                for op in rn.plan_from_rules(library.rule_folders(cfg, root, systems), rules):
                    op.reason = "Cleanup: " + op.reason if op.reason else "Cleanup"
                    ops.append(op)
            # one change per file: DAT name, then package name, then cleanup rules (that's the order added above)
            seen, merged = set(), []
            for op in ops:
                if op.src not in seen:
                    seen.add(op.src)
                    merged.append(op)
            return merged, notes

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
                if c == C_OLD and op.move:
                    text = f"{op.src.parent.name}/{op.src.name}"
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                it.setToolTip(str(op.src) if c == C_OLD else text)
                self.table.setItem(r, c, it)
            new = QTableWidgetItem(op.dst.name)
            if op.move:
                new.setToolTip(f"moves to {op.dst.parent}")
                new.setText(f"{op.dst.name}")
                new.setData(Qt.ItemDataRole.UserRole, str(op.dst.parent))
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
            op.dst = op.dst.with_name(item.text().strip())
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
    startup_update_check(w, "renamer")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
