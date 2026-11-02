"""Tests for the IC rows of the Parts tab (gui/parts.py), the IC assign dialog
(gui/ic_assign_dialog.py), the IC search worker and the window's IC path
(``IC_PLAN.md`` step 8). No network: searches are faked, results are
delivered by hand, and the window smoke test works on a scratch copy of the
ECG fixture."""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt, QThread, Signal  # noqa: E402

from kicadtools.gui.ic_assign_dialog import (  # noqa: E402
    COL_IC_CHECK,
    IC_CANDIDATE_COLUMNS,
    MATCH_TEXT,
    IcAssignDialog,
    check_message,
    conflicts_text,
    footprint_header,
    ic_candidate_cells,
    ic_result_summary,
    ic_source_text,
    stock_floor,
    stock_minimum,
    stock_warning_text,
)
from kicadtools.gui.parts import (  # noqa: E402
    COL_CURRENT,
    COL_DIELECTRIC,
    COL_FOOTPRINT,
    COL_MPN,
    COL_PACKAGE,
    COL_QTY,
    COL_RATING,
    COL_REFERENCES,
    COL_STATUS,
    COL_TOLERANCE,
    COL_VALUE,
    DASH,
    MISMATCH_TINT,
    STATUS_ASSIGNED,
    STATUS_CONFLICT,
    STATUS_NOT_SEARCHABLE,
    STATUS_QUEUED,
    STATUS_UNASSIGNED,
    UNVERIFIED_TINT,
    IcCandidateWorker,
    PartsTableModel,
    footprint_name,
    ic_package_cell,
    ic_value_cell,
    identifiers_text,
    is_ic,
    package_text,
)
from kicadtools.icrequirement import (  # noqa: E402
    IcGroup,
    IcRequirement,
    Identifiers,
    group_ics,
)
from kicadtools.icsearch import (  # noqa: E402
    CURRENT,
    DATABASE,
    MATCH,
    MISMATCH,
    MISMATCH_TEXT,
    NOT_AT_JLCPCB,
    SEARCH,
    UNVERIFIED,
    UNVERIFIED_TEXT,
    FootprintCheck,
    IcCandidate,
    IcSearchResult,
    ic_assignment_plan,
    ic_requirement_for,
)
from kicadtools.package import PackageInfo, parse_footprint  # noqa: E402
from kicadtools.requirement import Member, group_parts, part_defaults  # noqa: E402
from kicadtools.resolve import iter_bom_part_records  # noqa: E402

DATA = Path(__file__).parent / "data"
ECG = DATA / "ECG.kicad_sch"
# Schematic writes go only to scratch copies under this folder (when it can
# be used), else under pytest's tmp_path.
IC_WORK = Path("/private/tmp/claude-501/ic-work")

_MISMATCH = FootprintCheck(MISMATCH, ("pitch 0.5 mm vs 0.65 mm",))
_UNVERIFIED = FootprintCheck(UNVERIFIED, ("DigiKey states no package",))


def _ic_groups(path=ECG):
    return group_ics(iter_bom_part_records(path))


def _ic(**kw) -> IcCandidate:
    base = dict(
        mpn="LMC7101BYM5/NOPB", manufacturer="Texas Instruments",
        package="SOT-23-5", package_case="SC-74A, SOT-753", pins=5,
        digikey_pn="LMC7101BYM5/NOPBCT-ND", digikey_stock=5_000,
        digikey_status="Active", digikey_price=1.25,
        source=CURRENT, current=True, fields=("Value",),
        footprint=FootprintCheck(MATCH),
    )
    base.update(kw)
    return IcCandidate(**base)


def _model():
    """ECG's capacitor and resistor groups followed by its IC groups."""
    records = iter_bom_part_records(ECG)
    model = PartsTableModel()
    model.load(group_parts(records, part_defaults({})) + group_ics(records))
    return model


def _ic_row(model, ref):
    for r in range(model.rowCount()):
        g = model.group_at(r)
        if is_ic(g) and ref in g.references:
            return r
    raise AssertionError(ref)


def _cell(model, row, col):
    return model.data(model.index(row, col), Qt.DisplayRole)


def _footprint_tint(model, row):
    return model.data(model.index(row, COL_FOOTPRINT), Qt.BackgroundRole)


def _row_tint(model, row):
    # The Status cell has the row's tint and never a check colour.
    return model.data(model.index(row, COL_STATUS), Qt.BackgroundRole)


def _synthetic_group(ids=Identifiers(), footprint="", value=""):
    req = IcRequirement(identifiers=ids, value_text=value, footprint=footprint)
    member = Member("U7", Path("x.kicad_sch"), "u-7",
                    {"Value": value, "Footprint": footprint}, None)
    return IcGroup(req, [member])


# ---- pure cell helpers ------------------------------------------------------

def test_footprint_name_and_package_text():
    assert footprint_name("Package_TO_SOT_SMD:SOT-23-5") == "SOT-23-5"
    assert footprint_name("SOT-23-5") == "SOT-23-5"
    assert footprint_name("") == ""
    assert package_text(parse_footprint("Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm")) \
        == "QFN-24 EP, 4x4 mm, 0.5 mm pitch"
    assert package_text(parse_footprint("LQFP-48_7x7mm_P0.5mm")) == "LQFP-48, 7x7 mm, 0.5 mm pitch"
    assert package_text(PackageInfo(pins=8)) == "8 pins"
    assert package_text(PackageInfo()) == ""
    assert package_text(None) == ""


def test_ic_value_and_package_cells():
    ids = Identifiers(mpn="DRV8313RHHR", value_mpns=("DRV8313",))
    req = IcRequirement(ids, value_text="DRV8313", footprint="Package_TO_SOT_SMD:SOT-23-5")
    assert ic_value_cell(req) == "DRV8313RHHR"
    assert ic_value_cell(IcRequirement(Identifiers(), value_text="LMC7101BYM5")) == "LMC7101BYM5"
    assert ic_value_cell(IcRequirement(Identifiers())) == DASH
    assert ic_package_cell(req) == "SOT-23-5 (footprint)"
    assert ic_package_cell(req, _ic(package="SOT-23-5 (2.9x1.6)")) == "SOT-23-5 (2.9x1.6)"
    assert ic_package_cell(req, _ic(package="", package_case="SC-74A")) == "SC-74A"
    custom = IcRequirement(ids, footprint="ECG_Footprints:CP_20_10_ADI_NOTH")
    assert ic_package_cell(custom) == DASH


def test_identifiers_text():
    ids = Identifiers(digikey_pn="497-19548-2-ND", value_mpns=("LDL212PV33R",))
    assert identifiers_text(IcRequirement(ids)) == "DigiKeyPN 497-19548-2-ND, Value LDL212PV33R"
    assert identifiers_text(IcRequirement(Identifiers())) == "none"


# ---- Parts tab model --------------------------------------------------------

def test_ic_rows_follow_capacitors_and_resistors():
    model = _model()
    kinds = [is_ic(model.group_at(r)) for r in range(model.rowCount())]
    assert kinds == [False] * 13 + [True] * 3
    r = _ic_row(model, "U2")
    assert _cell(model, r, COL_VALUE) == "LMC7101BYM5"
    assert _cell(model, r, COL_PACKAGE) == "SOT-23-5 (footprint)"
    assert _cell(model, r, COL_FOOTPRINT) == "SOT-23-5"
    for col in (COL_RATING, COL_DIELECTRIC, COL_TOLERANCE):
        assert _cell(model, r, col) == ""
    assert _cell(model, r, COL_QTY) == "1"
    assert _cell(model, r, COL_REFERENCES) == "U2"
    assert _cell(model, r, COL_CURRENT) == DASH
    assert _cell(model, r, COL_STATUS) == STATUS_UNASSIGNED
    assert _cell(model, r, COL_MPN) == ""
    # Before a search the Footprint cell has no check colour.
    assert model.footprint_check(r) is None
    assert _footprint_tint(model, r) is _row_tint(model, r)
    tip = model.data(model.index(r, COL_FOOTPRINT), Qt.ToolTipRole)
    assert "Package_TO_SOT_SMD:SOT-23-5" in tip and "Not checked yet" in tip
    # U1's current part is its DigiKey PN.
    assert _cell(model, _ic_row(model, "U1"), COL_CURRENT) == "505-AD8232ACPZ-R7CT-ND"


def test_ic_not_searchable_rows():
    no_pn = _synthetic_group(footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", value="OLED")
    no_fp = _synthetic_group(ids=Identifiers(value_mpns=("LM358DR",)), value="LM358DR")
    model = PartsTableModel([no_pn, no_fp])
    assert model.status(0) == model.status(1) == STATUS_NOT_SEARCHABLE
    assert _cell(model, 1, COL_FOOTPRINT) == DASH
    assert model.data(model.index(0, COL_VALUE), Qt.ToolTipRole) == "Not searchable: no part number."
    assert model.data(model.index(1, COL_VALUE), Qt.ToolTipRole) == "Not searchable: no footprint."
    assert model.summary()[STATUS_NOT_SEARCHABLE] == 2
    # The Footprint tooltip gives the reason, not a double-click hint that
    # the window would refuse.
    tip = model.data(model.index(0, COL_FOOTPRINT), Qt.ToolTipRole)
    assert "Double-click" not in tip and "Not checked: no part number." in tip


def test_recorded_result_colours_footprint_cell():
    model = _model()
    r = _ic_row(model, "U2")
    req = model.group_at(r).requirement
    # A partial-MPN candidate does not colour the row when another is named.
    result = IcSearchResult([_ic(footprint=_MISMATCH),
                             _ic(mpn="LMC7101BIM5", current=False, source=SEARCH,
                                 footprint=_UNVERIFIED)],
                            requirement=IcRequirement(req.identifiers, req.value_text,
                                                      req.footprint, parse_footprint(req.footprint), 5))
    model.record_ic_result(r, result)
    assert model.footprint_check(r) == _MISMATCH
    assert model.data(model.index(r, COL_FOOTPRINT), Qt.BackgroundRole) is MISMATCH_TINT
    tip = model.data(model.index(r, COL_FOOTPRINT), Qt.ToolTipRole)
    assert MISMATCH_TEXT in tip and "pitch 0.5 mm vs 0.65 mm" in tip
    # The recorded requirement (pad count read) is what a reopened dialog gets.
    assert model.requirement_at(r).pad_count == 5

    model.record_ic_result(r, IcSearchResult([_ic(footprint=_UNVERIFIED, current=False)]))
    assert model.data(model.index(r, COL_FOOTPRINT), Qt.BackgroundRole) is UNVERIFIED_TINT
    assert UNVERIFIED_TEXT in model.data(model.index(r, COL_FOOTPRINT), Qt.ToolTipRole)

    model.record_ic_result(r, IcSearchResult([_ic()]))
    # A match has no check colour: the cell has the row's tint.
    assert _footprint_tint(model, r) is _row_tint(model, r)
    assert _row_tint(model, r) not in (MISMATCH_TINT, UNVERIFIED_TINT)
    assert "matches" in model.data(model.index(r, COL_FOOTPRINT), Qt.ToolTipRole)
    # Two candidates, neither named exactly: no single check to show.
    model.record_ic_result(r, IcSearchResult([_ic(current=False), _ic(mpn="X", current=False)]))
    assert model.footprint_check(r) is None


def test_recorded_conflicts_make_conflict_status():
    model = _model()
    r = _ic_row(model, "U1")
    conflict = "DigiKeyPN 161-LTC6992HS6-2 names LTC6992HS6-2; Value LTC6992CS6-2 names LTC6992CS6-2"
    model.record_ic_result(r, IcSearchResult([_ic(), _ic(mpn="Y")], conflicts=[conflict]))
    assert model.status(r) == STATUS_CONFLICT
    assert model.summary()[STATUS_CONFLICT] == 1
    tip = model.data(model.index(r, COL_VALUE), Qt.ToolTipRole)
    assert conflict in tip and "Double-click to choose one" in tip
    # A reload clears the recorded search.
    model.load(_ic_groups())
    assert model.ic_result_at(0) is None
    assert model.status(0) == STATUS_UNASSIGNED


def test_queue_ic_row():
    model = _model()
    r = _ic_row(model, "U2")
    group = model.group_at(r)
    cand = _ic(footprint=_MISMATCH, package="SOT-23-5 (2.9x1.6)")
    plan = ic_assignment_plan(group, cand)
    model.queue(r, group.requirement, cand, plan)
    assert model.status(r) == STATUS_QUEUED
    assert _cell(model, r, COL_MPN) == "LMC7101BYM5/NOPB"
    assert _cell(model, r, COL_PACKAGE) == "SOT-23-5 (2.9x1.6)"
    assert model.data(model.index(r, COL_FOOTPRINT), Qt.BackgroundRole) is MISMATCH_TINT
    edits = model.accepted_edits()
    (props,) = edits[ECG].values()
    assert props["MPN"] == "LMC7101BYM5/NOPB"
    assert props["Manufacturer"] == "Texas Instruments"
    assert props["DigiKeyPN"] == "LMC7101BYM5/NOPBCT-ND"
    assert props["Intent:Type"] == "ic"
    for key in ("Value", "Footprint", "Datasheet", "Reference"):
        assert key not in props


def test_set_defaults_leaves_ic_rows():
    model = _model()
    r = _ic_row(model, "U2")
    before = model.group_at(r).requirement
    model.set_defaults(part_defaults({"cap.voltage_min": "25V"}))
    assert model.group_at(r).requirement is before


def test_assigned_ic_row():
    props = {"Value": "LMC7101BYM5", "Footprint": "Package_TO_SOT_SMD:SOT-23-5",
             "MPN": "LMC7101BYM5/NOPB", "Manufacturer": "TI",
             "DigiKeyPN": "LMC7101BYM5/NOPBCT-ND"}
    req = IcRequirement(Identifiers(mpn="LMC7101BYM5/NOPB",
                                    digikey_pn="LMC7101BYM5/NOPBCT-ND",
                                    value_mpns=("LMC7101BYM5",)),
                        "LMC7101BYM5", "Package_TO_SOT_SMD:SOT-23-5")
    group = IcGroup(req, [Member("U2", Path("x.kicad_sch"), "u-2", props, None)])
    model = PartsTableModel([group])
    assert model.status(0) == STATUS_ASSIGNED
    assert _cell(model, 0, COL_VALUE) == "LMC7101BYM5/NOPB"
    assert _cell(model, 0, COL_MPN) == "LMC7101BYM5/NOPB"
    assert "check the footprint" in model.data(model.index(0, COL_VALUE), Qt.ToolTipRole)


# ---- dialog helpers ---------------------------------------------------------

def test_ic_candidate_cells():
    c = _ic(lcsc="C7377", jlc_library="extended", jlc_stock=4_000, jlc_price=0.9,
            footprint=_MISMATCH)
    cells = ic_candidate_cells(c)
    assert len(cells) == len(IC_CANDIDATE_COLUMNS)
    assert cells == ["LMC7101BYM5/NOPB", "Texas Instruments", "current part (Value)",
                     "LMC7101BYM5/NOPBCT-ND", "5,000", "C7377", "Extended", "4,000",
                     "SOT-23-5", "mismatch", "$0.9000"]
    # Not at JLCPCB: DigiKey's price, no JLCPCB stock.
    cells = ic_candidate_cells(_ic())
    assert cells[5] == NOT_AT_JLCPCB and cells[6] == cells[7] == ""
    assert cells[-1] == "$1.2500"
    # Listed at JLCPCB but below the floor: the note says why.
    short = ic_candidate_cells(_ic(lcsc="C7377", jlc_stock=10,
                                   jlc_note="JLCPCB stock 10, need at least 1001"))
    assert short[5] == "C7377 (JLCPCB stock 10, need at least 1001)"
    assert ic_candidate_cells(_ic(digikey_price=float("inf")))[-1] == ""


def test_ic_source_text():
    assert ic_source_text(_ic(fields=("DigiKeyPN", "Value"))) == "current part (DigiKeyPN, Value)"
    assert ic_source_text(_ic(source=DATABASE, usage_count=2)) == "database (2 uses)"
    assert ic_source_text(_ic(source=DATABASE)) == "database"
    assert ic_source_text(_ic(source=SEARCH, current=False)) == "partial MPN (Value)"
    # A partial MPN that equals the found MPN names it exactly.
    assert ic_source_text(_ic(source=SEARCH, current=True, fields=())) == "current part"


def test_check_message_and_header():
    assert check_message(None) == ""
    assert check_message(FootprintCheck(MATCH)) == MATCH_TEXT
    assert check_message(_MISMATCH).startswith(MISMATCH_TEXT)
    assert "pitch 0.5 mm vs 0.65 mm" in check_message(_MISMATCH)
    req = IcRequirement(Identifiers(value_mpns=("LMC7101BYM5",)),
                        footprint="Package_TO_SOT_SMD:SOT-23-5")
    assert footprint_header(req) == \
        "Package_TO_SOT_SMD:SOT-23-5 — SOT-23-5; pad count unknown"
    from dataclasses import replace
    assert footprint_header(replace(req, pad_count=5)).endswith("; 5 pads")
    custom = replace(req, footprint="ECG_Footprints:CP_20_10_ADI_NOTH")
    assert "package not readable from the name" in footprint_header(custom)
    assert footprint_header(replace(req, footprint="")) == "none"
    assert stock_floor(30) == 1030


def test_ic_result_summary_and_conflicts():
    res = IcSearchResult([_ic(), _ic(source=DATABASE), _ic(source=SEARCH, current=False)],
                         rejected=[("X", "DigiKey status 'Obsolete'")], dropped_by_limit=2,
                         problem="DigiKey credentials are not set.")
    assert ic_result_summary(res) == (
        "3 candidates (1 from the database, 1 named by the symbol, 1 from a partial "
        "MPN); rejected 1; 2 past the DigiKey lookup limit, not checked. DigiKey "
        "credentials are not set.")
    # A partial-MPN hit that is exactly the named part counts as named, as
    # its Source cell ("current part") says.
    exact = IcSearchResult([_ic(source=SEARCH, current=True)])
    assert ic_source_text(exact.candidates[0]).startswith("current part")
    assert ic_result_summary(exact) == (
        "1 candidate (0 from the database, 1 named by the symbol, 0 from a partial MPN).")
    assert conflicts_text(res) == ""
    res.conflicts = ["A names X; B names Y"]
    assert conflicts_text(res).endswith("\nA names X; B names Y")


# ---- dialog (offscreen) -----------------------------------------------------

@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _u2():
    return next(g for g in _ic_groups() if g.references == ["U2"])


def test_dialog_first_request_none_then_requirement(app):
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 10)
    sent = []
    dialog.search_requested.connect(lambda req, sup: sent.append((req, sup)))
    assert dialog.needed == 10
    assert dialog.request_search(False)
    assert sent == [(None, True)]
    resolved = IcRequirement(group.requirement.identifiers, group.requirement.value_text,
                             group.requirement.footprint,
                             parse_footprint(group.requirement.footprint), 5)
    dialog.show_result(IcSearchResult([_ic()], requirement=resolved), dialog.request_id)
    assert dialog.requirement is resolved
    assert dialog._footprint_label.text().endswith("; 5 pads")
    assert dialog.rerun()
    assert sent[-1] == (resolved, True)
    assert dialog.request_id == 2


def test_dialog_opened_with_recorded_requirement_sends_it(app):
    group = _u2()
    from dataclasses import replace
    recorded = replace(group.requirement,
                       package=parse_footprint(group.requirement.footprint), pad_count=5)
    dialog = IcAssignDialog(group, recorded, 1)
    sent = []
    dialog.search_requested.connect(lambda req, sup: sent.append(req))
    dialog.request_search()
    assert sent == [recorded]


def test_dialog_recorded_requirement_without_pad_count_is_resolved(app):
    """A footprint not found anywhere leaves pad_count None; the recorded
    requirement is still sent, so the footprints are not read again."""
    group = _u2()
    from dataclasses import replace
    recorded = replace(group.requirement,
                       package=parse_footprint(group.requirement.footprint))
    assert recorded.pad_count is None
    dialog = IcAssignDialog(group, recorded, 1)
    sent = []
    dialog.search_requested.connect(lambda req, sup: sent.append(req))
    dialog.request_search()
    assert sent == [recorded]


def test_dialog_takes_requirement_from_stale_result(app):
    """Search again before the first result: the stale result's requirement
    is kept, so a later request does not send None."""
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    sent = []
    dialog.search_requested.connect(lambda req, sup: sent.append(req))
    dialog.request_search()
    old = dialog.request_id
    dialog.request_search()
    assert sent == [None, None]
    resolved = ic_requirement_for(group)
    dialog.show_result(IcSearchResult([_ic(mpn="OLD")], requirement=resolved), old)
    assert dialog.chosen() is None
    assert dialog.requirement is resolved
    dialog.request_search()
    assert sent[-1] is resolved


def test_dialog_not_searchable(app):
    group = _synthetic_group(footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", value="OLED")
    dialog = IcAssignDialog(group, group.requirement, 1)
    sent = []
    dialog.search_requested.connect(lambda *a: sent.append(a))
    assert not dialog.request_search()
    assert sent == []
    assert dialog._status.text() == "Can't search: no part number."
    assert not dialog._search_btn.isEnabled()


def test_dialog_ignores_stale_results(app):
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    dialog.request_search()
    old = dialog.request_id
    dialog.request_search()
    dialog.show_result(IcSearchResult([_ic(mpn="OLD")]), old)
    assert dialog.chosen() is None
    dialog.show_error("boom", old)
    assert "failed" not in dialog._status.text()
    dialog.show_result(IcSearchResult([_ic(mpn="NEW")]), dialog.request_id)
    assert dialog.chosen().mpn == "NEW"


def test_dialog_lcsc_tooltip_has_note_and_link(app):
    from kicadtools.gui.ic_assign_dialog import COL_IC_LCSC
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    dialog.request_search()
    note = "JLCPCB stock 10, need at least 1001"
    dialog.show_result(IcSearchResult([_ic(lcsc="C7377", jlc_stock=10, jlc_note=note),
                                       _ic(lcsc="C7378")]), dialog.request_id)
    short = dialog._table.item(0, COL_IC_LCSC).toolTip()
    assert short.startswith(note + "\n") and "C7377" in short
    assert dialog._table.item(1, COL_IC_LCSC).toolTip().count("\n") == 0


def test_dialog_check_message_follows_selection(app):
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    dialog.request_search()
    dialog.show_result(IcSearchResult([_ic(footprint=_MISMATCH),
                                       _ic(mpn="B", footprint=_UNVERIFIED),
                                       _ic(mpn="C")]), dialog.request_id)
    assert dialog.check_text() == _MISMATCH.message
    assert "c02020" in dialog._check.styleSheet()
    item = dialog._table.item(0, COL_IC_CHECK)
    assert item.text() == "mismatch" and item.background().color() == MISMATCH_TINT
    dialog._table.selectRow(1)
    assert dialog.check_text() == _UNVERIFIED.message
    assert "c07000" in dialog._check.styleSheet()
    dialog._table.selectRow(2)
    assert dialog.check_text() == MATCH_TEXT
    assert dialog._accept_btn.isEnabled()
    dialog._table.clearSelection()
    assert dialog.check_text() == ""
    assert not dialog._accept_btn.isEnabled()


def test_dialog_candidate_without_digikey_pn_cannot_be_accepted(app):
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    dialog.request_search()
    dialog.show_result(IcSearchResult([_ic(digikey_pn=None)]), dialog.request_id)
    assert dialog.chosen() is None
    assert not dialog._accept_btn.isEnabled()
    assert "DigiKeyPN" in dialog._accept_btn.toolTip()


def test_dialog_shows_conflicts_notes_and_drops(app):
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    dialog.request_search()
    a, b = _ic(mpn="A"), _ic(mpn="B")
    dialog.show_result(IcSearchResult([a, b], notes=["Value LMC7101BYM5: DigiKey A"],
                                      conflicts=["MPN A names A; Value B names B"]),
                       dialog.request_id)
    assert not dialog._conflicts.isHidden()
    assert "MPN A names A" in dialog._conflicts.text()
    assert dialog._notes.toPlainText() == "Value LMC7101BYM5: DigiKey A"
    dialog.drop_candidate(a)
    assert dialog._table.rowCount() == 1
    # The result the window records when the dialog closes no longer holds
    # the rejected part, so the row cannot show its footprint check.
    assert dialog.result.candidates == [b]
    assert dialog.result.notes == ["Value LMC7101BYM5: DigiKey A"]
    assert dialog.requirement_for(b) is dialog.requirement


def test_dialog_context_menu_rejects_the_clicked_candidate(app, monkeypatch):
    from PySide6.QtCore import QPoint
    import kicadtools.gui.ic_assign_dialog as icd

    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    dialog.request_search()
    a, b = _ic(mpn="A"), _ic(mpn="B")
    dialog.show_result(IcSearchResult([a, b]), dialog.request_id)
    rejected, labels = [], []
    dialog.reject_requested.connect(rejected.append)
    # The dialog is not shown, so the click position is mapped by hand: the
    # click lands on row 1, or (None) outside every row.
    clicked = [dialog._table.item(1, 0)]
    monkeypatch.setattr(dialog._table, "itemAt", lambda pos: clicked[0])

    choose = [True]

    class Menu(icd.QMenu):
        # Never shown: returns the first action, or None (menu dismissed).
        def exec(self, *args):
            labels.append([act.text() for act in self.actions()])
            return self.actions()[0] if choose[0] else None

    monkeypatch.setattr(icd, "QMenu", Menu)
    dialog._context_menu(QPoint(5, 5))
    assert labels == [["Reject B"]]
    assert rejected == [b]
    # A click outside every row opens no menu.
    clicked[0] = None
    dialog._context_menu(QPoint(5, 5))
    assert labels == [["Reject B"]]
    # A dismissed menu rejects nothing.
    clicked[0], choose[0] = dialog._table.item(0, 0), False
    dialog._context_menu(QPoint(5, 5))
    assert labels[-1] == ["Reject A"]
    assert rejected == [b]


def test_dialog_units_needed_counts_every_reference(app):
    from PySide6.QtWidgets import QLabel

    req = IcRequirement(Identifiers(mpn="DRV8313RHHR"), value_text="DRV8313RHHR",
                        footprint="TI:RHH0036C")
    members = [Member(ref, Path("x.kicad_sch"), f"u-{ref}", {"Value": "DRV8313RHHR"}, None)
               for ref in ("U1", "U2")]
    group = IcGroup(req, members)
    assert group.quantity == 2
    dialog = IcAssignDialog(group, req, 10)
    assert dialog.needed == 20
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "20" in texts
    assert (f"{stock_floor(20):,} at DigiKey (required) and at JLCPCB; "
            f"down to {stock_minimum(20):,} with a warning") in texts


def test_stock_warning_text():
    assert stock_warning_text(_ic()) == ""
    cand = _ic(digikey_warning="DigiKey stock 798, below 1025")
    assert stock_warning_text(cand) == (
        "Stock is below the floor — check it before ordering: "
        "DigiKey stock 798, below 1025.")
    # A JLCPCB warning counts only when the part is at JLCPCB.
    assert stock_warning_text(_ic(jlc_warning="JLCPCB stock 500, below 1025")) == ""
    both = _ic(digikey_warning="DigiKey stock 798, below 1025", lcsc="C7",
               jlc_warning="JLCPCB stock 500, below 1025")
    assert stock_warning_text(both).endswith(
        "DigiKey stock 798, below 1025; JLCPCB stock 500, below 1025.")


def test_dialog_shows_stock_warning_for_selection(app):
    from kicadtools.gui.ic_assign_dialog import COL_IC_DK_STOCK
    group = _u2()
    dialog = IcAssignDialog(group, group.requirement, 1)
    dialog.request_search()
    low = _ic(mpn="LOW", digikey_stock=798, digikey_warning="DigiKey stock 798, below 1001")
    dialog.show_result(IcSearchResult([_ic(), low]), dialog.request_id)
    assert dialog.stock_text() == ""
    assert dialog._table.item(0, COL_IC_DK_STOCK).toolTip() == ""
    item = dialog._table.item(1, COL_IC_DK_STOCK)
    assert item.toolTip() == "DigiKey stock 798, below 1001"
    assert item.background().color().isValid() and item.background().style() != Qt.NoBrush
    dialog._table.selectRow(1)
    assert "DigiKey stock 798, below 1001" in dialog.stock_text()
    assert dialog.chosen().mpn == "LOW"


# ---- worker -----------------------------------------------------------------

def test_ic_worker_calls_assess_ic(tmp_path, monkeypatch):
    import kicadtools.gui.parts as parts

    calls = []
    result = IcSearchResult([_ic()])

    from kicadtools import suppliers
    from kicadtools.resolve import Cache

    cached = []

    def fake_cached(code, cache):
        cached.append((code, cache))
        return None

    def fake(group, **kw):
        # The supplier callables, checked while the worker's cache is open;
        # nothing here reaches the network.
        assert isinstance(kw["digikey"], suppliers.DigiKeyClient)
        assert kw["jlc_search"] is suppliers.jlcpcb_search
        assert kw["jlc_part"]("C7377") is None
        calls.append((group, kw))
        return result

    monkeypatch.setattr(parts, "assess_ic", fake)
    monkeypatch.setattr(parts, "cached_jlcpcb_part", fake_cached)
    group = _u2()
    board = tmp_path / "ECG.kicad_sch"
    worker = IcCandidateWorker(group=group, requirement=None, needed=40,
                               db_path=tmp_path / "parts.sqlite", board=board,
                               cache_path=tmp_path / "cache" / "lcsc.sqlite")
    found, failed = [], []
    worker.found.connect(found.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == [] and found == [result]
    (g, kw), = calls
    assert g is group and kw["needed"] == 40 and kw["requirement"] is None
    assert kw["pads"].pcb_path == board.with_suffix(".kicad_pcb")
    # jlc_part goes through the LCSC cache at cache_path.
    ((code, cache),) = cached
    assert code == "C7377" and isinstance(cache, Cache)
    assert (tmp_path / "cache" / "lcsc.sqlite").exists()
    # A later request: the requirement is passed and no pads are read.
    calls.clear()
    req = group.requirement
    IcCandidateWorker(group=group, requirement=req, needed=1,
                      db_path=tmp_path / "parts.sqlite", board=board).run()
    assert calls[0][1]["requirement"] is req and calls[0][1]["pads"] is None


def test_ic_worker_reports_failure(tmp_path, monkeypatch):
    import kicadtools.gui.parts as parts

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(parts, "assess_ic", boom)
    worker = IcCandidateWorker(group=_u2(), requirement=None, needed=1,
                               db_path=tmp_path / "parts.sqlite")
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == ["no network"]


# ---- headless window --------------------------------------------------------

@pytest.fixture
def scratch(tmp_path):
    """A scratch folder for schematic copies: under :data:`IC_WORK` when it
    can be created, else *tmp_path*."""
    try:
        IC_WORK.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="gui-", dir=IC_WORK))
    except OSError:
        yield tmp_path
        return
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def window(tmp_path, scratch, monkeypatch):
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "parts.sqlite"))
    import kicadtools.gui.main_window as mw

    monkeypatch.setattr(mw, "_LCSC_CACHE", tmp_path / ".kicadtools" / "lcsc_cache.sqlite")
    app = QApplication.instance() or QApplication([])
    board = scratch / "ECG.kicad_sch"
    shutil.copy(ECG, board)
    win = mw.MainWindow(str(board))
    yield win, board
    win.setWindowModified(False)
    win.close()
    app.processEvents()


class _FakeIcWorker(QObject):
    """Stands in for IcCandidateWorker: returns one candidate per request,
    with the requirement resolved (pad count 5)."""
    found = Signal(object)
    failed = Signal(str)
    inits: list = []
    run_threads: list = []
    footprint = FootprintCheck(MATCH)

    def __init__(self, *, group, requirement, needed, db_path, board=None, cache_path=None):
        super().__init__()
        self.group = group
        self.req = requirement
        type(self).inits.append(dict(group=group, requirement=requirement,
                                     needed=needed, board=board))

    def run(self):
        from dataclasses import replace
        type(self).run_threads.append(threading.get_ident())
        req = self.req or replace(self.group.requirement,
                                  package=parse_footprint(self.group.requirement.footprint),
                                  pad_count=5)
        self.found.emit(IcSearchResult([_ic(footprint=type(self).footprint)],
                                       notes=["Value LMC7101BYM5: DigiKey"],
                                       requirement=req))
        QThread.currentThread().quit()


def _wait_idle(win, timeout=10.0):
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + timeout
    while (win._busy() or win._search_running) and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()
    assert not win._busy() and not win._search_running


@pytest.fixture
def fake_ic(monkeypatch):
    import kicadtools.gui.main_window as mw
    monkeypatch.setattr(mw, "IcCandidateWorker", _FakeIcWorker)
    _FakeIcWorker.inits.clear()
    _FakeIcWorker.run_threads.clear()
    _FakeIcWorker.footprint = FootprintCheck(MATCH)
    yield _FakeIcWorker


def _accepting_exec(dialog):
    from PySide6.QtWidgets import QApplication, QDialog
    deadline = time.monotonic() + 10
    while dialog.chosen() is None and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    return QDialog.Accepted


def test_window_ic_rows_and_status(window):
    win, _board = window
    model = win.parts_model
    assert model.rowCount() == 16
    assert sum(is_ic(model.group_at(r)) for r in range(16)) == 3
    assert "16 part groups, 16 unassigned" in win.statusBar().currentMessage()


def test_window_assign_ic_end_to_end(window, fake_ic, monkeypatch):
    """Double-click an IC row: the IC dialog opens, the (fake) worker runs off
    the GUI thread, the candidate is accepted; the part is in the database,
    the plan is queued, and File > Save writes it merged with an Intents-tab
    accept. Value, Footprint and Datasheet are unchanged on disk."""
    import kicadtools.gui.main_window as mw

    win, board = window
    model = win.parts_model
    r = _ic_row(model, "U2")
    win.build_qty.setValue(7)
    monkeypatch.setattr(mw.IcAssignDialog, "exec", _accepting_exec)
    win._parts_double_clicked(win.parts_proxy.mapFromSource(model.index(r, 0)))
    _wait_idle(win)
    assert win._assign_dialog is None
    assert model.status(r) == STATUS_QUEUED
    assert _cell(model, r, COL_MPN) == "LMC7101BYM5/NOPB"
    assert win.isWindowModified()
    assert "Save to write it" in win.statusBar().currentMessage()
    assert NOT_AT_JLCPCB in win.statusBar().currentMessage()
    # One request, the first one sending None, for the dialog's group and
    # the board; it ran on a worker thread.
    (init,) = fake_ic.inits
    assert init["requirement"] is None and init["board"] == board
    assert init["needed"] == 7 * model.group_at(r).quantity
    assert init["group"] is model.group_at(r)
    assert threading.get_ident() not in fake_ic.run_threads
    # The dialog's last result is recorded on the row.
    assert model.ic_result_at(r) is not None
    assert model.requirement_at(r).pad_count == 5

    (hit,) = win._parts_db.find_ics("LMC7101BYM5/NOPB")
    assert hit.digikey_pn == "LMC7101BYM5/NOPBCT-ND" and hit.lcsc is None
    assert hit.usage_count == 1

    # An Intents-tab accept is merged into the same save.
    win.model.accept_all()
    uuid = model.group_at(r).members[0].symbol_uuid
    # An LCSC the LCSC tab resolved for the symbol's old part is replaced:
    # the chosen part is not at JLCPCB, so the save writes no LCSC.
    lcsc_tab = {Path(board): {uuid: {"LCSC": "C12345", "MPN": "LMC7101BIM5"}}}
    monkeypatch.setattr(win.lcsc_model, "accepted_edits", lambda: lcsc_tab)
    edits = win._merged_edits()
    assert edits[Path(board)][uuid]["MPN"] == "LMC7101BYM5/NOPB"
    assert edits[Path(board)][uuid]["LCSC"] is None
    assert edits[Path(board)][uuid]["Intent:Type"] == "ic"
    before = board.read_text()
    assert win._write_in_place(edits)

    after = iter_bom_part_records(board)
    u2 = next(rec for rec in after if rec.reference == "U2")
    assert u2.props["MPN"] == "LMC7101BYM5/NOPB"
    assert u2.props["Manufacturer"] == "Texas Instruments"
    assert u2.props["DigiKeyPN"] == "LMC7101BYM5/NOPBCT-ND"
    assert "LCSC" not in u2.props
    orig = next(rec for rec in iter_bom_part_records(ECG) if rec.reference == "U2")
    for key in ("Value", "Footprint", "Datasheet"):
        assert u2.props[key] == orig.props[key]
    assert before != board.read_text()
    r = _ic_row(win.parts_model, "U2")
    assert win.parts_model.status(r) == STATUS_ASSIGNED
    assert not win.isWindowModified()


def test_window_reopened_ic_dialog_reuses_requirement(window, fake_ic, monkeypatch):
    """A rejected (cancelled) dialog still records its result; the next dialog
    for the row sends the resolved requirement, so pads are read once."""
    from PySide6.QtWidgets import QApplication, QDialog
    import kicadtools.gui.main_window as mw

    win, _board = window
    fake_ic.footprint = _MISMATCH

    def cancel(dialog):
        deadline = time.monotonic() + 10
        while dialog.result is None and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        return QDialog.Rejected

    monkeypatch.setattr(mw.IcAssignDialog, "exec", cancel)
    r = _ic_row(win.parts_model, "U2")
    win._assign_group(r)
    _wait_idle(win)
    model = win.parts_model
    assert model.status(r) == STATUS_UNASSIGNED
    assert model.data(model.index(r, COL_FOOTPRINT), Qt.BackgroundRole) is MISMATCH_TINT
    win._assign_group(r)
    _wait_idle(win)
    assert fake_ic.inits[0]["requirement"] is None
    assert fake_ic.inits[1]["requirement"].pad_count == 5
    assert not win.isWindowModified()


def test_window_not_searchable_ic_row_does_not_open(window, fake_ic):
    win, _board = window
    group = _synthetic_group(footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", value="OLED")
    win.parts_model.load([group])
    win._parts_double_clicked(win.parts_proxy.mapFromSource(win.parts_model.index(0, 0)))
    assert fake_ic.inits == []
    assert win.statusBar().currentMessage().startswith("Not searchable: no part number.")


def test_window_queued_first_request_uses_the_resolved_requirement(window, fake_ic):
    """Search again before the first result: the queued request was made
    with None, but runs with the requirement the first result resolved, so
    the footprint files are read once."""
    win, _board = window
    r = _ic_row(win.parts_model, "U2")
    group = win.parts_model.group_at(r)
    dialog = IcAssignDialog(group, group.requirement, 1, win)
    win._assign_dialog = dialog
    dialog.search_requested.connect(win._request_candidates)
    assert dialog.request_search()
    assert dialog.request_search()
    assert win._search_next is not None and win._search_next[0] is None
    _wait_idle(win)
    assert [i["requirement"] is None for i in fake_ic.inits] == [True, False]
    assert fake_ic.inits[1]["requirement"].pad_count == 5
    assert dialog.chosen() is not None
    win._assign_dialog = None


def test_window_units_needed_for_a_two_reference_group(window, fake_ic, monkeypatch):
    from PySide6.QtWidgets import QDialog
    import kicadtools.gui.main_window as mw

    win, _board = window
    req = IcRequirement(Identifiers(mpn="LMC7101BYM5/NOPB"), value_text="LMC7101BYM5",
                        footprint="Package_TO_SOT_SMD:SOT-23-5")
    members = [Member(ref, Path("x.kicad_sch"), f"u-{ref}",
                      {"Value": "LMC7101BYM5", "Footprint": req.footprint}, None)
               for ref in ("U4", "U5")]
    win.parts_model.load([IcGroup(req, members)])
    win.build_qty.setValue(3)

    def cancel(dialog):
        _wait_idle(win)
        return QDialog.Rejected

    monkeypatch.setattr(mw.IcAssignDialog, "exec", cancel)
    win._assign_group(0)
    _wait_idle(win)
    assert [i["needed"] for i in fake_ic.inits] == [6]


def test_window_reject_ic_candidate(window, fake_ic):
    win, _board = window
    r = _ic_row(win.parts_model, "U2")
    group = win.parts_model.group_at(r)
    dialog = IcAssignDialog(group, group.requirement, 1, win)
    win._assign_dialog = dialog
    dialog.search_requested.connect(win._request_candidates)
    dialog.reject_requested.connect(win._reject_candidate)
    assert dialog.request_search()
    _wait_idle(win)
    cand = dialog.chosen()
    assert cand is not None
    dialog.reject_requested.emit(cand)
    _wait_idle(win)
    assert win._parts_db.is_rejected(cand.manufacturer, cand.mpn)
    # The search ran again after the rejection.
    assert len(fake_ic.inits) == 2
    assert fake_ic.inits[1]["requirement"].pad_count == 5
    win._assign_dialog = None
