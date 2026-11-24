"""ROM Verifier's questions about the whole collection: duplicates, missing discs, BIOS files."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QListWidget, QMenu, QMessageBox, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from .. import bios as bi
from .. import collection as col
from .. import scanner as sc
from .. import textfile
from ..config import get_config
from .common import STATUS_COLORS, Worker, human

OK_COLOR, WARN_COLOR, BAD_COLOR = "#2e9d53", "#d49a1b", "#d1452f"


def _tree(headers: list[str], widths: list[int]) -> QTreeWidget:
    t = QTreeWidget()
    t.setHeaderLabels(headers)
    t.setAlternatingRowColors(True)
    t.setUniformRowHeights(True)
    t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    for c, w in enumerate(widths):
        t.setColumnWidth(c, w)
    t.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.header().setStretchLastSection(True)
    t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    return t


def _folder_menu(tree: QTreeWidget) -> None:
    """Right-click: open the folder of the row's path (kept in UserRole + 1)."""
    def menu(pos):
        it = tree.itemAt(pos)
        p = it.data(0, Qt.ItemDataRole.UserRole + 1) if it else None
        if p:
            m = QMenu(tree)
            m.addAction(QIcon.fromTheme("folder-open"), "Open containing folder",
                        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            m.exec(tree.viewport().mapToGlobal(pos))
    tree.customContextMenuRequested.connect(menu)


class DuplicatesDialog(QDialog):
    """Games stored more than once. Nothing is ticked at first and nothing happens by itself; ticked copies go to the
    trash, and at least one copy of each game always stays."""

    def __init__(self, results: list[sc.Result], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Duplicates")
        self.resize(1180, 640)
        self.results = results
        self.trashed: list[Path] = []
        self.groups: list[col.Group] = []
        intro = QLabel("From the results in ROM Verifier's list. <b>Keep</b> marks the copy that looks best; it's only a "
                       "suggestion. Tick copies to move them to the trash - at least one of each game always stays.")
        intro.setWordWrap(True)
        self.kinds = {}
        row = QHBoxLayout()
        for k, on in ((col.IDENTICAL, True), (col.SAME_GAME, True), (col.VERSIONS, False)):
            b = QCheckBox(k.capitalize())
            b.setToolTip(col.KIND_HELP[k])
            b.setChecked(on)
            b.toggled.connect(self._fill)
            self.kinds[k] = b
            row.addWidget(b)
        row.addStretch(1)
        self.summary = QLabel()
        row.addWidget(self.summary)
        self.tree = _tree(["Game / copy", "Suggestion", "Status", "Size", "Folder"], [470, 150, 100, 90, 300])
        self.tree.itemChanged.connect(lambda *_: self._count())
        _folder_menu(self.tree)
        self.b_spare = QPushButton("Tick spare identical copies")
        self.b_spare.setToolTip("Ticks every copy with exactly the same data except the one marked Keep")
        self.b_spare.clicked.connect(self._tick_spare)
        b_none = QPushButton("Untick all")
        b_none.clicked.connect(lambda: self._set_all(False))
        self.b_trash = QPushButton(QIcon.fromTheme("user-trash"), "Move ticked to trash…")
        self.b_trash.clicked.connect(self._trash)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        for w in (self.b_spare, b_none, self.b_trash):
            bottom.addWidget(w)
        bottom.addStretch(1)
        bottom.addWidget(bb)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(row)
        lay.addWidget(self.tree, 1)
        lay.addLayout(bottom)
        self._fill()

    def _fill(self):
        gone = set(self.trashed)
        self.all_groups = col.duplicates([r for r in self.results if r.item.path not in gone])
        self.groups = [g for g in self.all_groups if self.kinds[g.kind].isChecked()]
        self.tree.blockSignals(True)
        self.tree.clear()
        for g in self.groups:
            top = QTreeWidgetItem([f"{g.title}", g.kind, g.system, human(sum(c.result.size for c in g.copies)), ""])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setToolTip(1, col.KIND_HELP[g.kind])
            self.tree.addTopLevelItem(top)
            for c in g.copies:
                it = QTreeWidgetItem([c.result.item.label, f"Keep ({c.why})" if c.keep else "", c.result.status,
                                      human(c.result.size) if c.result.size else "", str(c.path.parent)])
                it.setData(0, Qt.ItemDataRole.UserRole, c)
                it.setData(0, Qt.ItemDataRole.UserRole + 1, c.path.parent)
                it.setToolTip(0, str(c.path))
                it.setForeground(2, QColor(STATUS_COLORS.get(c.result.status, "#888")))
                if c.keep:
                    it.setForeground(1, QColor(OK_COLOR))
                if c.removable:
                    it.setText(1, (it.text(1) + " " if it.text(1) else "") + f"({c.removable})")
                else:
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(0, Qt.CheckState.Unchecked)
                top.addChild(it)
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self._count()

    def _rows(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for k in range(top.childCount()):
                it = top.child(k)
                yield self.groups[i], it.data(0, Qt.ItemDataRole.UserRole), it

    def _set_all(self, on: bool, only=lambda g, c: True):
        self.tree.blockSignals(True)
        for g, c, it in self._rows():
            if not c.removable:
                it.setCheckState(0, Qt.CheckState.Checked if on and only(g, c) else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._count()

    def _tick_spare(self):
        self._set_all(True, lambda g, c: g.kind == col.IDENTICAL and not c.keep)

    def ticked(self) -> list[tuple[col.Group, col.Copy]]:
        return [(g, c) for g, c, it in self._rows() if not c.removable and it.checkState(0) == Qt.CheckState.Checked]

    def _count(self):
        t = self.ticked()
        by = {k: sum(g.kind == k for g in self.all_groups) for k in self.kinds}
        self.summary.setText(" · ".join(f"{n} {k}" for k, n in by.items()) +
                             (f" · <b>{len(t)} ticked, {human(sum(c.result.size for _g, c in t))}</b>" if t else ""))
        self.b_trash.setEnabled(bool(t))

    def _trash(self):
        from ..compat import trash
        ticked = self.ticked()
        chosen = {id(c) for _g, c in ticked}
        full = [g.title for g in self.groups if all(id(c) in chosen for c in g.copies)]
        if full:
            QMessageBox.warning(self, "Duplicates", "Every copy of these games is ticked - leave at least one:\n\n" +
                                "\n".join(full[:15]))
            return
        # everything must still be what was scanned: the copies that go, and one that stays, as a file of its own
        for g, c in ticked:
            wrong = c.problem()
            if wrong:
                QMessageBox.warning(self, "Duplicates", f"{c.path.name} {wrong}. Scan again first.")
                return
        for g in {id(g): g for g, _c in ticked}.values():
            going = {col._identity(f) for c in g.copies if id(c) in chosen for f in c.files}
            if not any(id(c) not in chosen and not c.problem() and col._identity(c.path) not in going for c in g.copies):
                QMessageBox.warning(self, "Duplicates", f"The copy of “{g.title}” that should stay isn't there any more, or "
                                                        f"has changed. Scan again first.")
                return
        files = [f for _g, c in ticked for f in c.files if f.exists()]
        lists = {m for f in files for m in _playlists_with(f)}
        box = QMessageBox(QMessageBox.Icon.Question, "Move to trash",
                          f"Move {len(ticked)} copies ({len(files)} files, "
                          f"{human(sum(c.result.size for _g, c in ticked))}) to the trash?", parent=self)
        notes = []
        n = sum(g.kind != col.IDENTICAL for g, _c in ticked)
        if n:
            notes.append(f"⚠ {n} of them aren't identical to the copy that stays.")
        if lists:
            notes.append("⚠ These playlists name some of them and won't work afterwards: " +
                         ", ".join(sorted(p.name for p in lists)[:6]))
        notes.append("They can be restored from the trash.")
        box.setInformativeText("\n".join(notes))
        box.setDetailedText("\n".join(map(str, files)))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        bad = []
        for _g, c in ticked:
            left = [f for f in c.files if f.exists() and not trash(f)]
            if left:
                bad += left
            else:
                self.trashed.append(c.path)
        self._fill()
        if bad:
            QMessageBox.warning(self, "Duplicates", "These couldn't be moved to the trash and are still there:\n\n" +
                                "\n".join(map(str, bad[:20])))


def _playlists_with(path: Path) -> list[Path]:
    out = []
    try:
        for m in path.parent.glob("*.m3u"):
            lines = textfile.read_text(m).splitlines()
            if any((m.parent / ln.strip()) == path for ln in lines if ln.strip() and not ln.startswith("#")):
                out.append(m)
    except OSError:
        pass
    return out


class MissingDiscsDialog(QDialog):
    def __init__(self, missing: list[col.MissingDiscs], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Missing discs")
        self.resize(900, 460)
        intro = QLabel("Multi-disc games of which only some discs are in ROM Verifier's list. "
                       "<i>Maybe</i>: no DAT lists this game, there's just a gap in the disc numbers.")
        intro.setWordWrap(True)
        self.tree = _tree(["Game", "System", "You have", "Missing", "Sure?", "The DAT calls them"], [320, 80, 90, 90, 70, 220])
        _folder_menu(self.tree)
        for m in missing:
            it = QTreeWidgetItem([m.game, m.system, "Disc " + ", ".join(m.have), "Disc " + ", ".join(m.missing),
                                  "yes" if m.sure else "maybe", "; ".join(m.names)])
            it.setForeground(3, QColor(BAD_COLOR if m.sure else WARN_COLOR))
            it.setToolTip(5, "\n".join(m.names))
            it.setData(0, Qt.ItemDataRole.UserRole + 1, m.folder)
            self.tree.addTopLevelItem(it)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.tree, 1)
        lay.addWidget(bb)


class BiosDialog(QDialog):
    """Compares the BIOS files in some folders with the libretro database's list."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BIOS files")
        self.resize(1000, 640)
        self.cfg = get_config()
        self.worker: Worker | None = None
        root = Path(self.cfg["library_root"]).expanduser() if self.cfg["library_root"] else None
        saved = self.cfg["window_state"].get("bios_folders")
        folders = [Path(f) for f in saved] if saved is not None else bi.usual_folders(root)
        intro = QLabel("Compares the BIOS / firmware files in these folders with the hashes in the libretro database (the "
                       "files RetroArch's cores use). Only looks: nothing is downloaded but the list, nothing is renamed "
                       "or changed.")
        intro.setWordWrap(True)
        self.folders = QListWidget()
        self.folders.setMaximumHeight(96)
        for f in folders:
            self.folders.addItem(str(f))
        b_add = QPushButton(QIcon.fromTheme("list-add"), "Add folder…")
        b_add.clicked.connect(self._add)
        b_del = QPushButton(QIcon.fromTheme("list-remove"), "Remove")
        b_del.clicked.connect(self._remove)
        fb = QVBoxLayout()
        fb.addWidget(b_add)
        fb.addWidget(b_del)
        fb.addStretch(1)
        fr = QHBoxLayout()
        fr.addWidget(self.folders, 1)
        fr.addLayout(fb)
        self.all = QCheckBox("Also show systems you have no BIOS file for")
        self.all.toggled.connect(self._show)
        self.b_check = QPushButton(QIcon.fromTheme("media-playback-start"), "Check")
        self.b_check.clicked.connect(self.check)
        cr = QHBoxLayout()
        cr.addWidget(self.b_check)
        cr.addWidget(self.all)
        cr.addStretch(1)
        self.tree = _tree(["System / file", "Result", "Expected name", "Where"], [340, 150, 190, 300])
        _folder_menu(self.tree)
        self.status = QLabel("Press Check.")
        self.status.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(fr)
        lay.addLayout(cr)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.status)
        lay.addWidget(bb)
        self.reports: list[bi.SystemReport] = []

    def paths(self) -> list[Path]:
        return [Path(self.folders.item(i).text()) for i in range(self.folders.count())]

    def _save(self):
        self.cfg["window_state"]["bios_folders"] = [str(p) for p in self.paths()]
        self.cfg.save()

    def _add(self):
        d = QFileDialog.getExistingDirectory(self, "Folder with BIOS files", str(Path.home()))
        if d and Path(d) not in self.paths():
            self.folders.addItem(d)
            self._save()

    def _remove(self):
        for it in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(it))
        self._save()

    def check(self):
        if self.worker and self.worker.isRunning():
            return
        paths = self.paths()
        if not paths:
            QMessageBox.information(self, "BIOS files", "Add a folder with BIOS files first.")
            return
        age = bi.list_age_days()
        get = age is None
        if get:
            box = QMessageBox(QMessageBox.Icon.Question, "BIOS list",
                              "The list of known BIOS hashes has to be downloaded first (about 80 KB, from the libretro "
                              "database on GitHub). Download it now?",
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        elif age > 90 and QMessageBox.question(self, "BIOS list", f"The list of known hashes is {age} days old. Get the "
                                               f"current one?") == QMessageBox.StandardButton.Yes:
            get = True
        self.b_check.setEnabled(False)
        self.status.setText("Checking…")

        def job(w: Worker):
            if get:
                w.message.emit("Downloading the list…")
                bi.download()
            return bi.check(paths, bi.known(), w.message.emit, w.cancel)

        def done(reports):
            self.b_check.setEnabled(True)
            self.reports = reports
            self._show()

        self.worker = Worker(job, parent=self)
        self.worker.message.connect(self.status.setText)
        self.worker.done.connect(done)
        self.worker.failed.connect(lambda e: (self.b_check.setEnabled(True), self.status.setText("Failed"),
                                              QMessageBox.critical(self, "BIOS files", e)))
        self.worker.start()

    def _show(self):
        self.tree.clear()
        colors = {bi.GOOD: OK_COLOR, bi.MISNAMED: WARN_COLOR, bi.WRONG: BAD_COLOR}
        n = {k: 0 for k in colors}
        for r in self.reports:
            for f in r.found:
                n[f.state] += 1
            if not r.found and not self.all.isChecked():
                continue
            good = len({(f.bios.name, f.bios.md5) for f in r.good})
            top = QTreeWidgetItem([r.system, f"{good} good" if good else "none" if not r.found else "none good",
                                   f"{len(r.known)} known", ""])
            fnt = top.font(0)
            fnt.setBold(True)
            top.setFont(0, fnt)
            top.setForeground(1, QColor(OK_COLOR if good else BAD_COLOR if r.found else "#888"))
            top.setToolTip(0, "Known files:\n" + "\n".join(b.name for b in r.known[:60]))
            self.tree.addTopLevelItem(top)
            for f in sorted(r.found, key=lambda f: f.path.name.lower()):
                it = QTreeWidgetItem([f.path.name, f.state, f.bios.name, str(f.path.parent)])
                it.setForeground(1, QColor(colors[f.state]))
                it.setData(0, Qt.ItemDataRole.UserRole + 1, f.path.parent)
                if f.state == bi.WRONG:
                    it.setToolTip(1, "This name is known, but not with this data: a bad dump, a different revision or a "
                                     "modified file")
                top.addChild(it)
            top.setExpanded(bool(r.found))
        if self.reports:
            self.status.setText(f"{n[bi.GOOD]} good, {n[bi.MISNAMED]} good under another name, {n[bi.WRONG]} with unknown "
                                f"data. A core usually needs only one file of those listed for its system; files the list "
                                f"doesn't know aren't shown.")

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set()
            self.worker.wait(5000)
        super().reject()
