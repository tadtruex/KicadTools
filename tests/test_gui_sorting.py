"""Tests for sortable dialog tables (gui/sorting.py) and the shared cell sort
key (naturalsort.display_key): every table sorts on a header click, and a
dialog's selection, clicks and rejections still name the right candidate
after a sort."""
from __future__ import annotations

import pytest

from kicadtools.naturalsort import display_key


@pytest.mark.parametrize("low, high", [
    ("987", "1,234"),          # thousands commas
    ("$0.12", "$0.5"),         # prices by value
    ("100nF", "10µF"),         # SI prefixes
    ("4.7k", "10k"),
    ("10k", "1M"),
    ("6.3V", "25V"),
    ("10%", "20%"),
    ("C2", "C10"),             # natural order for text
    ("1N4148", "1N5819"),
    ("99", "abc"),             # numbers ahead of text
])
def test_display_key_order(low, high):
    assert display_key(low) < display_key(high)


pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from tests.test_gui_tht import _app, _cand, _group  # noqa: E402


def test_tht_dialog_sorted_by_stock_keeps_selection_and_reject():
    _app()
    from kicadtools.gui.tht_assign_dialog import COL_THT_STOCK, ThtAssignDialog
    from kicadtools.thtassign import ThtSearchResult
    g = _group()
    dlg = ThtAssignDialog(g, 1, series="")
    cands = [_cand("A-BIG", stock=12000), _cand("B-SMALL", stock=900), _cand("C-MID", stock=5000)]
    dlg.show_result(ThtSearchResult(requirement=g.requirement, candidates=cands), dlg.request_id)
    table = dlg._table
    assert table.isSortingEnabled()
    # Fill order until a header is clicked; the first candidate is selected.
    assert [table.item(r, 0).text() for r in range(3)] == ["A-BIG", "B-SMALL", "C-MID"]
    assert dlg.chosen().mpn == "A-BIG"
    table.sortByColumn(COL_THT_STOCK, Qt.AscendingOrder)
    assert [table.item(r, 0).text() for r in range(3)] == ["B-SMALL", "C-MID", "A-BIG"]
    assert dlg.chosen().mpn == "A-BIG"           # the selection follows its row
    table.selectRow(0)
    assert dlg.chosen().mpn == "B-SMALL"
    dlg.drop_candidate(dlg.chosen())
    assert [table.item(r, 0).text() for r in range(2)] == ["C-MID", "A-BIG"]
    table.selectRow(1)
    assert dlg.chosen().mpn == "A-BIG"
    # A new result keeps the user's sort and selects the best candidate.
    dlg.show_result(ThtSearchResult(requirement=g.requirement, candidates=cands), dlg.request_id)
    assert [table.item(r, 0).text() for r in range(3)] == ["B-SMALL", "C-MID", "A-BIG"]
    assert dlg.chosen().mpn == "A-BIG"


def test_autopopulate_sorted_by_accept_tier():
    _app()
    from kicadtools.autopopulate import EXACT, NEAR, find_matches
    from kicadtools.gui.autopopulate_dialog import COL_ACCEPT, AutopopulateDialog
    from kicadtools.gui.parts import PartsTableModel
    from kicadtools.partsdb import PartsDb
    from kicadtools.requirement import group_parts
    from tests.test_autopopulate import DEFAULTS, STATED, _db_cap, _rec
    db = PartsDb()
    _db_cap(db, "A-EXACT", volts=16.0)
    _db_cap(db, "B-NEAR", volts=50.0, lcsc="C2")
    groups = group_parts([_rec("C1", **{**STATED, "Voltage:Min": "35V"}),
                          _rec("C2", **STATED)], DEFAULTS)
    rows = find_matches(groups, db, open_rows=[0, 1])
    dlg = AutopopulateDialog(PartsTableModel(groups), rows)
    assert [dlg.tier_at(i) for i in range(2)] == [NEAR, EXACT]
    table = dlg._table
    table.sortByColumn(COL_ACCEPT, Qt.AscendingOrder)
    # The exact row is now on top, with its own check box and drop-down.
    top_box = table.cellWidget(0, COL_ACCEPT).findChild(type(dlg._boxes[0]))
    assert top_box is dlg._boxes[1]
    top_box.setChecked(True)
    assert [(r, m.mpn, m.tier) for r, m in dlg.chosen()] == [(rows[1].row, "A-EXACT", EXACT)]
