"""A Qt table model over a list of :class:`ElectricalIntent` components.

Mirrors the `kicadtools bom` listing (Ref · Status · Type · Footprint ·
Description) and adds an Accept checkbox on each *guessed* row. Accepting a row
records it for write-back and flips its status to a checkmark.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)

from kicadtools.intent import ElectricalIntent
from kicadtools.naturalsort import display_key, natural_key  # natural_key re-exported


def _check_rank(state) -> int:
    """A check box's sort rank: checked 0, partly checked 1, unchecked 2, no
    check box 3."""
    if state is None:
        return 3
    value = state.value if hasattr(state, "value") else int(state)
    return {Qt.Checked.value: 0, Qt.PartiallyChecked.value: 1}.get(value, 2)


class NaturalSortProxyModel(QSortFilterProxyModel):
    """Proxy that sorts by a cell's check box (checked, then unchecked, then
    none), then by :func:`~kicadtools.naturalsort.display_key` of its display
    text (numbers by value, then natural order), as the dialog tables do
    (:mod:`kicadtools.gui.sorting`)."""

    def _key(self, index: QModelIndex) -> tuple:
        model = self.sourceModel()
        return (_check_rank(model.data(index, Qt.CheckStateRole)),
                display_key(model.data(index, Qt.DisplayRole)))

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        return self._key(left) < self._key(right)

_COLUMNS = ["Accept", "Ref", "Status", "Type", "Footprint", "Description"]

_STATUS_TEXT = {
    "typed": "✅ typed",
    "guessed": "≈ guessed",
    "accepted": "✔ accepted",
    "unclassified": "❌ —",
}


class IntentTableModel(QAbstractTableModel):
    def __init__(self, components: Optional[Sequence[ElectricalIntent]] = None):
        super().__init__()
        self._c: List[ElectricalIntent] = list(components or [])
        self._checked: Set[int] = set()
        self._accepted: Set[int] = set()

    # ---- loading -----------------------------------------------------
    def load(self, components: Sequence[ElectricalIntent]) -> None:
        self.beginResetModel()
        self._c = list(components)
        self._checked.clear()
        self._accepted.clear()
        self.endResetModel()

    def component_at(self, row: int) -> ElectricalIntent:
        """The intent object backing *row* (a source-model row index)."""
        return self._c[row]

    # ---- status ------------------------------------------------------
    def status(self, row: int) -> str:
        if row in self._accepted:
            return "accepted"
        c = self._c[row]
        if type(c) is ElectricalIntent:
            return "unclassified"
        if c.intent_guessed:
            return "guessed"
        return "typed"

    def _checkable(self, row: int) -> bool:
        return self.status(row) == "guessed"

    # ---- Qt dimensions ----------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._c)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _COLUMNS[section]
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0 and self._checkable(index.row()):
            f |= Qt.ItemIsUserCheckable
        return f

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        c = self._c[row]

        if role == Qt.CheckStateRole and col == 0:
            if not self._checkable(row):
                return None
            return Qt.Checked if row in self._checked else Qt.Unchecked

        if role == Qt.ToolTipRole:
            return c.guess_basis or c.classification_note or None

        if role == Qt.DisplayRole:
            if col == 1:
                return c.reference
            if col == 2:
                return _STATUS_TEXT[self.status(row)]
            if col == 3:
                return "" if type(c) is ElectricalIntent else c.intent_type
            if col == 4:
                return c.footprint or ""
            if col == 5:
                return c.description()
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if role == Qt.CheckStateRole and index.column() == 0 and self._checkable(index.row()):
            row = index.row()
            # The view passes an int; tests may pass a Qt.CheckState. Normalise.
            state = value if isinstance(value, Qt.CheckState) else Qt.CheckState(value)
            if state == Qt.Checked:
                self._checked.add(row)
            else:
                self._checked.discard(row)
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
            return True
        return False

    # ---- actions -----------------------------------------------------
    def _guessed_rows(self) -> List[int]:
        return [r for r in range(len(self._c)) if self.status(r) == "guessed"]

    def accept_checked(self) -> None:
        self._accept({r for r in self._checked if self.status(r) == "guessed"})

    def accept_all(self) -> None:
        self._accept(set(self._guessed_rows()))

    def _accept(self, rows: Set[int]) -> None:
        if not rows:
            return
        self._accepted |= rows
        self._checked -= rows
        top = self.index(min(rows), 0)
        bottom = self.index(max(rows), self.columnCount() - 1)
        self.dataChanged.emit(top, bottom)

    def accepted_edits(self) -> Dict[Path, Dict[str, str]]:
        """Group accepted rows into ``{source_file: {symbol_uuid: intent_type}}``
        for :func:`kicadtools.writer.write_back`."""
        edits: Dict[Path, Dict[str, str]] = defaultdict(dict)
        for row in self._accepted:
            c = self._c[row]
            if c.source_file and c.symbol_uuid:
                edits[Path(c.source_file)][c.symbol_uuid] = c.intent_type
        return dict(edits)

    def summary(self) -> Dict[str, int]:
        counts = {"typed": 0, "guessed": 0, "accepted": 0, "unclassified": 0}
        for row in range(len(self._c)):
            counts[self.status(row)] += 1
        return counts
