from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from kicadtools.gui.lcsc import (  # noqa: E402
    LINK_ROLE,
    STATUS_ACCEPTED,
    STATUS_FLAGGED,
    STATUS_HAS,
    STATUS_NEEDS,
    STATUS_NONE,
    STATUS_RESOLVED,
    LcscRow,
    LcscTableModel,
    _COL_ROTATION,
)
from kicadtools.partrotation import PartRotationStore  # noqa: E402
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


def _resolution(ref, uuid, status, *, lcsc=None, resolved_mpn=None, reason="", dk="P-ND"):
    return Resolution(
        reference=ref,
        part=PartNumbers(digikey_pn=dk),
        status=status,
        entry="digikey",
        reason=reason,
        source_file=BOARD,
        symbol_uuid=uuid,
        lcsc=lcsc,
        resolved_mpn=resolved_mpn,
    )


def test_initial_status_classification():
    recs = [
        _rec("C1", LCSC="C541465"),
        _rec("R1", MPN="RMCF0603FT100K"),
        _rec("U1", **{"DigiKey PN": "P100-ND"}),
        _rec("J1", Value="conn"),
    ]
    m = LcscTableModel(recs)
    statuses = [m.data(m.index(r, 5), Qt.DisplayRole) for r in range(4)]
    assert statuses == [STATUS_HAS, STATUS_NEEDS, STATUS_NEEDS, STATUS_NONE]


def test_columns_show_part_numbers():
    m = LcscTableModel([_rec("R1", Description="10k 0603 resistor",
                             MPN="RMCF0603FT100K", **{"DigiKey PN": "P1-ND"})])
    assert m.headerData(0, Qt.Horizontal, Qt.DisplayRole) == "Accept"
    assert m.headerData(1, Qt.Horizontal, Qt.DisplayRole) == "Ref"
    assert m.headerData(6, Qt.Horizontal, Qt.DisplayRole) == "Rotation"
    assert m.headerData(7, Qt.Horizontal, Qt.DisplayRole) == "Description"  # rightmost
    assert m.columnCount() == 8
    row = [m.data(m.index(0, c), Qt.DisplayRole) for c in range(8)]
    assert row[0] == ""            # Accept (checkbox, no text)
    assert row[1] == "R1"          # Ref
    assert row[2] == ""            # LCSC (none yet)
    assert row[3] == "P1-ND"       # DigiKey
    assert row[4] == "RMCF0603FT100K"  # MPN
    assert row[7] == "10k 0603 resistor"  # Description (trailing column)


def test_description_populated_and_tooltip():
    long_desc = "Generic connector, single row, 01x02, script generated"
    m = LcscTableModel([_rec("J1", Description=long_desc)])
    assert m.data(m.index(0, 7), Qt.DisplayRole) == long_desc
    assert m.data(m.index(0, 7), Qt.ToolTipRole) == long_desc  # full text on hover
    # A part without a Description shows (and hovers) nothing.
    m2 = LcscTableModel([_rec("R9")])
    assert m2.data(m2.index(0, 7), Qt.DisplayRole) == ""
    assert m2.data(m2.index(0, 7), Qt.ToolTipRole) is None


def test_apply_resolution_fills_lcsc_and_mpn():
    m = LcscTableModel([_rec("U1", **{"DigiKey PN": "P100-ND"})])
    res = _resolution(
        "U1", "uuid-U1", Status.RESOLVED,
        lcsc="C123", resolved_mpn="ABC", reason="LCSC C123 ← MPN ABC", dk="P100-ND",
    )
    m.apply_resolution(0, res)
    assert m.data(m.index(0, 2), Qt.DisplayRole) == "C123"   # LCSC
    assert m.data(m.index(0, 4), Qt.DisplayRole) == "ABC"    # discovered MPN
    assert m.data(m.index(0, 5), Qt.DisplayRole) == STATUS_RESOLVED
    assert m.data(m.index(0, 5), Qt.ToolTipRole) == "LCSC C123 ← MPN ABC"


def test_rotation_not_editable_without_lcsc():
    m = LcscTableModel([_rec("R9")])  # no LCSC part
    idx = m.index(0, _COL_ROTATION)
    flags = m.flags(idx)
    assert not (flags & Qt.ItemIsEditable)
    assert m.data(idx, Qt.DisplayRole) == "—"  # locked, with a hint
    assert m.data(idx, Qt.ToolTipRole) == "Run Resolve LCSC first to set a rotation for this part."
    assert m.data(idx, Qt.EditRole) is None


def test_rotation_becomes_editable_after_resolution_lands_lcsc():
    m = LcscTableModel([_rec("Q1", **{"DigiKey PN": "T2N7002AKLMCT-ND"})])
    idx = m.index(0, _COL_ROTATION)
    assert not (m.flags(idx) & Qt.ItemIsEditable)  # DigiKey PN only -> locked
    m.apply_resolution(0, _resolution("Q1", "uuid-Q1", Status.RESOLVED, lcsc="C8545"))
    assert m.flags(idx) & Qt.ItemIsEditable  # C-number landed -> now editable
    assert m.data(idx, Qt.DisplayRole) == "0°"


def test_rotation_editable_and_persists_to_store():
    store = PartRotationStore(":memory:")
    m = LcscTableModel([_rec("Q1", LCSC="C1")], store=store)
    idx = m.index(0, _COL_ROTATION)
    assert m.flags(idx) & Qt.ItemIsEditable
    assert m.data(idx, Qt.DisplayRole) == "0°"  # default, no override yet
    assert m.setData(idx, 180.0, Qt.EditRole) is True
    assert store.get("C1") == 180.0
    assert m.data(idx, Qt.DisplayRole) == "180°"
    assert m.data(idx, Qt.EditRole) == 180.0


def test_rotation_reflects_existing_store_value():
    store = PartRotationStore(":memory:")
    store.set("C1", 90.0)
    m = LcscTableModel([_rec("Q1", LCSC="C1")], store=store)
    assert m.data(m.index(0, _COL_ROTATION), Qt.DisplayRole) == "90°"


def test_rotation_propagates_to_rows_sharing_the_lcsc():
    store = PartRotationStore(":memory:")
    m = LcscTableModel([_rec("Q1", LCSC="C1"), _rec("Q2", LCSC="C1"),
                        _rec("R1", LCSC="C2")], store=store)
    m.setData(m.index(0, _COL_ROTATION), 270.0, Qt.EditRole)
    # Both C1 parts follow; the C2 part is untouched.
    assert m.data(m.index(0, _COL_ROTATION), Qt.DisplayRole) == "270°"
    assert m.data(m.index(1, _COL_ROTATION), Qt.DisplayRole) == "270°"
    assert m.data(m.index(2, _COL_ROTATION), Qt.DisplayRole) == "0°"


def test_rotation_zero_clears_override():
    store = PartRotationStore(":memory:")
    m = LcscTableModel([_rec("Q1", LCSC="C1")], store=store)
    m.setData(m.index(0, _COL_ROTATION), 180.0, Qt.EditRole)
    m.setData(m.index(0, _COL_ROTATION), 0.0, Qt.EditRole)
    assert store.get("C1") is None
    assert m.data(m.index(0, _COL_ROTATION), Qt.DisplayRole) == "0°"


def test_rotation_overrides_map_for_export():
    store = PartRotationStore(":memory:")
    m = LcscTableModel([_rec("Q1", LCSC="C1"), _rec("Q2", LCSC="C1"),
                        _rec("R1", LCSC="C2")], store=store)
    m.setData(m.index(0, _COL_ROTATION), 180.0, Qt.EditRole)
    assert m.rotation_overrides() == {"Q1": 180.0, "Q2": 180.0}


def test_apply_resolution_flagged_keeps_blank_lcsc():
    m = LcscTableModel([_rec("U1", **{"DigiKey PN": "bad-ND"})])
    res = _resolution("U1", "uuid-U1", Status.FLAGGED, reason="no match", dk="bad-ND")
    m.apply_resolution(0, res)
    assert m.data(m.index(0, 2), Qt.DisplayRole) == ""
    assert m.data(m.index(0, 5), Qt.DisplayRole) == STATUS_FLAGGED


# ---- accept / write-back flow ---------------------------------------

def test_only_resolved_rows_become_checkable():
    m = LcscTableModel([
        _rec("C1", LCSC="C541465"),                    # already has → OK
        _rec("R1", MPN="RMCF0603FT100K"),              # will resolve
        _rec("U1", **{"DigiKey PN": "bad-ND"}),        # will flag
    ])
    accept = lambda r: bool(m.flags(m.index(r, 0)) & Qt.ItemIsUserCheckable)
    # Nothing is checkable before resolution.
    assert [accept(r) for r in range(3)] == [False, False, False]

    m.apply_resolution(0, _resolution("C1", "uuid-C1", Status.OK, lcsc="C541465"))
    m.apply_resolution(1, _resolution("R1", "uuid-R1", Status.RESOLVED, lcsc="C111"))
    m.apply_resolution(2, _resolution("U1", "uuid-U1", Status.FLAGGED))
    # Only the RESOLVED row (with a new value) is checkable.
    assert [accept(r) for r in range(3)] == [False, True, False]


def test_check_accept_and_edits():
    m = LcscTableModel([
        _rec("R1", MPN="RMCF0603FT100K"),
        _rec("U1", **{"DigiKey PN": "P100-ND"}),
    ])
    m.apply_resolution(0, _resolution("R1", "uuid-R1", Status.RESOLVED, lcsc="C111"))
    m.apply_resolution(
        1, _resolution("U1", "uuid-U1", Status.RESOLVED, lcsc="C222",
                       resolved_mpn="MPN-U1", dk="P100-ND"),
    )

    # Check R1 only, accept the checked row.
    assert m.setData(m.index(0, 0), Qt.Checked, Qt.CheckStateRole) is True
    m.accept_checked()
    assert m.data(m.index(0, 5), Qt.DisplayRole) == STATUS_ACCEPTED
    # An accepted row is no longer checkable / has no check state.
    assert not (m.flags(m.index(0, 0)) & Qt.ItemIsUserCheckable)
    assert m.data(m.index(0, 0), Qt.CheckStateRole) is None

    edits = m.accepted_edits()
    assert edits == {BOARD: {"uuid-R1": {"LCSC": "C111"}}}

    # Accept the rest; U1 discovered an MPN it lacked, so both keys are written.
    m.accept_all()
    assert m.accepted_edits() == {
        BOARD: {
            "uuid-R1": {"LCSC": "C111"},
            "uuid-U1": {"LCSC": "C222", "MPN": "MPN-U1"},
        }
    }


def test_discovered_mpn_not_written_when_symbol_already_had_one():
    # U1 already carries an MPN, so resolution's resolved_mpn isn't a new value.
    m = LcscTableModel([_rec("U1", MPN="EXISTING-MPN")])
    m.apply_resolution(
        0, _resolution("U1", "uuid-U1", Status.RESOLVED, lcsc="C222",
                       resolved_mpn="EXISTING-MPN"),
    )
    m.accept_all()
    assert m.accepted_edits() == {BOARD: {"uuid-U1": {"LCSC": "C222"}}}


# ---- clickable LCSC / DigiKey links ---------------------------------

def test_link_role_gives_supplier_urls():
    m = LcscTableModel([_rec("C1", LCSC="C541465", **{"DigiKey PN": "P100-ND"})])
    lcsc_link = m.data(m.index(0, 2), LINK_ROLE)      # LCSC column
    digikey_link = m.data(m.index(0, 3), LINK_ROLE)   # DigiKey column
    assert lcsc_link == "https://www.lcsc.com/search?q=C541465"
    assert digikey_link == "https://www.digikey.com/en/products/result?keywords=P100-ND"
    # Non-link columns (Ref, Status) carry no URL.
    assert m.data(m.index(0, 1), LINK_ROLE) is None
    assert m.data(m.index(0, 5), LINK_ROLE) is None


def test_empty_cells_are_not_links():
    m = LcscTableModel([_rec("R1", MPN="RMCF0603FT100K")])  # no LCSC, no DigiKey
    assert m.data(m.index(0, 2), LINK_ROLE) is None  # empty LCSC
    assert m.data(m.index(0, 3), LINK_ROLE) is None  # empty DigiKey
    # No link styling on an empty cell.
    assert m.data(m.index(0, 2), Qt.FontRole) is None
    assert m.data(m.index(0, 2), Qt.ForegroundRole) is None


def test_link_cells_are_styled():
    m = LcscTableModel([_rec("C1", LCSC="C541465")])
    font = m.data(m.index(0, 2), Qt.FontRole)
    assert font is not None and font.underline() is True
    assert m.data(m.index(0, 2), Qt.ForegroundRole) is not None


# ---- JLCPCB export uses session-resolved LCSC -----------------------

def test_gui_export_includes_session_lcsc():
    """A LCSC resolved in the session (not yet saved) flows into the JLCPCB BOM
    via the {ref: lcsc} override the export handler builds from the model."""
    from kicadtools.jlcpcb import build_jlcpcb_bom

    rec = _rec("U1", Value="STM32", Footprint="LQFP-48", **{"DigiKey PN": "P100-ND"})
    m = LcscTableModel([rec])
    assert m.rows()[0].lcsc == ""  # nothing on disk
    m.apply_resolution(0, _resolution("U1", "uuid-U1", Status.RESOLVED, lcsc="C123"))

    override = {row.reference: row.lcsc for row in m.rows() if row.lcsc}
    (line,) = build_jlcpcb_bom([rec], override)
    assert line.lcsc == "C123"


def test_loads_real_board_and_sorts_by_ref():
    m = LcscTableModel()
    m.load(iter_bom_part_records(BOARD))
    assert m.rowCount() > 0
    proxy = NaturalSortProxyModel()
    proxy.setSourceModel(m)
    proxy.sort(1, Qt.AscendingOrder)  # Ref column
    refs = [proxy.data(proxy.index(r, 1), Qt.DisplayRole) for r in range(proxy.rowCount())]
    caps = [r for r in refs if r.startswith("C")]
    if "C2" in caps and "C10" in caps:
        assert caps.index("C2") < caps.index("C10")
