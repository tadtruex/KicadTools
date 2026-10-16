"""The BOM Cockpit main window."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kicadtools import writer
from kicadtools.components import bom_components, get_components
from kicadtools.jlcpcb import (
    build_jlcpcb_bom,
    missing_lcsc,
    undetermined_references,
    write_jlcpcb_csv,
)
from kicadtools.digikey_bom import (
    BOTH,
    NON_SMD,
    SMD_ONLY,
    build_digikey_bom,
    unorderable_lines,
    write_digikey_csv,
)
from kicadtools.cpl import cpl_from_schematic, write_cpl_csv
from kicadtools.pcb import read_mounting_for_schematic, sibling_pcb
from kicadtools.gui.lcsc import (
    LINK_ROLE,
    LcscTableModel,
    ResolveWorker,
    RotationDelegate,
    StockWorker,
    SubstituteWorker,
    _COL_ROTATION,
)
from kicadtools.gui.model import IntentTableModel, NaturalSortProxyModel
from kicadtools.gui.param_editor import ElectricalParamsDialog
from kicadtools.gui.substitute_dialog import SubstituteDialog, describe_part
from kicadtools.naturalsort import natural_key
from kicadtools.partrotation import PartRotationStore
from kicadtools.resolve import iter_bom_part_records
from kicadtools.sexpr_parser import has_instantiated_sheets
from kicadtools.substitute import (
    DEFAULT_BUILD_QTY,
    STOCK_LOW,
    STOCK_OUT,
    SUPPORTED_TYPES,
)
from kicadtools.suppliers import DigiKeyClient, _norm_mpn, preferred_digikey_pn

_LCSC_CACHE = Path.home() / ".kicadtools" / "lcsc_cache.sqlite"


class MainWindow(QMainWindow):
    def __init__(self, schematic: Optional[str] = None):
        super().__init__()
        # The ``[*]`` is Qt's window-modified placeholder: it renders a "*"
        # (or the platform's document-dirty marker) only while there are
        # unsaved edits — see ``setWindowModified`` in the accept/save paths.
        self.setWindowTitle("BOM Cockpit[*]")
        self.resize(960, 640)

        self._board: Optional[Path] = None
        self._records: list = []
        self._resolve_thread: Optional[QThread] = None
        self._resolve_worker: Optional[ResolveWorker] = None
        # (thread, worker) pairs for the stock and substitute lookups.
        self._threads: list = []

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
        # Right-click a row to edit its electrical-intent parameters.
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._intent_context_menu)

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
        # A disk-backed rotation store shared with the CPL export, so a dropdown
        # choice here persists (keyed by LCSC part) and reappears next session.
        self._rotation_store = PartRotationStore(_LCSC_CACHE)
        self.lcsc_model = LcscTableModel(store=self._rotation_store)
        self.lcsc_table = self._make_table(self.lcsc_model)
        self.lcsc_table.setItemDelegateForColumn(_COL_ROTATION, RotationDelegate(self))
        # LCSC / DigiKey cells are links: open the supplier page on click, and
        # show a pointing-hand cursor while hovering one (needs mouse tracking).
        self.lcsc_table.setMouseTracking(True)
        self.lcsc_table.clicked.connect(self._open_link)
        self.lcsc_table.entered.connect(self._hover_link)
        # Right-click a row to search for an in-stock substitute.
        self.lcsc_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lcsc_table.customContextMenuRequested.connect(self._lcsc_context_menu)

        self.resolve_btn = QPushButton("Resolve LCSC")
        self.resolve_btn.clicked.connect(self._resolve_lcsc)
        self.stock_btn = QPushButton("Check Stock")
        self.stock_btn.clicked.connect(self._check_stock)
        lcsc_accept_checked = QPushButton("Accept Checked")
        lcsc_accept_all = QPushButton("Accept All")
        lcsc_accept_checked.clicked.connect(self._lcsc_accept_checked)
        lcsc_accept_all.clicked.connect(self._lcsc_accept_all)

        self.build_qty = QSpinBox()
        self.build_qty.setRange(1, 1_000_000)
        self.build_qty.setValue(DEFAULT_BUILD_QTY)
        self.build_qty.setToolTip("Boards in the build; stock is checked against this.")
        self.build_qty.valueChanged.connect(self.lcsc_model.set_build_qty)

        buttons = QHBoxLayout()
        buttons.addWidget(self.resolve_btn)
        buttons.addWidget(self.stock_btn)
        buttons.addWidget(lcsc_accept_checked)
        buttons.addWidget(lcsc_accept_all)
        buttons.addStretch(1)
        buttons.addWidget(QLabel("Boards:"))
        buttons.addWidget(self.build_qty)

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
        self._export_jlc_act = file_menu.addAction("Export &JLCPCB BOM…")
        self._export_jlc_act.triggered.connect(self._export_jlcpcb)

        self._export_dk_act = file_menu.addAction("Export &DigiKey BOM…")
        self._export_dk_act.triggered.connect(self._export_digikey)

        self._export_cpl_act = file_menu.addAction("Export JLCPCB &CPL…")
        self._export_cpl_act.triggered.connect(self._export_cpl)
        file_menu.addSeparator()
        quit_act = file_menu.addAction("&Quit")
        quit_act.setShortcut(QKeySequence.Quit)
        # Keep Quit in the File menu: macOS otherwise auto-relocates a "Quit"
        # action to the application menu.
        quit_act.setMenuRole(QAction.NoRole)
        quit_act.triggered.connect(self.close)

    # ---- open --------------------------------------------------------
    def _open_dialog(self) -> None:
        if self._refuse_if_busy():
            return
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
        self.setWindowTitle(f"BOM Cockpit — {path.name}[*]")
        self.setWindowModified(False)  # a fresh load matches disk → clean
        self._refresh_status()

    # ---- intent parameter editing -----------------------------------
    def _intent_context_menu(self, pos) -> None:
        """Show the Intents-tab row context menu at *pos* (viewport coords)."""
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        src = self.proxy.mapToSource(index)
        intent = self.model.component_at(src.row())
        menu = QMenu(self)
        edit_act = menu.addAction("Edit Electrical Parameters")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is edit_act:
            self._edit_params(intent)

    def _edit_params(self, intent) -> None:
        """Pop the parameter editor for *intent*; write accepted edits straight
        into the schematic (which reloads the board so the row reflects them)."""
        dialog = ElectricalParamsDialog(intent, self)
        if dialog.exec() != QDialog.Accepted:
            return
        edits = dialog.edits()
        if not edits:
            return
        if not (intent.source_file and intent.symbol_uuid):
            QMessageBox.warning(
                self,
                "Can't write",
                f"{intent.reference} has no source location on file to write to.",
            )
            return
        if self._refuse_if_busy():
            return
        plan = {Path(intent.source_file): {intent.symbol_uuid: edits}}
        # Writing reloads the board, which would drop any accepted-but-unsaved
        # edits — so offer to save them in the same write instead.
        pending = self._merged_edits()
        if pending:
            n = self._change_count(pending)
            resp = QMessageBox.question(
                self,
                "Save other changes too?",
                "Writing these parameters saves the schematic and reloads it. "
                f"Your {n} other unsaved change{'s' * (n != 1)} will be saved "
                "with them.",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if resp != QMessageBox.Save:
                return
            for src, by_uuid in plan.items():
                for uuid, props in by_uuid.items():
                    pending.setdefault(src, {}).setdefault(uuid, {}).update(props)
            plan = pending
        self._write_in_place(plan)

    # ---- LCSC / DigiKey links ---------------------------------------
    def _open_link(self, index) -> None:
        """Open the supplier page for a clicked LCSC/DigiKey cell (no-op for a
        cell that isn't a link)."""
        url = index.data(LINK_ROLE)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _hover_link(self, index) -> None:
        """Show a pointing-hand cursor over link cells, an arrow elsewhere."""
        cursor = Qt.PointingHandCursor if index.data(LINK_ROLE) else Qt.ArrowCursor
        self.lcsc_table.viewport().setCursor(cursor)

    # ---- LCSC resolution --------------------------------------------
    # ---- background work --------------------------------------------
    def _busy(self) -> bool:
        return self._resolve_thread is not None or bool(self._threads)

    def _refuse_if_busy(self) -> bool:
        """Tell the user to wait (and return True) while a lookup runs. Workers
        report results by row index, so the board must not reload under them."""
        if not self._busy():
            return False
        QMessageBox.information(
            self, "Lookup running", "Wait for the current JLCPCB / DigiKey lookup to finish."
        )
        return True

    def _start(self, worker) -> QThread:
        """Run *worker* (a QObject with ``run()`` that quits its own thread when
        done) on a new thread, tracked in ``_threads`` until it finishes."""
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        thread.finished.connect(self._on_thread_finished)
        self._threads.append((thread, worker))
        thread.start()
        return thread

    def _on_thread_finished(self) -> None:
        thread = self.sender()
        for t, w in [pair for pair in self._threads if pair[0] is thread]:
            w.deleteLater()
            t.deleteLater()
        self._threads = [pair for pair in self._threads if pair[0] is not thread]

    def _stop_threads(self) -> None:
        """Ask every lookup to stop and wait for it (the window is closing)."""
        threads = [t for t, _ in self._threads]
        if self._resolve_thread is not None:
            threads.append(self._resolve_thread)
        for t in threads:
            t.requestInterruption()
        if self._resolve_thread is not None:
            self._resolve_thread.quit()
        for t in threads:
            t.wait(30_000)

    # ---- stock -------------------------------------------------------
    def _check_stock(self) -> None:
        if not self._records:
            QMessageBox.information(self, "No board", "Open a schematic first.")
            return
        if self._refuse_if_busy():
            return
        codes = self.lcsc_model.lcsc_codes()
        if not codes:
            QMessageBox.information(
                self, "No LCSC numbers", "No part has an LCSC number yet. Run Resolve LCSC first."
            )
            return
        _LCSC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        self.stock_btn.setEnabled(False)
        self.stock_btn.setText("Checking…")
        worker = StockWorker(codes, _LCSC_CACHE)
        worker.progress.connect(self.statusBar().showMessage)
        worker.part_checked.connect(self.lcsc_model.apply_stock)
        worker.finished.connect(self._on_stock_done)
        self._start(worker)

    def _on_stock_done(self, _found: int, _missing: int) -> None:
        self.stock_btn.setEnabled(True)
        self.stock_btn.setText("Check Stock")
        levels = [self.lcsc_model.stock_level(r) for r in range(self.lcsc_model.rowCount())]
        out, low = levels.count(STOCK_OUT), levels.count(STOCK_LOW)
        qty = self.lcsc_model.build_qty
        if not out and not low:
            self.statusBar().showMessage(f"Stock: every part covers {qty} boards.")
        else:
            self.statusBar().showMessage(
                f"Stock: {out} part(s) unavailable (red), {low} short for {qty} boards "
                "(amber). Right-click one to find a substitute."
            )

    # ---- substitutes -------------------------------------------------
    def _lcsc_context_menu(self, pos) -> None:
        index = self.lcsc_table.indexAt(pos)
        if not index.isValid():
            return
        row = self.lcsc_table.model().mapToSource(index).row()
        lrow = self.lcsc_model.rows()[row]
        intent_type = (lrow.intent.intent_type if lrow.intent else "").lower()
        menu = QMenu(self)
        act = menu.addAction("Find Parametrically Equivalent Substitute…")
        if intent_type not in SUPPORTED_TYPES:
            act.setText("Find Parametrically Equivalent Substitute… (resistors and capacitors only)")
            act.setEnabled(False)
        chosen = menu.exec(self.lcsc_table.viewport().mapToGlobal(pos))
        if chosen is act:
            self._find_substitute(row)

    def _find_substitute(self, row: int) -> None:
        if self._refuse_if_busy():
            return
        rows = self.lcsc_model.rows_like(row)
        lrows = self.lcsc_model.rows()
        base = lrows[row]
        refs = sorted((lrows[i].reference for i in rows), key=natural_key)
        needed = len(rows) * self.lcsc_model.build_qty

        current_code = base.replaced_lcsc or base.lcsc
        current_part = self.lcsc_model.part_for(current_code)
        if current_part is not None:
            current = describe_part(current_part)
        else:
            current = current_code or "no LCSC number"
            if base.mpn:
                current += f", MPN {base.mpn}"

        _LCSC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        dialog = SubstituteDialog(refs, current, needed, self)
        worker = SubstituteWorker(row=base, needed=needed, cache_path=_LCSC_CACHE)
        worker.found.connect(dialog.show_result)
        worker.failed.connect(dialog.show_error)
        self._start(worker)
        accepted = dialog.exec() == QDialog.Accepted
        cand = dialog.chosen() if accepted else None
        try:  # a search still running must not report into a closed dialog
            worker.found.disconnect(dialog.show_result)
            worker.failed.disconnect(dialog.show_error)
        except (RuntimeError, TypeError):
            pass  # the worker already finished and was deleted
        dialog.deleteLater()
        if cand is None:
            return

        digikey_pn, why = self._digikey_pn_for(cand.part.mpn)
        if not digikey_pn:
            resp = QMessageBox.question(
                self,
                "No DigiKey part number",
                f"{why}\n\nUse {cand.part.lcsc} anyway? The LCSC number changes, "
                "but Alternates isn't updated.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
        self.lcsc_model.apply_substitute(rows, cand.part, digikey_pn)
        self._mark_dirty()
        alt = f"; Alternates += {digikey_pn}" if digikey_pn else ""
        self.statusBar().showMessage(
            f"{', '.join(refs)} → {cand.part.lcsc} ({cand.part.mpn}){alt}. Save to write it."
        )

    def _digikey_pn_for(self, mpn: str) -> "tuple[Optional[str], str]":
        """The cut-tape DigiKey PN for *mpn*, or ``(None, reason)``."""
        dk = DigiKeyClient()
        if not dk.configured:
            return None, (
                "DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET are not set, so the "
                f"DigiKey part number for {mpn} can't be looked up."
            )
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            product = dk.product_for(mpn)
        finally:
            QApplication.restoreOverrideCursor()
        if product is None or _norm_mpn(product.mpn or "") != _norm_mpn(mpn):
            return None, f"DigiKey has no exact match for MPN {mpn}."
        pn = preferred_digikey_pn(product.digikey_pns)
        if not pn:
            return None, f"DigiKey lists {mpn} but gives no part number for it."
        return pn, ""

    def _resolve_lcsc(self) -> None:
        if not self._records:
            QMessageBox.information(self, "No board", "Open a schematic first.")
            return
        if self._refuse_if_busy():
            return

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
    def _mark_dirty(self) -> None:
        """Flag unsaved edits — drives the title's ``*`` and the quit prompt."""
        self.setWindowModified(True)

    def _accept_checked(self) -> None:
        self.model.accept_checked()
        self._mark_dirty()
        self._refresh_status()

    def _accept_all(self) -> None:
        self.model.accept_all()
        self._mark_dirty()
        self._refresh_status()

    def _lcsc_accept_checked(self) -> None:
        self.lcsc_model.accept_checked()
        self._mark_dirty()

    def _lcsc_accept_all(self) -> None:
        self.lcsc_model.accept_all()
        self._mark_dirty()

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

    def _write_in_place(self, edits: "dict[Path, dict[str, dict[str, str]]]") -> bool:
        """Write *edits* into their source files in place, reload, and clear the
        dirty flag (the reload does). Returns ``True`` on success, ``False`` if a
        write failed (already reported to the user). No confirmation — callers own
        that."""
        n = self._change_count(edits)
        try:
            for src, uuid_to_props in edits.items():
                writer.write_properties(src, uuid_to_props)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return False
        if self._board:
            self._open_path(self._board)  # reload → accepted rows now read as ✅
        self.statusBar().showMessage(f"Saved {n} change{'s' * (n != 1)}.")
        return True

    def _save(self) -> bool:
        """Confirm, then write accepted edits back into the open schematic in
        place. Returns whether the save actually happened (Cancel → ``False``)."""
        if self._refuse_if_busy():
            return False
        edits = self._pending_edits()
        if edits is None:
            return False
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
            return False
        return self._write_in_place(edits)

    def _save_as(self) -> None:
        """Write accepted edits to a schematic under a new name.

        Only single-file schematics are supported for now: a hierarchical design
        would leave its subsheet links pointing at the old files, and we don't
        relink them yet — so we refuse and steer the user to Save (in place)."""
        if self._refuse_if_busy():
            return
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

    # ---- export ------------------------------------------------------
    def _export_jlcpcb(self) -> None:
        """Export the BOM as a JLCPCB-format CSV. Read-only: never touches the
        schematic or the dirty flag. LCSC numbers resolved in this session (even
        if not yet saved) are preferred over what's on disk."""
        if not self._records:
            QMessageBox.information(self, "No board", "Open a schematic first.")
            return
        # Offer an SMD-only BOM (mounting read from the sibling .kicad_pcb).
        resp = QMessageBox.question(
            self,
            "Export JLCPCB BOM",
            "Include surface-mount (SMD) parts only?\n\n"
            "Yes: SMD parts only (needs the board's .kicad_pcb; parts of "
            "undetermined mounting are excluded).\nNo: all BOM parts.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.No,
        )
        if resp == QMessageBox.Cancel:
            return
        smd_only = resp == QMessageBox.Yes

        # Prefer session-resolved LCSC numbers from the LCSC tab.
        override = {row.reference: row.lcsc for row in self.lcsc_model.rows() if row.lcsc}
        mounting: dict = {}
        if smd_only and self._board:
            mounting, _pcb = read_mounting_for_schematic(self._board)
        lines = build_jlcpcb_bom(
            self._records, override, mounting=mounting, smd_only=smd_only
        )

        start = str(Path(self._board).with_suffix(".csv")) if self._board else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export JLCPCB BOM", start, "CSV (*.csv)"
        )
        if not path:
            return
        dest = Path(path)
        if dest.suffix != ".csv":
            dest = dest.with_suffix(".csv")
        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                write_jlcpcb_csv(lines, f)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        miss = missing_lcsc(lines)
        msg = f"Exported {len(lines)} line(s) to {dest.name}."
        if smd_only:
            undet = undetermined_references(self._records, mounting)
            if undet:
                msg += f" {len(undet)} part(s) of undetermined mounting excluded."
        if miss:
            msg += f" {len(miss)} lack an LCSC Part #."
        self.statusBar().showMessage(msg)

    def _export_digikey(self) -> None:
        """Export the BOM as a DigiKey cart/BOM-format CSV. Read-only: never
        touches the schematic or the dirty flag. DigiKey and manufacturer part
        numbers come straight from the schematic (no lookups)."""
        if not self._records:
            QMessageBox.information(self, "No board", "Open a schematic first.")
            return

        # Three-way mounting filter (mounting read from the sibling .kicad_pcb).
        # Ordered labels; the picked index maps to a filter mode.
        modes = [
            ("All parts", BOTH),
            ("Surface-mount (SMD) only", SMD_ONLY),
            ("Non-surface-mount only (through-hole + undetermined)", NON_SMD),
        ]
        label, ok = QInputDialog.getItem(
            self,
            "Export DigiKey BOM",
            "Which parts to include?",
            [m[0] for m in modes],
            0,
            editable=False,
        )
        if not ok:
            return
        mode = dict(modes)[label]

        mounting: dict = {}
        if mode != BOTH and self._board:
            mounting, _pcb = read_mounting_for_schematic(self._board)
        lines = build_digikey_bom(self._records, mounting=mounting, mounting_filter=mode)

        start = str(Path(self._board).with_name(Path(self._board).stem + "_digikey.csv")) \
            if self._board else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export DigiKey BOM", start, "CSV (*.csv)"
        )
        if not path:
            return
        dest = Path(path)
        if dest.suffix != ".csv":
            dest = dest.with_suffix(".csv")
        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                write_digikey_csv(lines, f)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        msg = f"Exported {len(lines)} line(s) to {dest.name}."
        if mode == SMD_ONLY:
            undet = undetermined_references(self._records, mounting)
            if undet:
                msg += f" {len(undet)} part(s) of undetermined mounting excluded."
        unorderable = unorderable_lines(lines)
        if unorderable:
            msg += f" {len(unorderable)} lack a DigiKey/manufacturer part number."
        self.statusBar().showMessage(msg)

    def _export_cpl(self) -> None:
        """Export the pick-and-place file as a JLCPCB-format CPL CSV, built from
        the board's footprint geometry. Read-only: never touches the schematic or
        the dirty flag. Designators are restricted to the BOM's so the CPL and
        BOM match (a JLCPCB requirement)."""
        if not self._records or not self._board:
            QMessageBox.information(self, "No board", "Open a schematic first.")
            return
        pcb = sibling_pcb(self._board)
        if pcb is None:
            QMessageBox.warning(
                self,
                "No board file",
                f"No {Path(self._board).with_suffix('.kicad_pcb').name} beside the "
                "schematic. The CPL is built from the board, so place the parts first.",
            )
            return
        # Match a --smd BOM: offer to keep only surface-mount parts.
        resp = QMessageBox.question(
            self,
            "Export JLCPCB CPL",
            "Include surface-mount (SMD) parts only?\n\n"
            "Yes: SMD parts only (mounting read from the board).\n"
            "No: all placed BOM parts.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.No,
        )
        if resp == QMessageBox.Cancel:
            return
        if resp == QMessageBox.Yes:
            mounting, _pcb = read_mounting_for_schematic(self._board)
            include = {r.reference for r in self._records if mounting.get(r.reference) == "smd"}
        else:
            include = {r.reference for r in self._records}

        # Honour the LCSC tab's per-part rotation overrides.
        lines = cpl_from_schematic(
            pcb, include=include, manual_rotation=self.lcsc_model.rotation_overrides()
        )

        start = str(Path(self._board).with_name(Path(self._board).stem + "_cpl.csv"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Export JLCPCB CPL", start, "CSV (*.csv)"
        )
        if not path:
            return
        dest = Path(path)
        if dest.suffix != ".csv":
            dest = dest.with_suffix(".csv")
        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                write_cpl_csv(lines, f)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        msg = f"Exported {len(lines)} placement(s) to {dest.name}."
        missing = include - {ln.designator for ln in lines}
        if missing:
            msg += f" {len(missing)} BOM part(s) have no footprint on the board."
        self.statusBar().showMessage(msg)

    # ---- quit --------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        """Guard the quit path: if there are unsaved edits, offer to Save,
        Discard, or Cancel. Fires for both File ▸ Quit (which calls ``close``)
        and the window's close button."""
        if not self.isWindowModified():
            self._stop_threads()
            event.accept()
            return
        resp = QMessageBox.question(
            self,
            "Unsaved changes",
            "You have unsaved changes. Save before quitting?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if resp == QMessageBox.Cancel:
            event.ignore()
            return
        if resp == QMessageBox.Discard:
            self._stop_threads()
            event.accept()
            return
        # Save: write in place (no second confirmation) and only quit if it
        # succeeds — a failed write keeps the window open so nothing is lost.
        self._stop_threads()  # a reload must not happen under a running lookup
        edits = self._merged_edits()
        if edits and not self._write_in_place(edits):
            event.ignore()
            return
        event.accept()

    # ---- status bar --------------------------------------------------
    def _refresh_status(self) -> None:
        s = self.model.summary()
        self.statusBar().showMessage(
            f"{sum(s.values())} components — {s['typed']} typed, "
            f"{s['guessed']} guessed, {s['accepted']} accepted, "
            f"{s['unclassified']} unclassified"
        )
