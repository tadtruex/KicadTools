from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from kicadtools.components import get_components  # noqa: E402
from kicadtools.gui.model import IntentTableModel  # noqa: E402

DATA = Path(__file__).parent / "data"
BOARD = DATA / "MotorDriver.kicad_sch"


def _model() -> IntentTableModel:
    m = IntentTableModel()
    m.load(get_components(BOARD))
    return m


def test_dimensions_and_headers():
    m = _model()
    assert m.rowCount() > 0
    assert m.columnCount() == 6
    assert m.headerData(1, Qt.Horizontal, Qt.DisplayRole) == "Ref"


def test_only_guessed_rows_are_checkable():
    m = _model()
    for r in range(m.rowCount()):
        checkable = bool(m.flags(m.index(r, 0)) & Qt.ItemIsUserCheckable)
        assert checkable == (m.status(r) == "guessed")


def test_toggle_check_then_accept_checked():
    m = _model()
    row = next(r for r in range(m.rowCount()) if m.status(r) == "guessed")
    idx = m.index(row, 0)

    assert m.setData(idx, Qt.Checked, Qt.CheckStateRole) is True
    assert m.data(idx, Qt.CheckStateRole) == Qt.Checked

    m.accept_checked()
    assert m.status(row) == "accepted"
    # An accepted row is no longer checkable and no longer offers a check state.
    assert not (m.flags(idx) & Qt.ItemIsUserCheckable)
    assert m.data(idx, Qt.CheckStateRole) is None


def test_accept_all_clears_guesses_and_builds_edits():
    m = _model()
    guessed_before = m.summary()["guessed"]
    assert guessed_before > 0

    m.accept_all()
    s = m.summary()
    assert s["guessed"] == 0
    assert s["accepted"] == guessed_before

    edits = m.accepted_edits()
    assert edits  # grouped by source file
    assert all(mapping for mapping in edits.values())
    total = sum(len(mapping) for mapping in edits.values())
    # edits dedup by (file, uuid), so may be <= accepted rows (reused subsheets).
    assert 0 < total <= guessed_before


def test_setdata_rejected_on_non_guessed_row():
    m = _model()
    row = next(r for r in range(m.rowCount()) if m.status(r) != "guessed")
    assert m.setData(m.index(row, 0), Qt.Checked, Qt.CheckStateRole) is False
