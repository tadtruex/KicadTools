"""The "Assign LED" dialog for one plain-LED group (``LED_PLAN.md``, step 5).

The header lists the references, the Value, the current part, the footprint,
the quantity and the units needed. The requirement fields are editable:
colour (with its wavelength band beside it), wavelength, minimum intensity
(mcd), maximum forward voltage and lens. An empty limit field means no limit.

The dialog runs no network code; it has the interface of
:class:`~kicadtools.gui.tht_assign_dialog.ThtAssignDialog`, so the window's
search queue, accept and reject paths take it. The first request (from the
window) sends ``search_requested(None, False)``: the worker builds the
requirement from the group and its existing part and lists the current or a
stored part before searching. *Search* sends the edited requirement with
``True``: the JLCPCB listing is searched whatever is stored. Requests are
numbered; an outcome for an older one is ignored. Right-click a candidate to
reject it.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import List, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from kicadtools import ledcolor
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
from kicadtools.icsearch import UNVERIFIED
from kicadtools.ledassign import led_assignment_plan
from kicadtools.ledrequirement import CLEAR, DIFFUSED, LedGroup, LedRequirement
from kicadtools.ledsearch import LedCandidate
from kicadtools.partsearch import MIN_STOCK, SearchResult
from kicadtools.suppliers import digikey_url, lcsc_url

LED_CANDIDATE_COLUMNS = [
    "MPN", "Manufacturer", "Source", "LCSC", "JLCPCB", "JLCPCB stock",
    "DigiKey PN", "DigiKey stock", "Dominant", "Intensity", "Vf", "Lens",
    "Unit price", "Description",
]
COL_LED_LCSC = LED_CANDIDATE_COLUMNS.index("LCSC")
COL_LED_JLC_STOCK = LED_CANDIDATE_COLUMNS.index("JLCPCB stock")
COL_LED_DIGIKEY = LED_CANDIDATE_COLUMNS.index("DigiKey PN")
COL_LED_DK_STOCK = LED_CANDIDATE_COLUMNS.index("DigiKey stock")

_LENS_CHOICES = ["any", CLEAR, DIFFUSED]
_WARN_STYLE = "color: #c07000; font-weight: bold;"
_SOURCE_TEXT = {"current": "current part", "database": "parts database",
                "search": "JLCPCB search"}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def _nums(values, unit: str) -> str:
    return ", ".join(f"{v:g}" for v in values) + f" {unit}" if values else ""


def led_candidate_cells(c: LedCandidate) -> List[str]:
    """The table row for *c*, in :data:`LED_CANDIDATE_COLUMNS` order."""
    ev = c.led
    price = "" if math.isinf(c.jlc_price) else f"${c.jlc_price:.4f}"
    jlc_stock = "" if c.jlc_stock is None else f"{c.jlc_stock:,}"
    dk_stock = "" if c.digikey_stock is None else f"{c.digikey_stock:,}"
    return [c.mpn, c.manufacturer, _SOURCE_TEXT.get(c.source, c.source), c.lcsc or "",
            c.jlc_library or "", jlc_stock, c.digikey_pn or "", dk_stock,
            _nums(ev.dominant or ev.peak, "nm"), _nums(ev.intensity_mcd, "mcd"),
            _nums(ev.vf, "V"), ev.lens or "", price, c.description]


def led_result_summary(result: SearchResult) -> str:
    """The status line for a search outcome."""
    n = len(result.candidates)
    text = f"{n} candidate{'s' * (n != 1)}"
    if result.examined:
        text += f" from {result.examined:,} JLCPCB listings"
    if result.rejected:
        text += f"; rejected {len(result.rejected)}"
    text += "."
    problem = result.problem.strip().rstrip(".")
    if problem:
        text += f" {problem[0].upper()}{problem[1:]}."
    elif n and not result.examined:
        text += " JLCPCB not searched; Search for others."
    return text


def edited_requirement(req: LedRequirement, *, color: str, wavelength: str,
                       intensity: str, vf: str, lens: str) -> LedRequirement:
    """*req* with the dialog's field texts applied: an empty limit is no
    limit; an empty colour leaves the colour as it was. Raises
    ``ValueError`` for text that does not parse."""
    kw = {}
    clear = []
    if color.strip():
        kw["colors"] = [c.strip() for c in color.split(",") if c.strip()]
    for name, text, parse in (("wavelength", wavelength, ledcolor.parse_nm),
                              ("intensity_min", intensity, ledcolor.parse_mcd),
                              ("vf_max", vf, ledcolor.parse_vf)):
        text = text.strip()
        if not text:
            if getattr(req, name).value is not None:
                clear.append(name)
            continue
        unit = {"wavelength": "nm", "intensity_min": "mcd", "vf_max": "V"}[name]
        values = parse(text if any(ch.isalpha() for ch in text) else text + unit)
        if not values:
            raise ValueError(f"'{text}' is not a {unit} value")
        if getattr(req, name).value is None or abs(getattr(req, name).value - values[0]) > 1e-9:
            kw[name] = values[0]
    if lens == "any":
        if req.lenses.value:
            clear.append("lenses")
    elif tuple(req.lenses.value) != (lens,):
        kw["lenses"] = [lens]
    return req.edited(clear=clear, **kw)


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #

class LedAssignDialog(QDialog):
    search_requested = Signal(object, bool)   # (requirement or None, search JLCPCB)
    reject_requested = Signal(object)         # LedCandidate

    def __init__(self, group: LedGroup, req: LedRequirement, build_qty: int,
                 parent=None, *, existing=None) -> None:
        super().__init__(parent)
        refs = group.references
        self.setWindowTitle(f"Assign LED — {req.value_text or '?'} — {', '.join(refs)}")
        self.resize(1250, 600)
        self._group = group
        self._req = req
        self._existing = existing
        self.needed = group.quantity * max(1, int(build_qty))
        self._candidates: List[LedCandidate] = []
        self._result: Optional[SearchResult] = None
        self._request_id = 0
        self._last_search = False

        head = QFormLayout()
        refs_label = QLabel(", ".join(refs))
        refs_label.setWordWrap(True)
        head.addRow("References", refs_label)
        head.addRow("Value", QLabel(req.value_text or DASH))
        head.addRow("Current part", QLabel(current_part_cell(group)))
        head.addRow("Footprint", QLabel(req.footprint or "none"))
        head.addRow("Package", QLabel(req.package.value or DASH))

        self._color = QComboBox()
        self._color.setEditable(True)
        self._color.addItems([""] + list(ledcolor.COLORS))
        self._color.setToolTip("Matched by dominant wavelength; several may be given, "
                               "comma-separated.")
        self._band = QLabel("")
        self._color.currentTextChanged.connect(self._show_band)
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self._color)
        hl.addWidget(self._band, 1)
        head.addRow("Colour", row)
        self._wavelength = QLineEdit()
        self._wavelength.setPlaceholderText("none (the colour's band); e.g. 525nm")
        self._wavelength.textChanged.connect(self._show_band)
        head.addRow("Wavelength", self._wavelength)
        self._intensity = QLineEdit()
        self._intensity.setPlaceholderText("no minimum; e.g. 50mcd")
        head.addRow("Intensity at least", self._intensity)
        self._vf = QLineEdit()
        self._vf.setPlaceholderText("no maximum; e.g. 2.2V")
        head.addRow("Vf at most", self._vf)
        self._lens = QComboBox()
        self._lens.addItems(_LENS_CHOICES)
        head.addRow("Lens", self._lens)
        head.addRow("Units needed", QLabel(
            f"{self.needed:,} ({group.quantity} per board × {int(build_qty):,} boards); "
            f"search hits need {max(MIN_STOCK, self.needed):,} at JLCPCB and DigiKey"))
        self._fill_fields(req)

        self._status = QLabel("")
        self._status.setWordWrap(True)

        self._table = QTableWidget(0, len(LED_CANDIDATE_COLUMNS))
        self._table.setHorizontalHeaderLabels(LED_CANDIDATE_COLUMNS)
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
        self._stock = QLabel("")
        self._stock.setWordWrap(True)
        self._stock.setStyleSheet(_WARN_STYLE)
        self._stock.hide()

        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._search_btn = self._buttons.addButton("Search", QDialogButtonBox.ActionRole)
        self._search_btn.setToolTip("Apply the fields and search the JLCPCB listing, "
                                    "even when the current or a stored part fits.")
        self._search_btn.clicked.connect(self._search_clicked)
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
        layout.addWidget(self._stock)
        layout.addWidget(self._buttons)

    # ---- fields ------------------------------------------------------
    def _fill_fields(self, req: LedRequirement) -> None:
        self._color.setCurrentText(", ".join(req.colors.value))
        wl = req.wavelength.value
        self._wavelength.setText(f"{wl:g}nm" if wl is not None else "")
        mcd = req.intensity_min.value
        self._intensity.setText(f"{mcd:g}mcd" if mcd is not None else "")
        vf = req.vf_max.value
        self._vf.setText(f"{vf:g}V" if vf is not None else "")
        lens = req.lenses.value[0] if len(req.lenses.value) == 1 else "any"
        self._lens.setCurrentText(lens)
        self._show_band()

    def _show_band(self, *_args) -> None:
        names = [c.strip() for c in self._color.currentText().split(",") if c.strip()]
        wl = self._wavelength.text().strip() or None
        texts = []
        for n in names:
            band = ledcolor.band_text(n, wl)
            texts.append(f"{n}: {band}" if band else f"{n}: not a colour")
        self._band.setText("; ".join(texts))

    def field_requirement(self) -> LedRequirement:
        """The requirement with the fields applied (``ValueError`` when one
        does not parse)."""
        return edited_requirement(
            self._req, color=self._color.currentText(), wavelength=self._wavelength.text(),
            intensity=self._intensity.text(), vf=self._vf.text(),
            lens=self._lens.currentText())

    # ---- state -------------------------------------------------------
    @property
    def group(self) -> LedGroup:
        return self._group

    @property
    def requirement(self) -> LedRequirement:
        return self._req

    @property
    def search_requirement(self) -> Optional[LedRequirement]:
        return None

    @property
    def result(self) -> Optional[SearchResult]:
        return self._result

    @property
    def existing_lookup(self):
        """The existing-part lookup of the last result (``None`` before
        one), reused by later searches and recorded on the row."""
        return self._existing

    @property
    def request_id(self) -> int:
        return self._request_id

    # ---- search ------------------------------------------------------
    def _search_clicked(self) -> None:
        try:
            req = self.field_requirement()
        except ValueError as exc:
            self._status.setText(f"Can't search: {exc}.")
            return
        self._req = req
        self.request_search(True)

    def request_search(self, search_suppliers: bool = False) -> bool:
        self._request_id += 1
        self._last_search = bool(search_suppliers)
        self._status.setText("Searching JLCPCB and DigiKey…" if search_suppliers
                             else "Looking up the current and stored parts…")
        first = self._request_id == 1 and not search_suppliers
        self.search_requested.emit(None if first else self._req, bool(search_suppliers))
        return True

    def rerun(self) -> bool:
        return self.request_search(self._last_search)

    def _stale(self, tag: Optional[int]) -> bool:
        return tag is not None and tag != self._request_id

    def show_result(self, result: SearchResult, tag: Optional[int] = None) -> None:
        if self._stale(tag):
            return
        self._result = result
        if result.existing is not None:
            self._existing = result.existing
        if result.requirement is not None:
            self._req = result.requirement
            self._fill_fields(self._req)
        self._candidates = list(result.candidates)
        summary = led_result_summary(result)
        if result.requirement is not None and result.requirement.conflicts:
            summary += " Conflict: " + "; ".join(result.requirement.conflicts)
        self._status.setText(summary)
        self._status.setToolTip("\n".join(f"{m}: {why}" for m, why in result.rejected[:40]))
        notes = list(result.existing.notes) if result.existing is not None else []
        self._notes.setPlainText("\n".join(notes))
        self._table.clearSelection()
        with filling(self._table):
            self._table.setRowCount(len(self._candidates))
            link = self.palette().link().color()
            for r, cand in enumerate(self._candidates):
                for col, text in enumerate(led_candidate_cells(cand)):
                    item = SortItem(text)
                    if col in (COL_LED_JLC_STOCK, COL_LED_DK_STOCK) and cand.stock_warning:
                        item.setBackground(CHECK_TINTS[UNVERIFIED])
                        item.setToolTip(cand.stock_warning)
                    if col in (COL_LED_LCSC, COL_LED_DIGIKEY) and self._url(cand, col):
                        item.setForeground(link)
                        item.setToolTip(self._url(cand, col))
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
    def _url(cand: LedCandidate, col: int) -> Optional[str]:
        if col == COL_LED_LCSC:
            return lcsc_url(cand.lcsc or "")
        if col == COL_LED_DIGIKEY:
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
        if col in (COL_LED_LCSC, COL_LED_DIGIKEY):
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

    def drop_candidate(self, candidate: LedCandidate) -> None:
        for r, c in enumerate(self._candidates):
            if c is candidate:
                del self._candidates[r]
                remove_data_row(self._table, r)
                break
        if self._result is not None:
            self._result = replace(self._result, candidates=[
                c for c in self._result.candidates if c is not candidate])
        self._selection_changed()

    def requirement_for(self, candidate: Optional[LedCandidate]) -> LedRequirement:
        """The requirement the candidate was found for."""
        if self._result is not None and self._result.requirement is not None:
            return self._result.requirement
        return self._req

    def accept_problem(self, candidate: LedCandidate) -> str:
        try:
            led_assignment_plan(self._group, self.requirement_for(candidate), candidate)
        except ValueError as exc:
            return str(exc)
        return ""

    def _selected(self) -> Optional[LedCandidate]:
        r = selected_data_row(self._table)
        return self._candidates[r] if 0 <= r < len(self._candidates) else None

    def stock_text(self) -> str:
        return self._stock.text()

    def _selection_changed(self) -> None:
        cand = self._selected()
        problem = self.accept_problem(cand) if cand is not None else ""
        self._accept_btn.setEnabled(cand is not None and not problem)
        self._accept_btn.setToolTip(problem)
        stock = (f"Stock is below the floor — check it before ordering: {cand.stock_warning}."
                 if cand is not None and cand.stock_warning else "")
        self._stock.setText(stock)
        self._stock.setVisible(bool(stock))

    def chosen(self) -> Optional[LedCandidate]:
        cand = self._selected()
        if cand is None or self.accept_problem(cand):
            return None
        return cand

    def accept(self) -> None:
        if self.chosen() is None:
            return
        super().accept()
