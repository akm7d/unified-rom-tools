"""DAT manager: map each library folder to a system and download / import its DAT."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QProgressBar, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..config import DAT_DIR, get_config
from ..dat import quick_count
from ..datmanager import dat_filename, download, import_file, library_folders, system_for
from ..systems import BY_ID, CATALOG, UNVERIFIABLE_HINT
from .common import Worker

COL_FOLDER, COL_SYSTEM, COL_DAT, COL_INFO = range(4)


class DatDialog(QDialog):
    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage DATs")
        self.resize(1050, 560)
        self.root = root
        self.cfg = get_config()
        self.worker: Worker | None = None

        intro = QLabel("Pick the system for each folder, then download its DAT. Disc systems use official "
                       "<b>Redump</b> DATs; cartridge systems use <b>No-Intro</b> DATs from the libretro mirror. "
                       "You can also import any Logiqx/ClrMamePro DAT (e.g. a Dat-o-Matic pack, or a "
                       "translations DAT) - several DATs per folder are merged.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Folder", "System", "DAT file(s)", "Games"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_FOLDER, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_SYSTEM, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_DAT, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_INFO, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(COL_SYSTEM, 330)

        self.btn_dl = QPushButton(QIcon.fromTheme("download"), "Download selected")
        self.btn_dl_all = QPushButton(QIcon.fromTheme("folder-download"), "Download all missing")
        self.btn_update = QPushButton(QIcon.fromTheme("view-refresh"), "Update all")
        self.btn_import = QPushButton(QIcon.fromTheme("document-import"), "Add DAT file…")
        self.btn_clear = QPushButton(QIcon.fromTheme("edit-clear"), "Remove DATs")
        self.btn_dl.clicked.connect(lambda: self._download(self._selected_folders()))
        self.btn_dl_all.clicked.connect(lambda: self._download(self._missing()))
        self.btn_update.clicked.connect(lambda: self._download(self._mapped()))
        self.btn_import.clicked.connect(self._import)
        self.btn_clear.clicked.connect(self._clear)
        row = QHBoxLayout()
        for b in (self.btn_dl, self.btn_dl_all, self.btn_update, self.btn_import, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel(f"DATs are stored in {DAT_DIR}")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.table, 1)
        lay.addLayout(row)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        lay.addWidget(bb)
        self._fill()

    # ------------------------------------------------------------------
    def _fill(self):
        folders = library_folders(self.root)
        self.table.setRowCount(len(folders))
        for r, f in enumerate(folders):
            s = self.cfg.system(f)
            self.table.setItem(r, COL_FOLDER, QTableWidgetItem(f))
            combo = QComboBox()
            combo.addItem("— none —", "")
            for sysdef in CATALOG:
                combo.addItem(sysdef.label, sysdef.id)
            idx = combo.findData(s["system"])
            combo.setCurrentIndex(max(idx, 0))
            combo.currentIndexChanged.connect(lambda _i, f=f, c=combo: self._set_system(f, c.currentData()))
            self.table.setCellWidget(r, COL_SYSTEM, combo)
            self._refresh_row(r, f)

    def _refresh_row(self, r: int, f: str):
        s = self.cfg.system(f)
        sysdef = system_for(self.cfg, f)
        self.table.removeCellWidget(r, COL_INFO)
        if sysdef and sysdef.source == "package":
            it = QTableWidgetItem("No DAT needed: each package is identified from its own data (title, ID, "
                                  "Game / Update / DLC, version, region)")
            it.setToolTip(UNVERIFIABLE_HINT.get(f, "") or it.text())
            self.table.setItem(r, COL_DAT, it)
            box = QCheckBox("Read package data")
            box.setToolTip("On: ROM Verifier identifies PS4 PKG / Switch NSP / XCI files from their own data.\n"
                           "Off: they're listed without being read (shown as No DAT).")
            box.setChecked(s.get("read_packages", True) is not False)
            box.toggled.connect(lambda on, f=f: self._set_read_packages(f, on))
            self.table.setItem(r, COL_INFO, QTableWidgetItem())
            self.table.setCellWidget(r, COL_INFO, box)
            return
        dats = [d for d in s["dats"] if Path(d).is_file()]
        text = "; ".join(Path(d).name for d in dats) if dats else UNVERIFIABLE_HINT.get(f, "— missing —")
        it = QTableWidgetItem(text)
        it.setToolTip("\n".join(dats) or text)
        self.table.setItem(r, COL_DAT, it)
        games = ""
        if dats:
            try:
                games = f"{sum(quick_count(d) for d in dats):,}"
            except Exception as exc:  # noqa: BLE001
                games = f"error: {exc}"
        self.table.setItem(r, COL_INFO, QTableWidgetItem(games))

    def _set_read_packages(self, folder: str, on: bool):
        self.cfg.system(folder)["read_packages"] = on
        self.cfg.save()

    def _row_of(self, folder: str) -> int:
        for r in range(self.table.rowCount()):
            if self.table.item(r, COL_FOLDER).text() == folder:
                return r
        return -1

    def _set_system(self, folder: str, sysid: str):
        s = self.cfg.system(folder)
        s["system"] = sysid
        if sysid:
            s.pop("_none", None)
        else:
            s["_none"] = True            # "— none —" chosen on purpose: don't map it automatically again
        sysdef = BY_ID.get(sysid)
        auto = [d for d in s["dats"] if Path(d).parent == DAT_DIR]
        manual = [d for d in s["dats"] if d not in auto]
        s["dats"] = manual
        if sysdef and (DAT_DIR / dat_filename(sysdef)).exists():
            s["dats"] = [str(DAT_DIR / dat_filename(sysdef))] + manual
        self.cfg.save()
        self._refresh_row(self._row_of(folder), folder)

    def _selected_folders(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, COL_FOLDER).text() for r in rows]

    def _mapped(self) -> list[str]:
        return [f for f in library_folders(self.root)
                if system_for(self.cfg, f) and system_for(self.cfg, f).source != "package"]

    def _missing(self) -> list[str]:
        return [f for f in self._mapped() if not any(Path(d).is_file() for d in self.cfg.system(f)["dats"])]

    # ------------------------------------------------------------------
    def _download(self, folders: list[str]):
        targets = {}
        for f in folders:
            sysdef = system_for(self.cfg, f)
            if sysdef and sysdef.source != "package":
                targets.setdefault(sysdef.id, []).append(f)
        if not targets:
            QMessageBox.information(self, "Download DATs", "Select folders that have a system with a DAT chosen "
                                    "(PS4 and Switch need none: they're read from the packages).")
            return
        self._busy(True, len(targets))

        def job(w: Worker):
            results = {}
            for i, sysid in enumerate(targets, 1):
                sysdef = BY_ID[sysid]
                w.progress.emit(i - 1, len(targets), f"Downloading {sysdef.name}…", 0)
                try:
                    results[sysid] = (str(download(sysdef)), None)
                except Exception as exc:  # noqa: BLE001
                    results[sysid] = (None, str(exc))
            return results

        self.worker = Worker(job, parent=self)
        self.worker.progress.connect(lambda d, t, msg, _f: (self.progress.setValue(d), self.status.setText(msg)))

        def finished(results):
            errs = []
            for sysid, (path, err) in results.items():
                if err:
                    errs.append(f"{BY_ID[sysid].name}: {err}")
                    continue
                for f in targets[sysid]:
                    s = self.cfg.system(f)
                    s["dats"] = [path] + [d for d in s["dats"] if d != path and Path(d).parent != DAT_DIR]
                    self._refresh_row(self._row_of(f), f)
            self.cfg.save()
            self._busy(False)
            self.status.setText(f"Downloaded {len(results) - len(errs)} DAT(s)." + (" Some failed." if errs else ""))
            if errs:
                QMessageBox.warning(self, "Some downloads failed", "\n".join(errs))

        self.worker.done.connect(finished)
        self.worker.failed.connect(lambda e: (self._busy(False), QMessageBox.critical(self, "Download", e)))
        self.worker.start()

    def _import(self):
        folders = self._selected_folders()
        if not folders:
            QMessageBox.information(self, "Add DAT", "Select the folder(s) the DAT belongs to first.")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Choose DAT files", str(Path.home()),
                                                "DAT files (*.dat *.xml *.zip);;All files (*)")
        for fp in files:
            try:
                dest = str(import_file(Path(fp)))
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Import failed", f"{Path(fp).name}: {exc}")
                continue
            for f in folders:
                s = self.cfg.system(f)
                if dest not in s["dats"]:
                    s["dats"].append(dest)
        self.cfg.save()
        for f in folders:
            self._refresh_row(self._row_of(f), f)

    def _clear(self):
        for f in self._selected_folders():
            self.cfg.system(f)["dats"] = []
            self._refresh_row(self._row_of(f), f)
        self.cfg.save()

    def _busy(self, on: bool, total: int = 0):
        for b in (self.btn_dl, self.btn_dl_all, self.btn_update, self.btn_import, self.btn_clear):
            b.setEnabled(not on)
        self.progress.setVisible(on)
        if on:
            self.progress.setRange(0, total)
            self.progress.setValue(0)

    def reject(self):
        if self.worker and self.worker.isRunning():
            return
        super().reject()
