"""Tests for the Parts tab model (gui/parts.py) and a headless MainWindow smoke
test: open a scratch copy of the ECG fixture, queue an assignment through the
model (no network), save, and confirm the group reads assigned after reload."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from kicadtools.assign import assignment_plan  # noqa: E402
from kicadtools.partsearch import Candidate  # noqa: E402
from kicadtools.gui.parts import (  # noqa: E402
    COL_DIELECTRIC,
    COL_MPN,
    COL_PACKAGE,
    COL_QTY,
    COL_REFERENCES,
    COL_STATUS,
    COL_TOLERANCE,
    COL_VALUE,
    COL_RATING,
    DASH,
    PARTS_COLUMNS,
    STATUS_ASSIGNED,
    STATUS_QUEUED,
    STATUS_UNASSIGNED,
    PartsTableModel,
    board_label,
    merge_plans,
    requirement_settings,
    sourced_cell,
)
from kicadtools.partsdb import PartParams  # noqa: E402
from kicadtools.parttypes import CAPACITOR  # noqa: E402
from kicadtools.requirement import (  # noqa: E402
    DEFAULT,
    EDITED,
    RELAXED,
    SCHEMATIC,
    UNSET,
    Sourced,
    part_defaults,
    group_parts,
)
from kicadtools.resolve import iter_bom_part_records  # noqa: E402

DATA = Path(__file__).parent / "data"
ECG = DATA / "ECG.kicad_sch"


def _groups(path=ECG, settings=None, kind="capacitor"):
    """The board's groups of one part type (``None``: every type)."""
    groups = group_parts(iter_bom_part_records(path), part_defaults(settings or {}))
    return [g for g in groups if kind is None or g.requirement.kind == kind]


def _cand(**kw) -> Candidate:
    base = dict(kind="capacitor",
        mpn="CL10B104KB8NNNC", manufacturer="Samsung", package="0603",
        params=PartParams(100e-9, 50.0, "X7R", 10.0),
        lcsc="C1591", jlc_library="basic", jlc_preferred=True,
        jlc_stock=100_000, jlc_price=0.002,
        digikey_pn="1276-1000-1-ND", digikey_stock=50_000, source="search",
    )
    base.update(kw)
    return Candidate(**base)


def _model_with_ecg():
    model = PartsTableModel()
    model.load(_groups())
    return model


def _cell(model, row, col):
    return model.data(model.index(row, col), Qt.DisplayRole)


# ---- cell text ------------------------------------------------------------

def test_sourced_cell_markers():
    assert sourced_cell(Sourced("0603", SCHEMATIC), "package", CAPACITOR) == "0603"
    assert sourced_cell(Sourced(16.0, DEFAULT), "rating", CAPACITOR) == "16V (default)"
    assert sourced_cell(Sourced(("X7R", "X5R"), EDITED), "dielectric", CAPACITOR) == "X7R, X5R (edited)"
    assert sourced_cell(Sourced(10.0, DEFAULT), "tolerance", CAPACITOR) == "10% (default)"
    assert sourced_cell(Sourced(None, UNSET), "rating", CAPACITOR) == DASH
    assert sourced_cell(Sourced((), UNSET), "dielectric", CAPACITOR) == DASH
    assert sourced_cell(Sourced(None, SCHEMATIC), "rating", CAPACITOR) == "unreadable"


def test_columns_and_first_row():
    model = _model_with_ecg()
    assert model.columnCount() == len(PARTS_COLUMNS) == 11
    assert [model.headerData(c, Qt.Horizontal) for c in range(11)] == PARTS_COLUMNS
    assert PARTS_COLUMNS[COL_PACKAGE + 1] == "Footprint"
    assert PARTS_COLUMNS[COL_REFERENCES + 1] == "Current part"
    assert model.rowCount() == 4
    assert _cell(model, 0, COL_VALUE) == "100nF"
    assert _cell(model, 0, COL_PACKAGE) == "0603"            # from the footprint
    assert _cell(model, 0, COL_PACKAGE + 1) == "C_0603_1608Metric"
    assert _cell(model, 0, COL_RATING) == DASH              # unset
    assert _cell(model, 0, COL_DIELECTRIC) == "X7R (default)"
    assert _cell(model, 0, COL_TOLERANCE) == "10% (default)"
    assert _cell(model, 0, COL_QTY) == "7"
    assert _cell(model, 0, COL_REFERENCES) == "C1, C3, C4, C12, C13, C14, C17"
    assert _cell(model, 0, COL_STATUS) == STATUS_UNASSIGNED
    assert _cell(model, 0, COL_MPN) == ""
    # The 1 nF group falls in the small band: C0G/NP0 at 5 %.
    assert _cell(model, 1, COL_DIELECTRIC) == "C0G, NP0 (default)"
    assert _cell(model, 1, COL_TOLERANCE) == "5% (default)"


def test_remembered_voltage_shows_default_marker():
    model = PartsTableModel()
    model.load(_groups(settings={"cap.voltage_min": "25V"}))
    assert _cell(model, 0, COL_RATING) == "25V (default)"


def test_unassigned_rows_tinted():
    model = _model_with_ecg()
    assert model.data(model.index(0, 0), Qt.BackgroundRole) is not None


# ---- queue ----------------------------------------------------------------

def test_queue_changes_status_and_requirement():
    model = _model_with_ecg()
    group = model.group_at(0)
    req = group.requirement.edited(rating_min=16.0)
    cand = _cand()
    model.queue(0, req, cand, assignment_plan(group, req, cand))
    assert model.status(0) == STATUS_QUEUED
    assert _cell(model, 0, COL_STATUS) == STATUS_QUEUED
    assert _cell(model, 0, COL_MPN) == "CL10B104KB8NNNC"
    assert _cell(model, 0, COL_RATING) == "16V (edited)"
    assert model.data(model.index(0, 0), Qt.BackgroundRole) is None
    assert model.status(1) == STATUS_UNASSIGNED
    assert model.summary() == {STATUS_ASSIGNED: 0, STATUS_QUEUED: 1, STATUS_UNASSIGNED: 3,
                               "conflict": 0, "not searchable": 0, "no part": 0}


def test_accepted_edits_shape_keeps_none_values():
    model = _model_with_ecg()
    group = model.group_at(0)
    req = group.requirement.edited(rating_min=16.0)
    cand = _cand()
    plan = assignment_plan(group, req, cand)
    # A removal, as for an alias spelling of a part-number field.
    (src, by_uuid), = plan.items()
    first_uuid = next(iter(by_uuid))
    plan[src][first_uuid]["Digikey PN"] = None
    model.queue(0, req, cand, plan)

    edits = model.accepted_edits()
    assert set(edits) == {Path(ECG)}
    by_uuid = edits[Path(ECG)]
    assert len(by_uuid) == 7
    props = by_uuid[first_uuid]
    assert props["MPN"] == "CL10B104KB8NNNC"
    assert props["LCSC"] == "C1591"
    assert props["Voltage:Min"] == "16V"
    assert props["Class:Preferred"] == "X7R"
    assert props["Tolerance:Preferred"] == "10%"
    assert "Footprint" not in props        # the symbols already have one
    assert "Digikey PN" in props and props["Digikey PN"] is None


def test_accepted_edits_merge_rows():
    model = _model_with_ecg()
    for row in (0, 1):
        group = model.group_at(row)
        req = group.requirement.edited(rating_min=50.0)
        cand = _cand(mpn=f"MPN{row}")
        model.queue(row, req, cand, assignment_plan(group, req, cand))
    edits = model.accepted_edits()
    assert len(edits[Path(ECG)]) == 7 + 5


def test_load_clears_queue():
    model = _model_with_ecg()
    group = model.group_at(0)
    req = group.requirement.edited(rating_min=16.0)
    model.queue(0, req, _cand(), assignment_plan(group, req, _cand()))
    model.load(_groups())
    assert model.status(0) == STATUS_UNASSIGNED
    assert model.accepted_edits() == {}


def test_merge_plans_later_wins():
    a = {Path("x"): {"u1": {"MPN": "A", "LCSC": "C1"}}}
    b = {Path("x"): {"u1": {"MPN": "B", "Digikey PN": None}, "u2": {"MPN": "C"}}}
    assert merge_plans([a, b]) == {
        Path("x"): {"u1": {"MPN": "B", "LCSC": "C1", "Digikey PN": None}, "u2": {"MPN": "C"}}
    }


# ---- settings and board label --------------------------------------------

def test_requirement_settings_only_default_and_edited():
    group = _groups()[0]       # 100 nF, footprint (package from schematic)
    req = group.requirement.edited(rating_min=16.0)
    assert requirement_settings(req) == {
        "cap.voltage_min": "16V",
        "cap.dielectric.mid": "X7R",
        "cap.tolerance.class2": "10%",
    }
    small = _groups()[1].requirement.edited(dielectrics=("C0G",), tolerance_max_pct=2.0)
    assert requirement_settings(small) == {
        "cap.dielectric.small": "C0G",
        "cap.tolerance.class1": "2%",
    }


def test_requirement_settings_ignore_a_relaxed_tolerance():
    from dataclasses import replace
    req = _groups()[0].requirement.edited(rating_min=16.0)
    req = replace(req, tolerance_max_pct=Sourced(20.0, RELAXED))
    assert "cap.tolerance.class2" not in requirement_settings(req)


def test_board_label(tmp_path):
    root = tmp_path / "Boards"
    board = root / "ECG" / "ECG.kicad_sch"
    board.parent.mkdir(parents=True)
    board.write_text("")
    assert board_label(board, root) == "ECG/ECG.kicad_sch"
    other = tmp_path / "elsewhere.kicad_sch"
    other.write_text("")
    assert board_label(other, root) == str(other.resolve())


# ---- headless MainWindow smoke test ----------------------------------------

@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "parts.sqlite"))
    import kicadtools.gui.main_window as mw

    monkeypatch.setattr(mw, "_LCSC_CACHE", tmp_path / ".kicadtools" / "lcsc_cache.sqlite")
    app = QApplication.instance() or QApplication([])
    board = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, board)
    win = mw.MainWindow(str(board))
    yield win, board
    win.setWindowModified(False)
    win.close()
    app.processEvents()


def test_main_window_parts_tab_round_trip(window, tmp_path):
    win, board = window
    model = win.parts_model
    # ECG: 4 capacitor groups, 9 resistor groups, 1 LED group (D1/D4), 3 IC
    # groups, then 5 through-hole header groups (THT_PLAN.md step 9).
    assert model.rowCount() == 22
    assert "22 part groups, 22 unassigned" in win.statusBar().currentMessage()

    group = model.group_at(0)
    req = group.requirement.edited(rating_min=16.0)
    cand = _cand()
    win._accept_candidate(0, group, req, cand)
    assert model.status(0) == STATUS_QUEUED
    assert win.isWindowModified()
    assert win._parts_db.get_setting("cap.voltage_min") == "16V"

    edits = win._merged_edits()
    assert win._change_count(edits) >= 7 * 7
    assert win._write_in_place(edits)

    assert model.status(0) == STATUS_ASSIGNED
    assert _cell(model, 0, COL_MPN) == "CL10B104KB8NNNC"
    assert _cell(model, 0, COL_RATING) == "16V"          # now stated on the schematic
    assert not win.isWindowModified()
    assert model.status(1) == STATUS_UNASSIGNED
    assert (tmp_path / "parts.sqlite").exists()
    # The usage rows name the board by its absolute path (outside ~/Code/Boards).
    hits = win._parts_db.find_parts(kind="capacitor",
        nominal=100e-9, package="0603", rating_min=16.0,
        dielectrics=("X7R",), tolerance_max_pct=10.0,
    )
    assert [h.mpn for h in hits] == ["CL10B104KB8NNNC"]
    assert hits[0].usage_count == 7


# ---- review additions --------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from PySide6.QtCore import QObject, QThread, Signal  # noqa: E402

from kicadtools.partsearch import SearchResult  # noqa: E402
from kicadtools.requirement import (  # noqa: E402
    PartRequirement,
    Member,
    RequirementGroup,
)


def test_sourced_cell_package_from_non_chip_footprint():
    assert sourced_cell(Sourced(None, SCHEMATIC), "package", CAPACITOR) == "not a chip size"


def test_set_defaults_refreshes_unqueued_rows_only():
    model = _model_with_ecg()
    group = model.group_at(0)
    req = group.requirement.edited(rating_min=16.0)
    model.queue(0, req, _cand(), assignment_plan(group, req, _cand()))
    model.set_defaults(part_defaults({"cap.voltage_min": "25V",
                                           "cap.dielectric.small": "C0G"}))
    assert _cell(model, 0, COL_RATING) == "16V (edited)"      # queued: as accepted
    assert _cell(model, 1, COL_RATING) == "25V (default)"
    assert _cell(model, 1, COL_DIELECTRIC) == "C0G (default)"
    assert model.group_at(1).requirement.rating_min == Sourced(25.0, DEFAULT)


def test_shared_uuid_gives_one_plan_entry():
    """A subsheet symbol instantiated twice: two references, one uuid."""
    req = PartRequirement(kind="capacitor",
        value_text="100nF", nominal=100e-9,
        package=Sourced("0603", DEFAULT), rating_min=Sourced(16.0, EDITED),
        dielectrics=Sourced(("X7R",), DEFAULT), tolerance_max_pct=Sourced(10.0, DEFAULT),
    )
    src = Path("sub.kicad_sch")
    group = RequirementGroup(req, [
        Member("C1", src, "u-1", {"Digikey PN": ""}, None),
        Member("C101", src, "u-1", {"Digikey PN": ""}, None),
    ])
    model = PartsTableModel([group])
    model.queue(0, req, _cand(), assignment_plan(group, req, _cand()))
    edits = model.accepted_edits()
    assert list(edits[src]) == ["u-1"]
    assert edits[src]["u-1"]["Digikey PN"] is None
    assert edits[src]["u-1"]["Footprint"] == "Capacitor_SMD:C_0603_1608Metric"
    assert _cell(model, 0, COL_QTY) == "2"


class _GatedWorker(QObject):
    """Stands in for CandidateWorker: waits for *gate*, then returns one
    candidate whose MPN names the request's voltage (the group's own
    requirement for the first request, which sends ``None``)."""
    found = Signal(object)
    failed = Signal(str)
    gate = threading.Event()
    run_threads: list = []
    inits: list = []

    def __init__(self, *, group, defaults, requirement, needed, db_path,
                 search_suppliers=False, cache_path=None, existing=None):
        super().__init__()
        self.req = requirement or group.requirement
        type(self).inits.append(dict(group=group, requirement=requirement,
                                     existing=existing))

    def run(self):
        type(self).run_threads.append(threading.get_ident())
        type(self).gate.wait(5)
        v = self.req.rating_min.value
        mpn = f"V{v:g}" if v is not None else "Vnone"
        self.found.emit(SearchResult([_cand(mpn=mpn)]))
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
def gated(monkeypatch):
    import kicadtools.gui.main_window as mw
    monkeypatch.setattr(mw, "CandidateWorker", _GatedWorker)
    _GatedWorker.gate.clear()
    _GatedWorker.run_threads.clear()
    _GatedWorker.inits.clear()
    infos = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: infos.append(a))
    yield infos
    _GatedWorker.gate.set()


def test_window_search_off_gui_thread_busy_and_stale(window, gated):
    from kicadtools.gui.assign_dialog import AssignDialog

    win, _board = window
    infos = gated
    group = win.parts_model.group_at(0)
    dialog = AssignDialog(group, group.requirement.edited(rating_min=16.0), 1, win)
    win._assign_dialog = dialog
    dialog.search_requested.connect(win._request_candidates)

    assert dialog.request_search()
    assert win._busy()
    # Open and Save are refused while the worker runs.
    win._open_dialog()
    assert not win._save()
    assert len(infos) == 2

    # A second request while the first runs is kept; the first's outcome is stale.
    dialog._fields["rating"].setText("25V")
    assert dialog.request_search()
    assert win._search_next is not None
    _GatedWorker.gate.set()
    _wait_idle(win)
    assert dialog.chosen().mpn == "V25"
    assert len(_GatedWorker.run_threads) == 2
    # The first request sends no requirement; both name the dialog's group.
    assert _GatedWorker.inits[0]["requirement"] is None
    assert _GatedWorker.inits[1]["requirement"].rating_min.value == 25.0
    assert all(i["group"] is group for i in _GatedWorker.inits)
    assert threading.get_ident() not in _GatedWorker.run_threads


def test_window_drops_result_for_closed_dialog(window, gated):
    from kicadtools.gui.assign_dialog import AssignDialog

    win, _board = window
    group = win.parts_model.group_at(0)
    dialog = AssignDialog(group, group.requirement.edited(rating_min=16.0), 1, win)
    win._assign_dialog = dialog
    dialog.search_requested.connect(win._request_candidates)
    assert dialog.request_search()
    win._assign_dialog = None          # the dialog has closed
    _GatedWorker.gate.set()
    _wait_idle(win)
    assert dialog._candidates == []


def test_window_assign_group_end_to_end(window, gated, monkeypatch):
    """Double-click path: dialog opens, search runs on the (fake) worker, the
    first candidate is accepted; settings are saved and the next group's
    defaulted voltage is the one accepted for the first group."""
    from PySide6.QtWidgets import QApplication, QDialog
    import kicadtools.gui.main_window as mw

    win, _board = window
    _GatedWorker.gate.set()

    def fake_exec(dialog):
        dialog._fields["rating"].setText("16V")
        dialog.request_search(False)
        deadline = time.monotonic() + 10
        while dialog.chosen() is None and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        return QDialog.Accepted

    monkeypatch.setattr(mw.AssignDialog, "exec", fake_exec)
    win._assign_group(0)
    _wait_idle(win)
    assert win._assign_dialog is None
    assert win.parts_model.status(0) == STATUS_QUEUED
    assert _cell(win.parts_model, 0, COL_MPN) == "V16"
    assert win._parts_db.get_setting("cap.voltage_min") == "16V"
    assert _cell(win.parts_model, 1, COL_RATING) == "16V (default)"
    edits = win._merged_edits()
    props = next(iter(edits[board_path(win)].values()))
    assert props["Voltage:Min"] == "16V"


def board_path(win):
    return Path(win._board)


def test_close_event_closes_parts_db(window):
    win, _board = window
    assert win._parts_db is not None
    win.close()
    assert win._parts_db is None
    win._reject_candidate(_cand())       # no database: a no-op, not an error


def test_footprint_without_chip_size_reads_not_searchable():
    from kicadtools.gui.parts import STATUS_NOT_SEARCHABLE, PartsTableModel
    records = iter_bom_part_records(Path(__file__).parent / "data" / "MotorDriver.kicad_sch")
    model = PartsTableModel()
    model.load(group_parts(records, part_defaults({"cap.voltage_min": "16V"})))
    radial = [r for r in range(model.rowCount())
              if "CP_Radial" in model.group_at(r).requirement.footprint]
    assert radial
    assert all(model.status(r) == STATUS_NOT_SEARCHABLE for r in radial)
    assert model.summary()[STATUS_NOT_SEARCHABLE] == len(radial)


# ---- revision 2: current part, conflicts, evidence markers ---------------------

from dataclasses import replace as _replace  # noqa: E402

from kicadtools.partsearch import CurrentOption, ExistingLookup  # noqa: E402
from kicadtools.evidence import PartEvidence  # noqa: E402
from kicadtools.gui.parts import (  # noqa: E402
    COL_CURRENT,
    STATUS_CONFLICT,
    CandidateWorker,
    current_part_cell,
    value_cell,
)
from kicadtools.requirement import DESCRIPTION, PART  # noqa: E402


def _rev2_req(**kw) -> PartRequirement:
    base = dict(kind="capacitor",
        value_text="100nF", nominal=100e-9,
        package=Sourced("0603", DEFAULT), rating_min=Sourced(16.0, DEFAULT),
        dielectrics=Sourced(("X7R",), DEFAULT), tolerance_max_pct=Sourced(10.0, DEFAULT),
    )
    base.update(kw)
    return PartRequirement(**base)


def _rev2_group(req, props=None) -> RequirementGroup:
    return RequirementGroup(req, [Member("C1", Path("b.kicad_sch"), "u-1",
                                         dict(props or {}), None)])


def test_sourced_cell_part_and_description_markers():
    assert sourced_cell(Sourced(25.0, PART), "rating", CAPACITOR) == "25V (part)"
    assert sourced_cell(Sourced(("X5R", "X7R"), DESCRIPTION), "dielectric", CAPACITOR) == \
        "X5R, X7R (description)"
    assert sourced_cell(Sourced("0805", DESCRIPTION), "package", CAPACITOR) == "0805 (description)"


def test_value_cell_from_description():
    assert value_cell(_rev2_req()) == "100nF"
    req = _rev2_req(value_text="", nominal=10e-6, nominal_source=DESCRIPTION)
    assert value_cell(req) == "10uF (description)"
    assert value_cell(_rev2_req(value_text="", nominal=None)) == DASH


def test_current_part_column():
    model = _model_with_ecg()
    # ECG's 100 nF group holds a DigiKey PN and no MPN.
    assert _cell(model, 0, COL_CURRENT) == model.group_at(0).existing.digikey_pn
    assert model.group_at(0).existing.mpn is None
    assert _cell(model, 0, COL_CURRENT).endswith("-ND")
    bare = _rev2_group(_rev2_req())
    assert current_part_cell(bare) == DASH
    swapped = _rev2_group(_rev2_req(), {"MPN": "1276-1871-1-ND",
                                        "DigiKeyPN": "1276-1869-1-ND"})
    assert current_part_cell(swapped) == "1276-1871-1-ND"


def test_description_warning_is_not_a_conflict():
    line = "Description says 0603; Footprint says 0402 (Description not used)"
    model = PartsTableModel([_rev2_group(_rev2_req(warnings=(line,)))])
    assert model.status(0) == STATUS_UNASSIGNED
    assert line in model.data(model.index(0, COL_STATUS), Qt.ToolTipRole)


def test_conflict_status_tooltip_and_summary():
    conflicted = _rev2_req(conflicts=("Value says 10nF; Description says 10uF",))
    # A conflict outranks "not searchable" (no chip size in the footprint).
    radial = _rev2_req(footprint="Capacitor_THT:CP_Radial_D6.3mm_P2.50mm",
                       package=Sourced(None, SCHEMATIC),
                       conflicts=("Value says 10nF; Description says 10uF",))
    model = PartsTableModel([_rev2_group(conflicted), _rev2_group(radial),
                             _rev2_group(_rev2_req())])
    assert model.status(0) == STATUS_CONFLICT
    assert model.status(1) == STATUS_CONFLICT
    assert model.status(2) == STATUS_UNASSIGNED
    assert _cell(model, 0, COL_STATUS) == "conflict"
    tip = model.data(model.index(0, COL_STATUS), Qt.ToolTipRole)
    assert "Value says 10nF; Description says 10uF" in tip
    assert model.data(model.index(0, 0), Qt.BackgroundRole) is not None
    assert model.summary()[STATUS_CONFLICT] == 2
    # Queued takes precedence over a conflict.
    group = model.group_at(0)
    model.queue(0, conflicted, _cand(), {})
    assert model.status(0) == STATUS_QUEUED


def test_set_defaults_keeps_description_evidence(tmp_path):
    from kicadtools.intent import CapacitorIntent
    from kicadtools.requirement import part_requirement
    from kicadtools.units import parse_cap_description

    intent = CapacitorIntent(reference="C1", dnp=False, in_bom=True, value="10uF")
    props = {"Value": "10uF", "Description": "10uF, 25V, X5R, 805 package"}
    req = part_requirement(intent, "", part_defaults({}),
                                description=parse_cap_description(props["Description"]))
    group = RequirementGroup(req, [Member("C1", Path("b.kicad_sch"), "u-1", props, intent)])
    model = PartsTableModel([group])
    model.set_defaults(part_defaults({"cap.voltage_min": "50V"}))
    r = model.group_at(0).requirement
    assert r.rating_min == Sourced(25.0, DESCRIPTION)
    assert r.package == Sourced("0805", DESCRIPTION)
    assert _cell(model, 0, COL_PACKAGE) == "0805 (description)"


def test_requirement_settings_ignore_part_and_description():
    req = _rev2_req(
        package=Sourced("0805", DESCRIPTION),
        rating_min=Sourced(25.0, PART),
        dielectrics=Sourced(("X5R", "X7R"), PART),
        tolerance_max_pct=Sourced(10.0, PART),
    )
    assert requirement_settings(req) == {}
    # A default tolerance alongside part dielectrics is still remembered.
    mixed = _replace(req, tolerance_max_pct=Sourced(20.0, EDITED))
    assert requirement_settings(mixed) == {"cap.tolerance.class2": "20%"}


def test_worker_calls_assess(tmp_path, monkeypatch):
    import kicadtools.gui.parts as parts

    calls = []
    lookup = ExistingLookup(notes=["looked up"])
    result = SearchResult([_cand()])

    def fake_assess(group, defaults, **kw):
        calls.append((group, defaults, kw))
        return result

    monkeypatch.setattr(parts, "assess", fake_assess)
    group = _rev2_group(_rev2_req())
    req = _rev2_req(rating_min=Sourced(50.0, EDITED))
    worker = CandidateWorker(group=group, defaults={"cap.package": "0805"},
                             requirement=req, needed=30,
                             db_path=tmp_path / "parts.sqlite",
                             search_suppliers=True, existing=lookup)
    found, failed = [], []
    worker.found.connect(found.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == [] and found == [result]
    (g, defaults, kw), = calls
    assert g is group
    assert defaults == {"cap.package": "0805"}
    assert kw["requirement"] is req
    assert kw["existing"] is lookup
    assert kw["needed"] == 30 and kw["search_suppliers"] is True

    # The first request: no requirement, no lookup.
    calls.clear()
    CandidateWorker(group=group, defaults={}, requirement=None, needed=1,
                    db_path=tmp_path / "parts.sqlite").run()
    assert calls[0][2]["requirement"] is None and calls[0][2]["existing"] is None


def test_worker_reports_assess_failure(tmp_path, monkeypatch):
    import kicadtools.gui.parts as parts

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(parts, "assess", boom)
    worker = CandidateWorker(group=_rev2_group(_rev2_req()), defaults={},
                             requirement=None, needed=1,
                             db_path=tmp_path / "parts.sqlite")
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == ["no network"]


def test_window_passes_existing_lookup_to_later_requests(window, gated):
    """The window reads the dialog's ExistingLookup when a search starts, so a
    request after the first result reuses it."""
    from kicadtools.gui.assign_dialog import AssignDialog

    win, _board = window
    group = win.parts_model.group_at(0)
    dialog = AssignDialog(group, group.requirement, 1, win)
    win._assign_dialog = dialog
    dialog.search_requested.connect(win._request_candidates)
    lookup = ExistingLookup(notes=["1276-1000-1-ND at DigiKey"])
    dialog.show_result(SearchResult([], existing=lookup), None)
    dialog._fields["rating"].setText("25V")
    assert dialog.request_search()
    _GatedWorker.gate.set()
    _wait_idle(win)
    assert _GatedWorker.inits[-1]["existing"] is lookup
    assert _GatedWorker.inits[-1]["requirement"].rating_min.value == 25.0


def test_window_status_bar_counts_conflicts(window):
    win, _board = window
    g = win.parts_model.group_at(0)
    g.requirement = _replace(g.requirement, conflicts=("Value says 1nF; Description says 1uF",))
    win._refresh_status()
    assert "1 with conflicts" in win.statusBar().currentMessage()


# ---- review of the Cockpit stage ---------------------------------------------

_FULL = {"MPN": "CL10B104KB8NNNC", "Manufacturer": "Samsung",
         "DigiKeyPN": "1276-1000-1-ND", "LCSC": "C1591",
         "Footprint": "Capacitor_SMD:C_0603_1608Metric"}


def test_conflict_outranks_assigned():
    conflicted = _rev2_req(conflicts=("Value says 10nF; Description says 10uF",))
    model = PartsTableModel([_rev2_group(conflicted, _FULL), _rev2_group(_rev2_req(), _FULL)])
    assert model.group_at(0).assigned
    assert model.status(0) == STATUS_CONFLICT
    assert model.status(1) == STATUS_ASSIGNED


def test_value_cell_library_style_shows_capacitance():
    from kicadtools.intent import CapacitorIntent
    from kicadtools.requirement import part_requirement

    def cell(value):
        intent = CapacitorIntent(reference="C1", dnp=False, in_bom=True, value=value)
        return value_cell(part_requirement(intent, "", part_defaults({})))

    assert cell("C_4p7U") == "C_4p7U (4.7uF)"
    assert cell("C_P1U") == "C_P1U (100nF)"
    assert cell("C_10U_805") == "C_10U_805 (10uF)"
    assert cell("0.1uF") == "0.1uF"
    assert cell("100nF") == "100nF"


def _ecg_part_group():
    from kicadtools.intent import CapacitorIntent
    from kicadtools.requirement import part_requirement

    intent = CapacitorIntent(reference="C1", dnp=False, in_bom=True, value="100nF")
    props = {"Value": "100nF", "DigiKeyPN": "1276-1000-1-ND"}
    req = part_requirement(intent, "", part_defaults({}))
    return RequirementGroup(req, [Member("C1", Path("b.kicad_sch"), "u-1", props, intent)])


def test_record_lookup_keeps_evidence_and_warnings_through_set_defaults():
    model = PartsTableModel([_ecg_part_group()])
    assert model.lookup_at(0) is None
    evidence = PartEvidence(label="current part 1276-1000-1-ND (DigiKey)",
                            nominal=100e-9, rating=50.0,
                            dielectrics=("X7R",), tolerance_pct=10.0, package="0603")
    lookup = ExistingLookup(options=[CurrentOption(("DigiKeyPN",), evidence)])
    model.record_lookup(0, lookup, part_defaults({}))
    assert model.lookup_at(0) is lookup
    assert _cell(model, 0, COL_RATING) == "50V (part)"
    # A later settings change keeps the part's evidence.
    model.set_defaults(part_defaults({"cap.voltage_min": "16V"}))
    assert _cell(model, 0, COL_RATING) == "50V (part)"

    # a second part of another capacitance is not used: a warning, no conflict
    other = PartEvidence(label="current part B (DigiKey)", nominal=10e-6)
    model.record_lookup(0, ExistingLookup(options=[
        CurrentOption(("DigiKeyPN",), evidence), CurrentOption(("MPN",), other)]),
        part_defaults({}))
    model.set_defaults(part_defaults({}))
    assert model.status(0) == STATUS_UNASSIGNED
    assert len(model.group_at(0).requirement.warnings) == 1
    assert _cell(model, 0, COL_RATING) == "50V (part)"
    # A reload clears recorded lookups.
    model.load([_ecg_part_group()])
    assert model.lookup_at(0) is None
    assert model.status(0) == STATUS_UNASSIGNED


def test_window_records_lookup_and_reuses_it(window, gated, monkeypatch):
    """The dialog's lookup is recorded on the row when it closes, and the next
    dialog for that row starts with it (no second lookup)."""
    from PySide6.QtWidgets import QDialog
    import kicadtools.gui.main_window as mw

    win, _board = window
    lookup = ExistingLookup(notes=["found"])
    opened = []

    def fake_exec(dialog):
        opened.append(dialog.existing_lookup)
        if dialog.existing_lookup is None:
            dialog.show_result(SearchResult([], existing=lookup), None)
        return QDialog.Rejected

    monkeypatch.setattr(mw.AssignDialog, "exec", fake_exec)
    _GatedWorker.gate.set()
    win._assign_group(0)
    _wait_idle(win)
    assert win.parts_model.lookup_at(0) is lookup
    win._assign_group(0)
    _wait_idle(win)
    assert opened == [None, lookup]
