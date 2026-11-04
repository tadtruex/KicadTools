"""The "Assign IC" dialog for one IC group (``IC_PLAN.md`` step 8).

An IC is found from the part numbers its symbol carries, so nothing in this
dialog is editable: the header lists the references, the Value, the
identifiers (``MPN``, ``DigiKeyPN``, ``LCSC`` and the Value's MPN-shaped
tokens), the footprint with the package parsed from its name and its pad
count, the quantity, the build quantity, the units needed, the stock floor
(:func:`kicadtools.icsearch.stock_floor`) and the stock minimum
(:func:`kicadtools.icsearch.stock_minimum`). A candidate whose stock is below
the floor shows its stock cell in amber and a warning above *Accept*.

The dialog runs no network code. A search is requested with the
``search_requested(requirement, search_suppliers)`` signal (the second
argument is always ``True``: every IC is confirmed at DigiKey); the window
runs :func:`kicadtools.icsearch.assess_ic` on a worker thread and passes the
outcome to :meth:`show_result` or :meth:`show_error`. The first request sends
``None`` unless the dialog was opened with a requirement whose pad count was
already read: the worker then reads the footprint's pad count. The
requirement a result names (package and pad count filled in) is sent with
every later request, so the footprint files are read once per dialog. Every
request is numbered (:attr:`request_id`) and an outcome tagged with an older
number is ignored.

The candidate table lists MPN, manufacturer, source, DigiKey PN and stock,
LCSC number and JLCPCB stock (``not at JLCPCB`` when JLCPCB does not list the
part or cannot assemble it), the DigiKey package and the footprint check, the
check cell coloured red for a mismatch and amber when the footprint cannot be
verified. The selected candidate's check message is shown above the Accept
button. Lookup notes are listed below the table; identifiers that name
different parts are listed in amber (each part is still a candidate, so the
user picks one). A candidate whose write plan fails
(:func:`kicadtools.icsearch.ic_assignment_plan`: no MPN, manufacturer or
DigiKey PN) cannot be accepted; the Accept button's tooltip gives the reason.

Right-click a candidate to reject it (``reject_requested(candidate)``); the
window records the rejection, removes the row with :meth:`drop_candidate` and
requests the search again. *Accept* closes the dialog; :meth:`chosen` gives
the pick and :meth:`requirement_for` the requirement it was accepted against.

Cell and label text comes from the module-level pure helpers
(:func:`ic_candidate_cells`, :func:`ic_source_text`, :func:`ic_result_summary`,
:func:`footprint_header`, :func:`check_message`), testable without a window.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import List, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
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

from kicadtools.gui.parts import (
    CHECK_TINTS,
    DASH,
    current_part_cell,
    identifiers_text,
    package_text,
)
from kicadtools.gui.sorting import (
    SortItem,
    data_row,
    filling,
    make_sortable,
    remove_data_row,
    selected_data_row,
    view_row,
)
from kicadtools.icrequirement import IcGroup, IcRequirement
from kicadtools.icsearch import (
    DATABASE,
    MATCH,
    MISMATCH,
    SEARCH,
    UNVERIFIED,
    FootprintCheck,
    IcCandidate,
    IcSearchResult,
    ic_assignment_plan,
    stock_floor,
    stock_minimum,
)
from kicadtools.package import parse_footprint
from kicadtools.suppliers import digikey_url, lcsc_url

IC_CANDIDATE_COLUMNS = [
    "MPN", "Manufacturer", "Source", "DigiKey PN", "DigiKey stock",
    "LCSC", "JLC library", "JLC stock", "Package", "Footprint", "Unit price",
]
COL_IC_DIGIKEY = IC_CANDIDATE_COLUMNS.index("DigiKey PN")
COL_IC_LCSC = IC_CANDIDATE_COLUMNS.index("LCSC")
COL_IC_CHECK = IC_CANDIDATE_COLUMNS.index("Footprint")
COL_IC_DK_STOCK = IC_CANDIDATE_COLUMNS.index("DigiKey stock")
COL_IC_JLC_STOCK = IC_CANDIDATE_COLUMNS.index("JLC stock")

# The check message's text colour per result; a match is plain text.
_CHECK_STYLES = {
    MISMATCH: "color: #c02020; font-weight: bold;",
    UNVERIFIED: "color: #c07000; font-weight: bold;",
}

MATCH_TEXT = "Footprint matches the part's package."


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def _count(n: Optional[int]) -> str:
    return "" if n is None else f"{n:,}"


def ic_source_text(c: IcCandidate) -> str:
    """The Source cell: ``"database (2 uses)"``, ``"current part
    (DigiKeyPN, Value)"`` for a part an identifier names exactly, ``"partial
    MPN (Value)"`` for one found from a partial MPN."""
    named = f" ({', '.join(c.fields)})" if c.fields else ""
    if c.source == DATABASE:
        uses = f" ({c.usage_count} use{'s' * (c.usage_count != 1)})" if c.usage_count else ""
        return "database" + uses
    if c.source == SEARCH and not c.current:
        return "partial MPN" + named
    return "current part" + named


def ic_candidate_cells(c: IcCandidate) -> List[str]:
    """The table row for *c*, in :data:`IC_CANDIDATE_COLUMNS` order."""
    price = "" if math.isinf(c.price) else f"${c.price:.4f}"
    library = {"basic": "Basic", "extended": "Extended"}.get(c.jlc_library or "", "")
    if library and c.jlc_preferred:
        library += " (preferred)"
    return [
        c.mpn,
        c.manufacturer,
        ic_source_text(c),
        c.digikey_pn or "",
        _count(c.digikey_stock),
        c.jlc_text,
        library if c.lcsc else "",
        _count(c.jlc_stock) if c.lcsc else "",
        c.package or c.package_case,
        c.footprint.result,
        price,
    ]


def check_message(check: Optional[FootprintCheck]) -> str:
    """The line above the Accept button for a selected candidate's check:
    :attr:`FootprintCheck.message`, or :data:`MATCH_TEXT` for a match; ``""``
    with no candidate selected."""
    if check is None:
        return ""
    return check.message if check.result != MATCH else MATCH_TEXT


def footprint_header(req: IcRequirement) -> str:
    """The header's Footprint line: the lib id, the package parsed from its
    name and the pad count once read (``"Package_TO_SOT_SMD:SOT-23-5 —
    SOT-23-5; 5 pads"``); ``"none"`` without a footprint."""
    if not req.footprint:
        return "none"
    info = req.package if req.package is not None else parse_footprint(req.footprint)
    parsed = package_text(info) or "package not readable from the name"
    pads = f"{req.pad_count} pads" if req.pad_count is not None else "pad count unknown"
    return f"{req.footprint} — {parsed}; {pads}"


def ic_result_summary(result: IcSearchResult) -> str:
    """The status line for an IC search outcome."""
    n = len(result.candidates)
    from_db = sum(1 for c in result.candidates if c.source == DATABASE)
    # The same test as ic_source_text: a partial-MPN hit that is exactly the
    # part the symbol names counts as named, as its Source cell says.
    partial = sum(1 for c in result.candidates if c.source == SEARCH and not c.current)
    named = n - from_db - partial
    parts = [f"{n} candidate{'s' * (n != 1)} ({from_db} from the database, "
             f"{named} named by the symbol, {partial} from a partial MPN)"]
    if result.rejected:
        parts.append(f"rejected {len(result.rejected)}")
    if result.dropped_by_limit:
        parts.append(f"{result.dropped_by_limit} past the DigiKey lookup limit, not checked")
    text = "; ".join(parts) + "."
    problem = result.problem.strip().rstrip(".")
    if problem:
        text += f" {problem[0].upper()}{problem[1:]}."
    return text


def stock_warning_text(cand: IcCandidate) -> str:
    """The amber line for a candidate whose stock is below the floor, ``""``
    when it meets it."""
    if not cand.stock_warnings:
        return ""
    return ("Stock is below the floor — check it before ordering: "
            + "; ".join(cand.stock_warnings) + ".")


def conflicts_text(result: IcSearchResult) -> str:
    """The amber note for identifiers that name different parts, ``""`` when
    they agree."""
    if not result.conflicts:
        return ""
    return ("Identifiers name different parts — accept the right one and fix "
            "the other on the schematic:\n" + "\n".join(result.conflicts))



# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #

class IcAssignDialog(QDialog):
    search_requested = Signal(object, bool)   # (IcRequirement or None, True)
    reject_requested = Signal(object)         # IcCandidate

    def __init__(
        self,
        group: IcGroup,
        requirement: IcRequirement,
        build_qty: int,
        parent=None,
    ) -> None:
        """*requirement* is the group's requirement, or the requirement of a
        search recorded for the row (its pad count then already read)."""
        super().__init__(parent)
        refs = group.references
        self.setWindowTitle(
            f"Assign IC — {requirement.identifiers.mpn or requirement.value_text or '?'}"
            f" — {', '.join(refs)}")
        self.resize(1150, 600)
        self._group = group
        self._req = requirement
        # The requirement is resolved once ic_requirement_for has run on it (a
        # recorded or returned search): that sets ``package`` always, and
        # ``pad_count`` only when the footprint was found, so ``package`` is
        # the marker. Until then a request sends None and the worker reads
        # the footprint files.
        self._resolved = requirement.package is not None
        self.needed = group.quantity * max(1, int(build_qty))
        self._candidates: List[IcCandidate] = []
        self._result: Optional[IcSearchResult] = None
        self._request_id = 0

        head = QFormLayout()
        refs_label = QLabel(", ".join(refs))
        refs_label.setWordWrap(True)
        head.addRow("References", refs_label)
        head.addRow("Value", QLabel(requirement.value_text or DASH))
        ids = QLabel(identifiers_text(requirement))
        ids.setWordWrap(True)
        ids.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addRow("Identifiers", ids)
        head.addRow("Current part", QLabel(current_part_cell(group)))
        self._footprint_label = QLabel(footprint_header(requirement))
        self._footprint_label.setWordWrap(True)
        self._footprint_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._footprint_label.setToolTip("Read-only: the footprint is never written.")
        head.addRow("Footprint", self._footprint_label)
        head.addRow("Quantity", QLabel(f"{group.quantity} per board"))
        head.addRow("Build quantity", QLabel(f"{int(build_qty):,} boards"))
        head.addRow("Units needed", QLabel(f"{self.needed:,}"))
        head.addRow("Stock floor", QLabel(
            f"{stock_floor(self.needed):,} at DigiKey (required) and at JLCPCB; "
            f"down to {stock_minimum(self.needed):,} with a warning"))

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._conflicts = QLabel("")
        self._conflicts.setWordWrap(True)
        self._conflicts.setStyleSheet("color: #c07000; font-weight: bold;")
        self._conflicts.hide()

        self._table = QTableWidget(0, len(IC_CANDIDATE_COLUMNS))
        self._table.setHorizontalHeaderLabels(IC_CANDIDATE_COLUMNS)
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

        # The selected candidate's footprint check, above the buttons.
        self._check = QLabel("")
        self._check.setWordWrap(True)
        self._check.hide()
        # The selected candidate's stock warnings (below the floor).
        self._stock = QLabel("")
        self._stock.setWordWrap(True)
        self._stock.setStyleSheet(_CHECK_STYLES[UNVERIFIED])
        self._stock.hide()

        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._search_btn = self._buttons.addButton("Search again", QDialogButtonBox.ActionRole)
        self._search_btn.clicked.connect(lambda: self.request_search())
        self._accept_btn = self._buttons.addButton("Accept", QDialogButtonBox.AcceptRole)
        self._accept_btn.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(self._status)
        layout.addWidget(self._conflicts)
        layout.addWidget(self._table, 1)
        layout.addWidget(QLabel("Lookups"))
        layout.addWidget(self._notes)
        layout.addWidget(self._check)
        layout.addWidget(self._stock)
        layout.addWidget(self._buttons)

        if not requirement.searchable:
            self._status.setText(f"Can't search: {requirement.problem()}.")
            self._search_btn.setEnabled(False)

    # ---- state -------------------------------------------------------
    @property
    def group(self) -> IcGroup:
        return self._group

    @property
    def requirement(self) -> IcRequirement:
        """The requirement searched: the opening one until a result names
        one with its package and pad count."""
        return self._req

    @property
    def search_requirement(self) -> Optional[IcRequirement]:
        """The requirement a search request sends: ``None`` until it is
        resolved (the worker then reads the footprint's pad count)."""
        return self._req if self._resolved else None

    @property
    def result(self) -> Optional[IcSearchResult]:
        """The last result shown, ``None`` before one, without the candidates
        dropped since (:meth:`drop_candidate`)."""
        return self._result

    @property
    def existing_lookup(self):
        """Always ``None``: an IC has no existing-part lookup separate from its
        search (kept so the window can treat both dialogs alike)."""
        return None

    @property
    def request_id(self) -> int:
        """The number of the latest search request."""
        return self._request_id

    # ---- search ------------------------------------------------------
    def request_search(self, search_suppliers: bool = True) -> bool:
        """Emit ``search_requested``; ``False`` (with the reason in the status
        line) when the requirement is not searchable. *search_suppliers* is
        accepted for the window's sake and ignored: an IC search always asks
        DigiKey and JLCPCB."""
        del search_suppliers
        if not self._req.searchable:
            self._status.setText(f"Can't search: {self._req.problem()}.")
            return False
        self._request_id += 1
        self._status.setText("Searching the parts database, DigiKey and JLCPCB…")
        self.search_requested.emit(self.search_requirement, True)
        return True

    def rerun(self) -> bool:
        """Request the search again (after a rejection)."""
        return self.request_search()

    def _stale(self, tag: Optional[int]) -> bool:
        return tag is not None and tag != self._request_id

    def show_result(self, result: IcSearchResult, tag: Optional[int] = None) -> None:
        """Show *result*, unless *tag* names an older request. Its requirement
        (package and pad count read) is kept for later requests, also from a
        stale result, so a second request does not read the footprints again."""
        if self._stale(tag):
            if not self._resolved and result.requirement is not None:
                self._req = result.requirement
                self._resolved = True
                self._footprint_label.setText(footprint_header(self._req))
            return
        if result.requirement is not None:
            self._req = result.requirement
            self._resolved = True
            self._footprint_label.setText(footprint_header(self._req))
        self._result = result
        self._candidates = list(result.candidates)
        self._status.setText(ic_result_summary(result))
        if result.rejected:
            lines = [f"{label}: {why}" for label, why in result.rejected[:40]]
            if len(result.rejected) > 40:
                lines.append(f"… and {len(result.rejected) - 40} more")
            self._status.setToolTip("\n".join(lines))
        else:
            self._status.setToolTip("")
        text = conflicts_text(result)
        self._conflicts.setText(text)
        self._conflicts.setVisible(bool(text))
        self._notes.setPlainText("\n".join(result.notes))

        self._table.clearSelection()
        with filling(self._table):
            self._table.setRowCount(len(self._candidates))
            link = self.palette().link().color()
            for r, cand in enumerate(self._candidates):
                for col, text in enumerate(ic_candidate_cells(cand)):
                    item = SortItem(text)
                    if col == COL_IC_CHECK:
                        tint = CHECK_TINTS.get(cand.footprint.result)
                        if tint is not None:
                            item.setBackground(tint)
                        item.setToolTip(check_message(cand.footprint))
                    warning = {COL_IC_DK_STOCK: cand.digikey_warning,
                               COL_IC_JLC_STOCK: cand.jlc_warning if cand.at_jlcpcb else ""}.get(col)
                    if warning:
                        item.setBackground(CHECK_TINTS[UNVERIFIED])
                        item.setToolTip(warning)
                    url = self._url(cand, col)
                    if url:
                        item.setForeground(link)
                        # The LCSC cell's note (why JLCPCB cannot assemble it)
                        # goes above its link.
                        note = cand.jlc_note if col == COL_IC_LCSC else ""
                        item.setToolTip(f"{note}\n{url}" if note else url)
                    self._table.setItem(r, col, item)
        if self._candidates:
            self._table.selectRow(view_row(self._table, 0))
        self._selection_changed()

    def show_error(self, message: str, tag: Optional[int] = None) -> None:
        if self._stale(tag):
            return
        self._status.setText(f"Search failed: {message}")

    # ---- table -------------------------------------------------------
    @staticmethod
    def _url(cand: IcCandidate, col: int) -> Optional[str]:
        if col == COL_IC_LCSC and cand.lcsc:
            return lcsc_url(cand.lcsc)
        if col == COL_IC_DIGIKEY:
            return digikey_url(cand.digikey_pn or "")
        return None

    def _cell_clicked(self, row: int, col: int) -> None:
        row = data_row(self._table, row)
        if 0 <= row < len(self._candidates):
            url = self._url(self._candidates[row], col)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _double_clicked(self, row: int, col: int) -> None:
        row = data_row(self._table, row)
        if col in (COL_IC_LCSC, COL_IC_DIGIKEY):
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

    def drop_candidate(self, candidate: IcCandidate) -> None:
        """Remove *candidate*'s row (after it has been rejected), and remove it
        from :attr:`result`, which the window records on the row when the
        dialog closes before the next search returns."""
        for r, c in enumerate(self._candidates):
            if c is candidate:
                del self._candidates[r]
                remove_data_row(self._table, r)
                break
        if self._result is not None:
            self._result = replace(self._result, candidates=[
                c for c in self._result.candidates if c is not candidate])
        self._selection_changed()

    def accept_problem(self, candidate: IcCandidate) -> str:
        """``""`` when *candidate* can be accepted, else a one-line reason."""
        try:
            ic_assignment_plan(self._group, candidate)
        except ValueError as exc:
            return str(exc)
        return ""

    def requirement_for(self, candidate: Optional[IcCandidate]) -> IcRequirement:
        """The requirement *candidate* is accepted against (the searched
        one; an IC requirement has no fields to edit)."""
        return self._req

    def _selected(self) -> Optional[IcCandidate]:
        r = selected_data_row(self._table)
        return self._candidates[r] if 0 <= r < len(self._candidates) else None

    def check_text(self) -> str:
        """The footprint-check line shown above the buttons (``""`` when no
        candidate is selected)."""
        return self._check.text()

    def stock_text(self) -> str:
        """The stock-warning line shown above the buttons (``""`` when the
        selected candidate meets the floor or none is selected)."""
        return self._stock.text()

    def _selection_changed(self) -> None:
        cand = self._selected()
        problem = self.accept_problem(cand) if cand is not None else ""
        self._accept_btn.setEnabled(cand is not None and not problem)
        self._accept_btn.setToolTip(problem)
        check = cand.footprint if cand is not None else None
        text = check_message(check)
        self._check.setText(text)
        self._check.setStyleSheet(_CHECK_STYLES.get(check.result, "") if check else "")
        self._check.setVisible(bool(text))
        stock = stock_warning_text(cand) if cand is not None else ""
        self._stock.setText(stock)
        self._stock.setVisible(bool(stock))

    def chosen(self) -> Optional[IcCandidate]:
        """The selected candidate when it can be accepted, else ``None``."""
        cand = self._selected()
        if cand is None or self.accept_problem(cand):
            return None
        return cand

    def accept(self) -> None:
        """Close with *Accept* only when :meth:`chosen` has a candidate (a
        double-click reaches here with the button disabled)."""
        if self.chosen() is None:
            return
        super().accept()
