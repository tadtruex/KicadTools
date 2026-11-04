"""Sortable dialog tables: click a header to sort, as on the main window's tabs.

A :class:`~PySide6.QtWidgets.QTableWidget` reorders its rows when sorted, so
a dialog that keeps its candidates in a list maps between the row on screen
(the *view row*) and the list index (the *data row*):

- fill the table inside :func:`filling`, in list order, with
  :class:`SortItem` cells; on exit each row's first cell holds its list index
  and the table is sorted again by the column the user picked, if any;
- :func:`data_row` gives the list index of a view row (for click, double-click
  and selection handlers), :func:`view_row` the reverse;
- :func:`remove_data_row` removes a list index's row and renumbers the rest.

Cells sort by :func:`kicadtools.naturalsort.display_key` of their text
(numbers by value, then natural order), or by a key set with
:meth:`SortItem.set_key` (for a cell under a widget).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem

from kicadtools.naturalsort import display_key

ROW_ROLE = Qt.UserRole + 64


def _key(item: Any) -> tuple:
    key = getattr(item, "_sort_key", None)
    return key if key is not None else display_key(item.text())


class SortItem(QTableWidgetItem):
    """A table cell that sorts by :func:`display_key` of its text, or by the
    key given to :meth:`set_key`."""

    def __init__(self, text: str = "", key: Optional[tuple] = None) -> None:
        super().__init__(text)
        self._sort_key = key

    def set_key(self, key: Optional[tuple]) -> None:
        self._sort_key = key

    def __lt__(self, other: QTableWidgetItem) -> bool:
        return _key(self) < _key(other)


class SortTreeItem(QTreeWidgetItem):
    """A tree row that sorts by :func:`display_key` of the sort column's
    text."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        return display_key(self.text(col)) < display_key(other.text(col))


def make_sortable(view: Any) -> None:
    """Enable header-click sorting on a table or tree, keeping the fill order
    until a header is clicked."""
    view.setSortingEnabled(True)
    header = view.horizontalHeader() if isinstance(view, QTableWidget) else view.header()
    header.setSortIndicator(-1, Qt.AscendingOrder)


@contextmanager
def filling(view: Any) -> Iterator[None]:
    """Fill *view* in data order inside this block. Sorting is off while the
    rows are set; on exit each table row's first cell holds its data row and
    the view is sorted again by the user's column, if any."""
    view.setSortingEnabled(False)
    try:
        yield
    finally:
        if isinstance(view, QTableWidget):
            for r in range(view.rowCount()):
                item = view.item(r, 0)
                if item is not None:
                    item.setData(ROW_ROLE, r)
        view.setSortingEnabled(True)


def data_row(table: QTableWidget, row: int) -> int:
    """The data row shown at view row *row* (``-1`` when out of range)."""
    if not 0 <= row < table.rowCount():
        return -1
    item = table.item(row, 0)
    value = item.data(ROW_ROLE) if item is not None else None
    return row if value is None else int(value)


def view_row(table: QTableWidget, index: int) -> int:
    """The view row showing data row *index* (``-1`` when none does)."""
    for r in range(table.rowCount()):
        if data_row(table, r) == index:
            return r
    return -1


def remove_data_row(table: QTableWidget, index: int) -> None:
    """Remove data row *index* and renumber the data rows after it."""
    r = view_row(table, index)
    if r < 0:
        return
    table.removeRow(r)
    for v in range(table.rowCount()):
        item = table.item(v, 0)
        value = item.data(ROW_ROLE) if item is not None else None
        if value is not None and int(value) > index:
            item.setData(ROW_ROLE, int(value) - 1)


def selected_data_row(table: QTableWidget) -> int:
    """The data row of the single selected row, else ``-1``."""
    rows = table.selectionModel().selectedRows()
    return data_row(table, rows[0].row()) if len(rows) == 1 else -1
