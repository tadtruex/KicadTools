from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from kicadtools.gui.lcsc import (  # noqa: E402
    STATUS_FLAGGED,
    STATUS_HAS,
    STATUS_NEEDS,
    STATUS_NONE,
    STATUS_RESOLVED,
    LcscRow,
    LcscTableModel,
)
from kicadtools.gui.model import NaturalSortProxyModel  # noqa: E402
from kicadtools.partnum import PartNumbers  # noqa: E402
from kicadtools.resolve import (  # noqa: E402
    PartRecord,
    Resolution,
    Status,
    iter_bom_part_records,
)

DATA = Path(__file__).parent / "data"
BOARD = DATA / "MotorDriver.kicad_sch"


def _rec(ref, **props):
    props.setdefault("Reference", ref)
    return PartRecord(ref, props, BOARD, f"uuid-{ref}")


def test_initial_status_classification():
    recs = [
        _rec("C1", LCSC="C541465"),
        _rec("R1", MPN="RMCF0603FT100K"),
        _rec("U1", **{"DigiKey PN": "P100-ND"}),
        _rec("J1", Value="conn"),
    ]
    m = LcscTableModel(recs)
    statuses = [m.data(m.index(r, 4), Qt.DisplayRole) for r in range(4)]
    assert statuses == [STATUS_HAS, STATUS_NEEDS, STATUS_NEEDS, STATUS_NONE]


def test_columns_show_part_numbers():
    m = LcscTableModel([_rec("R1", MPN="RMCF0603FT100K", **{"DigiKey PN": "P1-ND"})])
    assert m.headerData(0, Qt.Horizontal, Qt.DisplayRole) == "Ref"
    assert m.columnCount() == 5
    row = [m.data(m.index(0, c), Qt.DisplayRole) for c in range(5)]
    assert row[0] == "R1"          # Ref
    assert row[1] == ""            # LCSC (none yet)
    assert row[2] == "P1-ND"       # DigiKey
    assert row[3] == "RMCF0603FT100K"  # MPN


def test_apply_resolution_fills_lcsc_and_mpn():
    m = LcscTableModel([_rec("U1", **{"DigiKey PN": "P100-ND"})])
    res = Resolution(
        reference="U1",
        part=PartNumbers(digikey_pn="P100-ND"),
        status=Status.RESOLVED,
        entry="digikey",
        reason="LCSC C123 ← MPN ABC",
        source_file=BOARD,
        symbol_uuid="uuid-U1",
        lcsc="C123",
        resolved_mpn="ABC",
    )
    m.apply_resolution(0, res)
    assert m.data(m.index(0, 1), Qt.DisplayRole) == "C123"   # LCSC
    assert m.data(m.index(0, 3), Qt.DisplayRole) == "ABC"    # discovered MPN
    assert m.data(m.index(0, 4), Qt.DisplayRole) == STATUS_RESOLVED
    assert m.data(m.index(0, 4), Qt.ToolTipRole) == "LCSC C123 ← MPN ABC"


def test_apply_resolution_flagged_keeps_blank_lcsc():
    m = LcscTableModel([_rec("U1", **{"DigiKey PN": "bad-ND"})])
    res = Resolution(
        reference="U1", part=PartNumbers(digikey_pn="bad-ND"),
        status=Status.FLAGGED, entry="digikey",
        reason="no match", source_file=BOARD, symbol_uuid="uuid-U1",
    )
    m.apply_resolution(0, res)
    assert m.data(m.index(0, 1), Qt.DisplayRole) == ""
    assert m.data(m.index(0, 4), Qt.DisplayRole) == STATUS_FLAGGED


def test_loads_real_board_and_sorts_by_ref():
    m = LcscTableModel()
    m.load(iter_bom_part_records(BOARD))
    assert m.rowCount() > 0
    proxy = NaturalSortProxyModel()
    proxy.setSourceModel(m)
    proxy.sort(0, Qt.AscendingOrder)  # Ref column
    refs = [proxy.data(proxy.index(r, 0), Qt.DisplayRole) for r in range(proxy.rowCount())]
    caps = [r for r in refs if r.startswith("C")]
    if "C2" in caps and "C10" in caps:
        assert caps.index("C2") < caps.index("C10")
