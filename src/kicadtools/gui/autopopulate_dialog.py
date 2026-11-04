"""The Autopopulate dialog: parts-database matches for the open Parts-tab rows,
accepted several at a time (:mod:`kicadtools.autopopulate`).

One row per Parts-tab group that has a stored match: **Accept** first, then
the Parts tab's columns (Value · Package · Footprint · Rating · Class ·
Tolerance · Qty · References · Current part · Status), then **Match**:

- **Match**: a drop-down of the stored parts, best first (most uses, then
  the closest fit), each as ``MPN — manufacturer (N uses)``;
- **Accept**: a check box whose background gives the selected match's tier:
  green when exact, orange when it exceeds the requirement, red when it rests
  on a default or unset field or on a footprint alone. The tooltip says why.

Click a header to sort: Accept sorts by tier (exact first), Match by the
selected MPN, the others as on the Parts tab. Nothing is checked at first. *Check green* checks every exact match,
*Check all* every row, *Clear* none. *Accept checked* closes the dialog;
:meth:`AutopopulateDialog.chosen` gives ``(row, match)`` for each checked
row. No supplier is asked: stock and price are not checked.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from kicadtools.autopopulate import EXACT, NEAR, UNSURE, Match, RowMatches
from kicadtools.gui.parts import COL_MPN, PARTS_COLUMNS
from kicadtools.gui.sorting import SortItem, filling, make_sortable
from kicadtools.naturalsort import display_key

# The accept box first, then the Parts tab's columns without Chosen MPN, then
# the match.
_PARTS_COLS = [c for c in range(len(PARTS_COLUMNS)) if c != COL_MPN]
AUTO_COLUMNS = ["Accept"] + [PARTS_COLUMNS[c] for c in _PARTS_COLS] + ["Match"]
COL_ACCEPT = 0
_FIRST_PARTS_COL = 1
COL_MATCH = len(AUTO_COLUMNS) - 1

TIER_COLOURS: Dict[str, QColor] = {
    EXACT: QColor(60, 170, 70),
    NEAR: QColor(240, 150, 30),
    UNSURE: QColor(210, 50, 50),
}
TIER_TEXT = {EXACT: "exact", NEAR: "near", UNSURE: "less sure"}
_TIER_ORDER = {EXACT: 0, NEAR: 1, UNSURE: 2}


def match_text(m: Match) -> str:
    """The drop-down entry for *m*."""
    uses = f" ({m.usage_count} use{'s' * (m.usage_count != 1)})" if m.usage_count else ""
    return f"{m.mpn} — {m.manufacturer}{uses}"


def summary_text(rows: Sequence[RowMatches]) -> str:
    counts = {EXACT: 0, NEAR: 0, UNSURE: 0}
    for r in rows:
        counts[r.best.tier] += 1
    n = len(rows)
    return (f"{n} row{'s' * (n != 1)} with a stored part: {counts[EXACT]} exact, "
            f"{counts[NEAR]} near, {counts[UNSURE]} less sure (best match). "
            "Only the parts database was searched; stock and price are not checked.")


class AutopopulateDialog(QDialog):
    def __init__(self, model, rows: Sequence[RowMatches], parent=None) -> None:
        """*model* is the Parts tab's :class:`~kicadtools.gui.parts.PartsTableModel`
        (for the cell text); *rows* the matches from
        :func:`kicadtools.autopopulate.find_matches`."""
        super().__init__(parent)
        self.setWindowTitle("Autopopulate from the parts database")
        self.resize(1400, 600)
        self._rows = list(rows)
        self._combos: List[QComboBox] = []
        self._boxes: List[QCheckBox] = []
        self._holders: List[QWidget] = []
        # The cells under the Accept and Match widgets, which hold their sort keys.
        self._accept_items: List[SortItem] = []
        self._match_items: List[SortItem] = []

        self._table = QTableWidget(len(self._rows), len(AUTO_COLUMNS))
        self._table.setHorizontalHeaderLabels(AUTO_COLUMNS)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        header = self._table.horizontalHeaderItem(COL_ACCEPT)
        header.setToolTip("Green: exact. Orange: exceeds the requirement. "
                          "Red: rests on a default or unset field, or on the footprint alone.")
        make_sortable(self._table)
        with filling(self._table):
            for i, rm in enumerate(self._rows):
                self._fill_row(model, i, rm)

        self._summary = QLabel(summary_text(self._rows))
        self._summary.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        green = buttons.addButton("Check green", QDialogButtonBox.ActionRole)
        green.clicked.connect(self.check_exact)
        every = buttons.addButton("Check all", QDialogButtonBox.ActionRole)
        every.clicked.connect(lambda: self._set_all(True))
        clear = buttons.addButton("Clear", QDialogButtonBox.ActionRole)
        clear.clicked.connect(lambda: self._set_all(False))
        self._accept_btn = buttons.addButton("Accept checked", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        layout.addWidget(self._table, 1)
        layout.addWidget(buttons)

    def _fill_row(self, model, i: int, rm: RowMatches) -> None:
        for out_col, col in enumerate(_PARTS_COLS):
            item = SortItem(model.cell_text(rm.row, col))
            tip = model.data(model.index(rm.row, col), Qt.ToolTipRole)
            if tip:
                item.setToolTip(str(tip))
            self._table.setItem(i, _FIRST_PARTS_COL + out_col, item)
        accept_item, match_item = SortItem(), SortItem()
        self._table.setItem(i, COL_ACCEPT, accept_item)
        self._table.setItem(i, COL_MATCH, match_item)
        self._accept_items.append(accept_item)
        self._match_items.append(match_item)
        combo = QComboBox()
        for m in rm.matches:
            combo.addItem(match_text(m))
        combo.currentIndexChanged.connect(lambda _=0, i=i: self._paint(i))
        self._table.setCellWidget(i, COL_MATCH, combo)
        self._combos.append(combo)
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(6, 0, 6, 0)
        box = QCheckBox(TIER_TEXT[rm.best.tier])
        lay.addWidget(box)
        holder.setAutoFillBackground(True)
        self._table.setCellWidget(i, COL_ACCEPT, holder)
        self._boxes.append(box)
        self._holders.append(holder)
        self._paint(i)

    # ---- state -------------------------------------------------------
    def selected_match(self, i: int) -> Match:
        return self._rows[i].matches[max(0, self._combos[i].currentIndex())]

    def _paint(self, i: int) -> None:
        m = self.selected_match(i)
        holder, box = self._holders[i], self._boxes[i]
        colour = TIER_COLOURS[m.tier]
        holder.setStyleSheet(f"background-color: {colour.name()};")
        box.setText(TIER_TEXT[m.tier])
        tip = f"{TIER_TEXT[m.tier].capitalize()}: {m.why}."
        holder.setToolTip(tip)
        box.setToolTip(tip)
        # Read at the next header click; a changed match does not move the row.
        self._accept_items[i].set_key((_TIER_ORDER[m.tier],) + display_key(m.mpn))
        self._match_items[i].set_key(display_key(m.mpn))

    def tier_at(self, i: int) -> str:
        return self.selected_match(i).tier

    def set_checked(self, i: int, on: bool) -> None:
        self._boxes[i].setChecked(on)

    def check_exact(self) -> None:
        for i in range(len(self._rows)):
            if self.tier_at(i) == EXACT:
                self._boxes[i].setChecked(True)

    def _set_all(self, on: bool) -> None:
        for box in self._boxes:
            box.setChecked(on)

    def chosen(self) -> List[Tuple[int, Match]]:
        """``(Parts-tab row, match)`` for each checked row."""
        return [(rm.row, self.selected_match(i)) for i, rm in enumerate(self._rows)
                if self._boxes[i].isChecked()]
