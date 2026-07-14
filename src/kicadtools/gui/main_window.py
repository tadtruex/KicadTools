"""The BOM Cockpit main window."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kicadtools import writer
from kicadtools.components import bom_components, get_components
from kicadtools.gui.lcsc import LcscTableModel, ResolveWorker
from kicadtools.gui.model import IntentTableModel, NaturalSortProxyModel
from kicadtools.resolve import iter_bom_part_records
from kicadtools.sexpr_parser import has_instantiated_sheets
from kicadtools.suppliers import DigiKeyClient

_LCSC_CACHE = Path.home() / ".kicadtools" / "lcsc_cache.sqlite"


class MainWindow(QMainWindow):
    def __init__(self, schematic: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("BOM Cockpit")
        self.resize(960, 640)

        self._board: Optional[Path] = None
        self._records: list = []
        self._resolve_thread: Optional[QThread] = None
        self._resolve_worker: Optional[ResolveWorker] = None

        tabs = QTabWidget()
        tabs.addTab(self._build_intent_tab(), "Intents")
        tabs.addTab(self._build_lcsc_tab(), "LCSC")
        self.setCentralWidget(tabs)

        self._build_menu()
        self.statusBar().showMessage("Open a schematic to begin.")

        if schematic:
            self._open_path(Path(schematic))

    # ---- tab construction -------------------------------------------
    def _make_table(self, model) -> QTableView:
        """A sortable, drag-resizable row-selection table over *model* (wrapped
        in a natural-sort proxy)."""
        proxy = NaturalSortProxyModel(self)
        proxy.setSourceModel(model)
        table = QTableView()
        table.setModel(proxy)
        table.setSelectionBehavior(QTableView.SelectRows)
        table.setSortingEnabled(True)  # click a header to sort
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)  # drag to resize
        header.setStretchLastSection(True)
        header.setSortIndicator(-1, Qt.AscendingOrder)  # natural order until clicked
        return table

    def _build_intent_tab(self) -> QWidget:
        self.model = IntentTableModel()
        # A proxy sits between the view and the model so header clicks can sort
        # without disturbing the source rows (the accept/check sets are keyed by
        # source row and stay valid).
        self.table = self._make_table(self.model)
        self.proxy = self.table.model()

        accept_checked = QPushButton("Accept Checked")
        accept_all = QPushButton("Accept All")
        accept_checked.clicked.connect(self._accept_checked)
        accept_all.clicked.connect(self._accept_all)

        buttons = QHBoxLayout()
        buttons.addWidget(accept_checked)
        buttons.addWidget(accept_all)
        buttons.addStretch(1)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        return tab

    def _build_lcsc_tab(self) -> QWidget:
        self.lcsc_model = LcscTableModel()
        self.lcsc_table = self._make_table(self.lcsc_model)

        self.resolve_btn = QPushButton("Resolve LCSC")
        self.resolve_btn.clicked.connect(self._resolve_lcsc)
        lcsc_accept_checked = QPushButton("Accept Checked")
        lcsc_accept_all = QPushButton("Accept All")
        lcsc_accept_checked.clicked.connect(self._lcsc_accept_checked)
        lcsc_accept_all.clicked.connect(self._lcsc_accept_all)

        buttons = QHBoxLayout()
        buttons.addWidget(self.resolve_btn)
        buttons.addWidget(lcsc_accept_checked)
        buttons.addWidget(lcsc_accept_all)
        buttons.addStretch(1)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self.lcsc_table)
        layout.addLayout(buttons)
        return tab

    # ---- menu --------------------------------------------------------
    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_act = file_menu.addAction("&Open Schematic…")
        open_act.setShortcut(QKeySequence.Open)
        open_act.triggered.connect(self._open_dialog)
        self._save_act = file_menu.addAction("&Save")
        self._save_act.setShortcut(QKeySequence.Save)
        self._save_act.triggered.connect(self._save)
        self._save_as_act = file_menu.addAction("Save &As…")
        self._save_as_act.setShortcut(QKeySequence.SaveAs)
        self._save_as_act.triggered.connect(self._save_as)
        file_menu.addSeparator()
        quit_act = file_menu.addAction("&Quit")
        quit_act.setShortcut(QKeySequence.Quit)
        # Keep Quit in the File menu: macOS otherwise auto-relocates a "Quit"
        # action to the application menu.
        quit_act.setMenuRole(QAction.NoRole)
        quit_act.triggered.connect(self.close)

    # ---- open --------------------------------------------------------
    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open KiCad schematic", "", "KiCad schematic (*.kicad_sch)"
        )
        if path:
            self._open_path(Path(path))

    def _open_path(self, path: Path) -> None:
        try:
            components = bom_components(get_components(path))
        except Exception as exc:  # noqa: BLE001 — surface any read error to the user
            QMessageBox.critical(self, "Open failed", f"Could not read {path.name}:\n{exc}")
            return
        self._records = iter_bom_part_records(path)
        self._board = path
        self.model.load(components)
        self.table.resizeColumnsToContents()  # sensible initial widths; still draggable
        self.lcsc_model.load(self._records)
        self.lcsc_table.resizeColumnsToContents()
        self.setWindowTitle(f"BOM Cockpit — {path.name}")
        self._refresh_status()

    # ---- LCSC resolution --------------------------------------------
    def _resolve_lcsc(self) -> None:
        if not self._records:
            QMessageBox.information(self, "No board", "Open a schematic first.")
            return
        if self._resolve_thread is not None:
            return  # a resolution is already in flight

        # Warn if any part relies on DigiKey but credentials are missing.
        from kicadtools.partnum import extract_part_numbers

        pns = [extract_part_numbers(r.props) for r in self._records]
        needs_dk = any(p.digikey_pn and not p.lcsc and not p.mpn for p in pns)
        if needs_dk and not DigiKeyClient().configured:
            QMessageBox.warning(
                self,
                "DigiKey not configured",
                "Some parts have only a DigiKey PN, but DIGIKEY_CLIENT_ID / "
                "DIGIKEY_CLIENT_SECRET are not set — those parts will be flagged.",
            )

        _LCSC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        self.resolve_btn.setEnabled(False)
        self.resolve_btn.setText("Resolving…")

        self._resolve_counts = (0, 0, 0)
        self._resolve_thread = QThread(self)
        self._resolve_worker = ResolveWorker(self._records, _LCSC_CACHE)
        self._resolve_worker.moveToThread(self._resolve_thread)
        self._resolve_thread.started.connect(self._resolve_worker.run)
        self._resolve_worker.progress.connect(self.statusBar().showMessage)
        self._resolve_worker.row_resolved.connect(self.lcsc_model.apply_resolution)
        # Stash the tallies, then end the thread's event loop; do the UI update
        # and object teardown only once the thread has actually stopped
        # (QThread.finished). Calling thread.wait() from the worker.finished slot
        # would deadlock, since quit() hasn't run yet at that point.
        self._resolve_worker.finished.connect(self._on_resolve_counts)
        self._resolve_thread.finished.connect(self._on_resolve_cleanup)
        self._resolve_thread.start()

    def _on_resolve_counts(self, had: int, resolved: int, flagged: int) -> None:
        self._resolve_counts = (had, resolved, flagged)
        if self._resolve_thread is not None:
            self._resolve_thread.quit()

    def _on_resolve_cleanup(self) -> None:
        had, resolved, flagged = self._resolve_counts
        total = had + resolved + flagged
        self.statusBar().showMessage(
            f"LCSC: {had + resolved}/{total} have a number "
            f"({had} already had, {resolved} resolved, {flagged} flagged)"
        )
        self.resolve_btn.setEnabled(True)
        self.resolve_btn.setText("Resolve LCSC")
        if self._resolve_worker is not None:
            self._resolve_worker.deleteLater()
        if self._resolve_thread is not None:
            self._resolve_thread.deleteLater()
        self._resolve_thread = None
        self._resolve_worker = None

    # ---- accept ------------------------------------------------------
    def _accept_checked(self) -> None:
        self.model.accept_checked()
        self._refresh_status()

    def _accept_all(self) -> None:
        self.model.accept_all()
        self._refresh_status()

    def _lcsc_accept_checked(self) -> None:
        self.lcsc_model.accept_checked()

    def _lcsc_accept_all(self) -> None:
        self.lcsc_model.accept_all()

    # ---- write -------------------------------------------------------
    def _merged_edits(self) -> "dict[Path, dict[str, dict[str, str]]]":
        """Merge accepted edits from both tabs into one
        ``{file: {uuid: {key: value}}}`` plan. The Intents tab contributes an
        ``Intent:Type`` per accepted guess; the LCSC tab contributes resolved
        ``LCSC`` / ``MPN`` values. A symbol touched by both tabs gets both."""
        merged: dict[Path, dict[str, dict[str, str]]] = defaultdict(dict)
        for src, uuid_to_type in self.model.accepted_edits().items():
            for uuid, intent_type in uuid_to_type.items():
                merged[src].setdefault(uuid, {})["Intent:Type"] = intent_type
        for src, uuid_to_props in self.lcsc_model.accepted_edits().items():
            for uuid, props in uuid_to_props.items():
                merged[src].setdefault(uuid, {}).update(props)
        return dict(merged)

    def _pending_edits(self) -> "Optional[dict[Path, dict[str, dict[str, str]]]]":
        """The merged accepted edits, or ``None`` (with a nudge) if there's
        nothing to save yet."""
        edits = self._merged_edits()
        if not edits:
            QMessageBox.information(
                self,
                "Nothing to save",
                "Accept one or more guesses (Intents) or resolved LCSC numbers first.",
            )
            return None
        return edits

    @staticmethod
    def _change_count(edits: "dict[Path, dict[str, dict[str, str]]]") -> int:
        # Count individual properties, not just symbols — a symbol may carry both
        # an Intent:Type and an LCSC.
        return sum(len(props) for m in edits.values() for props in m.values())

    def _save(self) -> None:
        """Write accepted edits back into the open schematic, in place."""
        edits = self._pending_edits()
        if edits is None:
            return
        n = self._change_count(edits)
        noun = "change" if n == 1 else "changes"
        resp = QMessageBox.question(
            self,
            "Save",
            f"Save {n} {noun}?",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if resp != QMessageBox.Save:
            return
        try:
            for src, uuid_to_props in edits.items():
                writer.write_properties(src, uuid_to_props)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        if self._board:
            self._open_path(self._board)  # reload → accepted rows now read as ✅
        self.statusBar().showMessage(f"Saved {n} {noun}.")

    def _save_as(self) -> None:
        """Write accepted edits to a schematic under a new name.

        Only single-file schematics are supported for now: a hierarchical design
        would leave its subsheet links pointing at the old files, and we don't
        relink them yet — so we refuse and steer the user to Save (in place)."""
        edits = self._pending_edits()
        if edits is None:
            return
        if self._board and has_instantiated_sheets(self._board):
            QMessageBox.warning(
                self,
                "Save As unavailable",
                "This schematic instantiates hierarchical sheets, which Save As "
                "can't relink yet — the saved copy's sheets would still point at "
                "the original files.\n\nUse File ▸ Save to write the changes in "
                "place instead.",
            )
            return

        start = str(self._board) if self._board else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save schematic as", start, "KiCad schematic (*.kicad_sch)"
        )
        if not path:
            return
        dest = Path(path)
        if dest.suffix != ".kicad_sch":
            dest = dest.with_suffix(".kicad_sch")

        # A single-file schematic contributes exactly one source file.
        (src, uuid_to_props), = edits.items()
        n = self._change_count(edits)
        try:
            writer.write_properties(src, uuid_to_props, dest=lambda _p: dest)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save As failed", str(exc))
            return
        self._open_path(dest)  # follow the new file; accepted rows re-read as ✅
        self.statusBar().showMessage(f"Saved {n} change{'s' * (n != 1)} to {dest.name}.")

    # ---- status bar --------------------------------------------------
    def _refresh_status(self) -> None:
        s = self.model.summary()
        self.statusBar().showMessage(
            f"{sum(s.values())} components — {s['typed']} typed, "
            f"{s['guessed']} guessed, {s['accepted']} accepted, "
            f"{s['unclassified']} unclassified"
        )
