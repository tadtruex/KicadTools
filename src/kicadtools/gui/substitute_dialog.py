"""The "Find Parametrically Equivalent Substitute" dialog.

Opens immediately in a *searching* state; the window's worker thread delivers a
:class:`~kicadtools.substitute.SubstituteSearch` (``show_result``) or an error
(``show_error``). It shows the spec the search used — each requirement and
where it came from — then the matching in-stock JLCPCB parts, best first.
Choosing one and pressing *Use Selected* accepts the dialog; :meth:`chosen`
returns the pick.

The cell text is computed by the pure :func:`candidate_cells` so it can be
tested without a window.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QVBoxLayout,
)

from kicadtools.gui.sorting import (
    SortItem,
    data_row,
    filling,
    make_sortable,
    selected_data_row,
    view_row,
)
from kicadtools.substitute import Candidate, SubstituteSearch
from kicadtools.suppliers import JlcPart, lcsc_url

_FIXED_COLUMNS = ["LCSC", "MPN", "Manufacturer", "Library", "Stock", "Unit price"]
_LOW_TINT = QColor(230, 160, 0, 70)


def describe_part(part: Optional[JlcPart]) -> str:
    """One line about the current part: code, MPN, stock, and why JLCPCB won't
    sell it, when it won't."""
    if part is None:
        return "not found on JLCPCB"
    text = f"{part.lcsc} {part.mpn} — stock {part.stock:,}"
    if part.no_buy_reason:
        text += f" ({part.no_buy_reason})"
    return text


def candidate_cells(cand: Candidate, param_labels: List[str]) -> List[str]:
    """The table row for *cand*: the fixed columns, then the candidate's value
    for each constrained parameter in *param_labels*."""
    p = cand.part
    price = "" if p.unit_price == float("inf") else f"${p.unit_price:.4f}"
    have = {c.label: c.have for c in cand.checks}
    return [
        p.lcsc,
        p.mpn,
        p.manufacturer,
        "Basic" if p.is_basic else "Extended",
        f"{p.stock:,}",
        price,
    ] + [have.get(label, "") for label in param_labels]


class SubstituteDialog(QDialog):
    def __init__(
        self,
        references: List[str],
        current: str,
        needed: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Find Substitute — {', '.join(references)}")
        self.resize(900, 480)
        self._needed = needed
        self._candidates: List[Candidate] = []

        head = QFormLayout()
        head.addRow("Replaces", QLabel(", ".join(references)))
        head.addRow("Current part", QLabel(current))
        head.addRow("Build needs", QLabel(f"{needed:,}"))
        self._spec_form = QFormLayout()

        self._status = QLabel("Searching JLCPCB…")
        self._status.setWordWrap(True)

        self._table = QTableWidget(0, 0)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._selection_changed)
        self._table.cellDoubleClicked.connect(self._double_clicked)
        self._table.setMouseTracking(True)
        make_sortable(self._table)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._use = self._buttons.addButton("Use Selected", QDialogButtonBox.AcceptRole)
        self._use.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addLayout(self._spec_form)
        layout.addWidget(self._status)
        layout.addWidget(self._table)
        layout.addWidget(self._buttons)

    # ---- results -----------------------------------------------------
    def show_result(self, result: SubstituteSearch) -> None:
        spec = result.spec
        if spec.package:
            self._spec_form.addRow("Package", QLabel(f"{spec.package}  ({spec.package_source})"))
        for c in spec.constraints:
            self._spec_form.addRow(c.label, QLabel(f"{c.shown}  ({c.source})"))

        if not spec.searchable:
            self._status.setText(f"Can't search: {spec.problem()}.")
            return

        self._candidates = result.candidates
        if not self._candidates:
            self._status.setText(
                f"No in-stock JLCPCB part matched ({result.examined} search results checked)."
            )
            return
        enough = sum(1 for c in self._candidates if c.part.stock >= self._needed)
        self._status.setText(
            f"{len(self._candidates)} matching part(s) from {result.examined} search "
            f"results; {enough} with enough stock for the build. Best first: "
            f"enough stock, then Basic, then price. Click an LCSC code to open it."
        )

        labels = [c.label for c in spec.constraints]
        headers = _FIXED_COLUMNS + labels
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        with filling(self._table):
            self._table.setRowCount(len(self._candidates))
            for r, cand in enumerate(self._candidates):
                short = cand.part.stock < self._needed
                for col, text in enumerate(candidate_cells(cand, labels)):
                    item = SortItem(text)
                    if short:
                        item.setBackground(_LOW_TINT)
                        item.setToolTip(f"Only {cand.part.stock:,} in stock; the build needs {self._needed:,}.")
                    if col == 0:
                        item.setForeground(self.palette().link().color())
                        item.setToolTip(lcsc_url(cand.part.lcsc) or "")
                    self._table.setItem(r, col, item)
        self._table.cellClicked.connect(self._cell_clicked)
        self._table.selectRow(view_row(self._table, 0))

    def show_error(self, message: str) -> None:
        self._status.setText(f"Search failed: {message}")

    # ---- selection ---------------------------------------------------
    def _selection_changed(self) -> None:
        self._use.setEnabled(self.chosen() is not None)

    def _double_clicked(self, row: int, _col: int) -> None:
        row = data_row(self._table, row)
        if 0 <= row < len(self._candidates):
            self.accept()

    def _cell_clicked(self, row: int, col: int) -> None:
        row = data_row(self._table, row)
        if col == 0 and 0 <= row < len(self._candidates):
            url = lcsc_url(self._candidates[row].part.lcsc)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def chosen(self) -> Optional[Candidate]:
        r = selected_data_row(self._table)
        return self._candidates[r] if 0 <= r < len(self._candidates) else None
