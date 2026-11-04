"""The "Assign through-hole part" dialog for one through-hole group
(``THT_PLAN.md`` step 9).

A through-hole part is found from its footprint (a header's rows, positions,
pitch and orientation, or the part number in a footprint's name) and from the
part numbers its symbol carries; nothing here is editable. The header lists
the references, the Value, the current part, the footprint with its signature
and pad count, any mounting note, the quantity, the build quantity, the units
needed and the stock floor. Through-hole parts are soldered by hand, so only
DigiKey is searched and no LCSC number is written.

The dialog runs no network code. A search is requested with
``search_requested(None, search_digikey)``: the first request (from the
window) sends ``False``, so a part stored in the parts database for this
footprint is offered without a DigiKey search; the *Search DigiKey* button
sends ``True`` and searches DigiKey whatever is stored. The window runs
:func:`kicadtools.thtassign.assess_tht` on a worker thread and passes the
outcome to :meth:`show_result` or :meth:`show_error`. Requests are numbered
(:attr:`request_id`); an outcome tagged with an older number is ignored.

The candidate table lists MPN, manufacturer, source (database, current part,
series, maker list or the text that found it), DigiKey PN and stock, the pad
check, unit price and DigiKey's description. Right-click a candidate to
reject it (``reject_requested(candidate)``). *Accept* closes the dialog;
:meth:`chosen` gives the pick. For a header or socket, the first part
accepted in a run sets the run's series (the window calls
:meth:`kicadtools.headersearch.HeaderRun.pick`).
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import List, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QTableWidget,
    QVBoxLayout,
)

from kicadtools.gui.parts import CHECK_TINTS, DASH, current_part_cell
from kicadtools.gui.sorting import (
    SortItem,
    data_row,
    filling,
    make_sortable,
    remove_data_row,
    selected_data_row,
    view_row,
)
from kicadtools.icsearch import MATCH, UNVERIFIED, stock_floor, stock_minimum
from kicadtools.suppliers import digikey_url
from kicadtools.thtassign import (
    ThtCandidate,
    ThtGroup,
    ThtRequirement,
    ThtSearchResult,
    pads_text,
    rank_text,
    signature_text,
    tht_assignment_plan,
)
from kicadtools.thtfootprint import HEADER, SOCKET

THT_CANDIDATE_COLUMNS = [
    "MPN", "Manufacturer", "Source", "DigiKey PN", "DigiKey stock", "Pads",
    "Unit price", "Description",
]
COL_THT_DIGIKEY = THT_CANDIDATE_COLUMNS.index("DigiKey PN")
COL_THT_STOCK = THT_CANDIDATE_COLUMNS.index("DigiKey stock")
COL_THT_PADS = THT_CANDIDATE_COLUMNS.index("Pads")

_WARN_STYLE = "color: #c07000; font-weight: bold;"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def tht_candidate_cells(c: ThtCandidate) -> List[str]:
    """The table row for *c*, in :data:`THT_CANDIDATE_COLUMNS` order."""
    price = "" if math.isinf(c.price) else f"${c.price:.4f}"
    stock = "" if c.stock is None else f"{c.stock:,}"
    return [c.mpn, c.manufacturer, rank_text(c), c.digikey_pn or "", stock,
            pads_text(c), price, c.description]


def footprint_header(req: ThtRequirement) -> str:
    """The header's Footprint line: ``"Connector_PinHeader_2.54mm:PinHeader_1x04_…
    — header 1x04, 2.54 mm, vertical; 4 pads"``."""
    if not req.footprint:
        return "none"
    pads = f"{req.pad_count} pads" if req.pad_count is not None else "pad count unknown"
    return f"{req.footprint} — {signature_text(req.signature)}; {pads}"


def tht_result_summary(result: ThtSearchResult) -> str:
    """The status line for a search outcome."""
    n = len(result.candidates)
    text = f"{n} candidate{'s' * (n != 1)}"
    if result.from_database:
        text += " from the parts database (DigiKey not searched; Search DigiKey for others)"
    elif result.total and result.total > n:
        text += f" shown of {result.total:,} at DigiKey"
    if result.series:
        text += f"; series {result.series}"
    if result.rejected:
        text += f"; rejected {len(result.rejected)}"
    text += "."
    problem = result.problem.strip().rstrip(".")
    if problem and not n:
        text += f" {problem[0].upper()}{problem[1:]}."
    return text


def pad_message(c: Optional[ThtCandidate]) -> str:
    """The line above the Accept button for the selected candidate's pad
    check; ``""`` for a match or no selection."""
    if c is None or c.pads.result == MATCH:
        return ""
    return f"Pads: {c.pads.result} — {c.pads.reason}. Check the footprint against the datasheet."


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #

class ThtAssignDialog(QDialog):
    search_requested = Signal(object, bool)   # (None, search DigiKey whatever is stored)
    reject_requested = Signal(object)         # ThtCandidate

    def __init__(self, group: ThtGroup, build_qty: int, parent=None, *,
                 series: str = "") -> None:
        """*series* is the run's header series as text (``""`` before one is
        set), shown for header and socket groups."""
        super().__init__(parent)
        req = group.requirement
        refs = group.references
        self.setWindowTitle(f"Assign through-hole part — {req.value_text or '?'} — {', '.join(refs)}")
        self.resize(1150, 560)
        self._group = group
        self._req = req
        self.needed = group.quantity * max(1, int(build_qty))
        self._candidates: List[ThtCandidate] = []
        self._result: Optional[ThtSearchResult] = None
        self._request_id = 0
        self._last_search_digikey = False

        head = QFormLayout()
        refs_label = QLabel(", ".join(refs))
        refs_label.setWordWrap(True)
        head.addRow("References", refs_label)
        head.addRow("Value", QLabel(req.value_text or DASH))
        head.addRow("Current part", QLabel(current_part_cell(group)))
        fp = QLabel(footprint_header(req))
        fp.setWordWrap(True)
        fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fp.setToolTip("Read-only: the footprint is never written.")
        head.addRow("Footprint", fp)
        if req.mounting_note:
            note = QLabel(req.mounting_note)
            note.setWordWrap(True)
            head.addRow("Mounting", note)
        if req.family in (HEADER, SOCKET):
            self._series = QLabel(series or "not set yet: the first part accepted sets it")
            head.addRow("Series", self._series)
        head.addRow("Quantity", QLabel(f"{group.quantity} per board"))
        head.addRow("Build quantity", QLabel(f"{int(build_qty):,} boards"))
        head.addRow("Units needed", QLabel(f"{self.needed:,}"))
        head.addRow("Stock floor", QLabel(
            f"{stock_floor(self.needed):,} at DigiKey; down to "
            f"{stock_minimum(self.needed):,} with a warning (hand-soldered: DigiKey only)"))

        self._status = QLabel("")
        self._status.setWordWrap(True)

        self._table = QTableWidget(0, len(THT_CANDIDATE_COLUMNS))
        self._table.setHorizontalHeaderLabels(THT_CANDIDATE_COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._selection_changed)
        self._table.cellClicked.connect(self._cell_clicked)
        self._table.cellDoubleClicked.connect(self._double_clicked)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        make_sortable(self._table)

        self._notes = QPlainTextEdit()
        self._notes.setReadOnly(True)
        self._notes.setMaximumHeight(110)
        self._notes.setPlaceholderText("Lookups made by the search are listed here.")

        self._pads = QLabel("")
        self._pads.setWordWrap(True)
        self._pads.setStyleSheet(_WARN_STYLE)
        self._pads.hide()
        self._stock = QLabel("")
        self._stock.setWordWrap(True)
        self._stock.setStyleSheet(_WARN_STYLE)
        self._stock.hide()

        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._search_btn = self._buttons.addButton("Search DigiKey", QDialogButtonBox.ActionRole)
        self._search_btn.setToolTip("Search DigiKey even when the parts database has a part "
                                    "for this footprint.")
        self._search_btn.clicked.connect(lambda: self.request_search(True))
        self._accept_btn = self._buttons.addButton("Accept", QDialogButtonBox.AcceptRole)
        self._accept_btn.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(self._status)
        layout.addWidget(self._table, 1)
        layout.addWidget(QLabel("Lookups"))
        layout.addWidget(self._notes)
        layout.addWidget(self._pads)
        layout.addWidget(self._stock)
        layout.addWidget(self._buttons)

        if not req.searchable:
            self._status.setText(f"Can't search: {req.problem() or 'nothing to buy'}.")
            self._search_btn.setEnabled(False)

    # ---- state -------------------------------------------------------
    @property
    def group(self) -> ThtGroup:
        return self._group

    @property
    def requirement(self) -> ThtRequirement:
        return self._req

    @property
    def search_requirement(self) -> None:
        return None

    @property
    def result(self) -> Optional[ThtSearchResult]:
        return self._result

    @property
    def existing_lookup(self):
        """Always ``None`` (kept so the window can treat every dialog alike)."""
        return None

    @property
    def request_id(self) -> int:
        return self._request_id

    @property
    def search_digikey(self) -> bool:
        """Whether the latest request asked to search DigiKey whatever is stored."""
        return self._last_search_digikey

    # ---- search ------------------------------------------------------
    def request_search(self, search_digikey: bool = False) -> bool:
        if not self._req.searchable:
            self._status.setText(f"Can't search: {self._req.problem() or 'nothing to buy'}.")
            return False
        self._request_id += 1
        self._last_search_digikey = bool(search_digikey)
        self._status.setText("Searching DigiKey…" if search_digikey
                             else "Searching the parts database and DigiKey…")
        self.search_requested.emit(None, bool(search_digikey))
        return True

    def rerun(self) -> bool:
        """Request the search again (after a rejection), as the last one ran."""
        return self.request_search(self._last_search_digikey)

    def _stale(self, tag: Optional[int]) -> bool:
        return tag is not None and tag != self._request_id

    def show_result(self, result: ThtSearchResult, tag: Optional[int] = None) -> None:
        if self._stale(tag):
            return
        self._result = result
        self._candidates = list(result.candidates)
        self._status.setText(tht_result_summary(result))
        self._status.setToolTip("\n".join(f"{m}: {why}" for m, why in result.rejected[:40]))
        if result.series and hasattr(self, "_series"):
            self._series.setText(result.series)
        self._notes.setPlainText("\n".join(result.notes))
        self._table.clearSelection()
        with filling(self._table):
            self._table.setRowCount(len(self._candidates))
            link = self.palette().link().color()
            for r, cand in enumerate(self._candidates):
                for col, text in enumerate(tht_candidate_cells(cand)):
                    item = SortItem(text)
                    if col == COL_THT_PADS:
                        tint = CHECK_TINTS.get(cand.pads.result)
                        if tint is not None:
                            item.setBackground(tint)
                        item.setToolTip(cand.pads.reason)
                    if col == COL_THT_STOCK and cand.stock_warning:
                        item.setBackground(CHECK_TINTS[UNVERIFIED])
                        item.setToolTip(cand.stock_warning)
                    if col == COL_THT_DIGIKEY:
                        url = digikey_url(cand.digikey_pn or "")
                        if url:
                            item.setForeground(link)
                            item.setToolTip(url)
                    self._table.setItem(r, col, item)
        if self._candidates:
            self._table.selectRow(view_row(self._table, 0))
        self._selection_changed()

    def show_error(self, message: str, tag: Optional[int] = None) -> None:
        if self._stale(tag):
            return
        self._status.setText(f"Search failed: {message}")

    # ---- table -------------------------------------------------------
    def _cell_clicked(self, row: int, col: int) -> None:
        row = data_row(self._table, row)
        if col == COL_THT_DIGIKEY and 0 <= row < len(self._candidates):
            url = digikey_url(self._candidates[row].digikey_pn or "")
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _double_clicked(self, row: int, col: int) -> None:
        row = data_row(self._table, row)
        if col == COL_THT_DIGIKEY:
            return
        if 0 <= row < len(self._candidates):
            self.accept()

    def _context_menu(self, pos) -> None:
        item = self._table.itemAt(pos)
        r = data_row(self._table, item.row()) if item is not None else -1
        if not 0 <= r < len(self._candidates):
            return
        cand = self._candidates[r]
        menu = QMenu(self)
        act = menu.addAction(f"Reject {cand.mpn}")
        if menu.exec(self._table.viewport().mapToGlobal(pos)) is act:
            self.reject_requested.emit(cand)

    def drop_candidate(self, candidate: ThtCandidate) -> None:
        for r, c in enumerate(self._candidates):
            if c is candidate:
                del self._candidates[r]
                remove_data_row(self._table, r)
                break
        if self._result is not None:
            self._result = replace(self._result, candidates=[
                c for c in self._result.candidates if c is not candidate])
        self._selection_changed()

    def accept_problem(self, candidate: ThtCandidate) -> str:
        try:
            tht_assignment_plan(self._group, candidate)
        except ValueError as exc:
            return str(exc)
        return ""

    def requirement_for(self, candidate: Optional[ThtCandidate]) -> ThtRequirement:
        return self._req

    def _selected(self) -> Optional[ThtCandidate]:
        r = selected_data_row(self._table)
        return self._candidates[r] if 0 <= r < len(self._candidates) else None

    def pads_text(self) -> str:
        return self._pads.text()

    def stock_text(self) -> str:
        return self._stock.text()

    def _selection_changed(self) -> None:
        cand = self._selected()
        problem = self.accept_problem(cand) if cand is not None else ""
        self._accept_btn.setEnabled(cand is not None and not problem)
        self._accept_btn.setToolTip(problem)
        text = pad_message(cand)
        self._pads.setText(text)
        self._pads.setVisible(bool(text))
        stock = (f"Stock is below the floor — check it before ordering: {cand.stock_warning}."
                 if cand is not None and cand.stock_warning else "")
        self._stock.setText(stock)
        self._stock.setVisible(bool(stock))

    def chosen(self) -> Optional[ThtCandidate]:
        cand = self._selected()
        if cand is None or self.accept_problem(cand):
            return None
        return cand

    def accept(self) -> None:
        if self.chosen() is None:
            return
        super().accept()
