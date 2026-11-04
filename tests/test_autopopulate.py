"""Tests for Autopopulate (kicadtools.autopopulate, gui/autopopulate_dialog.py
and the window's path): database matches only, graded exact / near / less
sure."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicadtools.autopopulate import EXACT, NEAR, UNSURE, find_matches, passive_tier
from kicadtools.partsdb import PartChoice, PartParams, PartsDb
from kicadtools.requirement import group_parts, part_defaults
from kicadtools.resolve import PartRecord
from kicadtools.intent import ElectricalIntent
from kicadtools.thtassign import ThtGroup, ThtRequirement
from kicadtools.requirement import Member
from kicadtools.thtdb import tht_choice
from kicadtools.thtfootprint import parse_signature
from kicadtools.suppliers import DigiKeyProduct

DEFAULTS = part_defaults({"cap.voltage_min": "16V"})
FP = "Capacitor_SMD:C_0603_1608Metric"


def _rec(ref, **props):
    data = {"Reference": ref, "dnp": False, "in_bom": True, "Intent:Type": "capacitor",
            "Value": "10uF", "Footprint": FP, **props}
    return PartRecord(ref, data, Path("/b/x.kicad_sch"), f"u-{ref}", ElectricalIntent.from_dict(data))


def _db_cap(db, mpn, volts=25.0, diel="X5R", tol=10.0, lcsc="C1", uses=0):
    pid = db.accept_part(PartChoice(kind="capacitor", mpn=mpn, manufacturer="Samsung",
                                    package="0603", params=PartParams(10e-6, volts, diel, tol),
                                    digikey_pn=f"{mpn}-ND", lcsc=lcsc, jlc_library="basic"))
    if uses:
        db.record_usage(pid, "b", [f"C{100 + i}" for i in range(uses)])
    return pid


STATED = {"Voltage:Min": "16V", "Class:Preferred": "X5R", "Tolerance:Preferred": "10%"}


@pytest.mark.parametrize("stored, props, tier", [
    (dict(volts=16.0), STATED, EXACT),
    (dict(volts=25.0), STATED, NEAR),                       # exceeds the rating
    (dict(diel="X7R"), {**STATED, "Voltage:Min": "25V"}, NEAR),   # better class
    (dict(tol=5.0), {**STATED, "Voltage:Min": "25V"}, NEAR),      # tighter tolerance
    (dict(volts=25.0), {}, UNSURE),                         # every field a default
    (dict(volts=25.0), {"Voltage:Min": "16V"}, UNSURE),     # class and tolerance default
])
def test_passive_tiers(stored, props, tier):
    db = PartsDb()
    _db_cap(db, "CL10A106", **stored)
    groups = group_parts([_rec("C1", **props)], DEFAULTS)
    (rm,) = find_matches(groups, db, open_rows=[0])
    assert rm.best.tier == tier, rm.best.why


def test_part_the_symbol_names_is_exact_even_with_defaults():
    db = PartsDb()
    _db_cap(db, "CL10A106", lcsc="C96446")
    groups = group_parts([_rec("C1", LCSC="C96446")], DEFAULTS)
    (rm,) = find_matches(groups, db, open_rows=[0])
    assert (rm.best.mpn, rm.best.tier) == ("CL10A106", EXACT)


def test_matches_ordered_by_usage_then_tier_and_unmatched_rows_left_out():
    db = PartsDb()
    _db_cap(db, "A-EXACT", volts=16.0)
    _db_cap(db, "B-USED", volts=50.0, lcsc="C2", uses=3)
    groups = group_parts([_rec("C1", **STATED), _rec("C9", Value="22uF")], DEFAULTS)
    rows = find_matches(groups, db, open_rows=[0, 1])
    assert len(rows) == 1
    assert [(m.mpn, m.tier) for m in rows[0].matches] == [("B-USED", NEAR), ("A-EXACT", EXACT)]


def test_no_supplier_is_called():
    db = PartsDb()
    _db_cap(db, "CL10A106")
    groups = group_parts([_rec("C1")], DEFAULTS)
    (rm,) = find_matches(groups, db, open_rows=[0])
    c = rm.best.candidate
    assert c.jlc_stock is None and c.digikey_stock is None


def _tht_group(footprint, value="Conn_01x04", **props):
    sig = parse_signature(footprint, 4)
    allprops = {"Footprint": footprint, "Value": value, **props}
    req = ThtRequirement(signature=sig, footprint=footprint, value_text=value, pad_count=4,
                         props=allprops)
    return ThtGroup(req, [Member("J1", Path("/b/x.kicad_sch"), "u-J1", allprops, None)])


def test_through_hole_tiers():
    db = PartsDb()
    hdr = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
    jst = "Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal"
    p = DigiKeyProduct(mpn="PREC004SAAN-RC", manufacturer="Sullins", digikey_pns=["S-ND"])
    db.accept_part(tht_choice(p, parse_signature(hdr), ""))
    q = DigiKeyProduct(mpn="S3B-PH-K-S", manufacturer="JST", digikey_pns=["455-1720-ND"])
    db.accept_part(tht_choice(q, parse_signature(jst), "I2C_Conn"))
    groups = [_tht_group(hdr), _tht_group(jst, "I2C_Conn"), _tht_group(hdr, DigiKeyPN="S-ND")]
    rows = find_matches(groups, db, open_rows=[0, 1, 2])
    assert [r.best.tier for r in rows] == [UNSURE, NEAR, EXACT]
    assert rows[0].best.candidate.digikey_pn == "S-ND"


# ---- dialog and window ------------------------------------------------------

pytest.importorskip("PySide6")


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_dialog_colours_check_green_and_chosen():
    _app()
    from kicadtools.gui.autopopulate_dialog import (
        COL_ACCEPT, TIER_COLOURS, AutopopulateDialog, summary_text)
    from kicadtools.gui.parts import PartsTableModel
    db = PartsDb()
    _db_cap(db, "A-EXACT", volts=16.0)
    _db_cap(db, "B-NEAR", volts=50.0, lcsc="C2", uses=3)
    groups = group_parts([_rec("C1", **STATED), _rec("C2", **{**STATED, "Voltage:Min": "35V"})],
                         DEFAULTS)
    model = PartsTableModel(groups)
    rows = find_matches(groups, db, open_rows=[0, 1])
    dlg = AutopopulateDialog(model, rows)
    assert "2 rows with a stored part" in summary_text(rows)
    assert dlg.tier_at(0) == NEAR            # B-NEAR first: most uses
    dlg._combos[0].setCurrentIndex(1)        # pick A-EXACT
    assert dlg.tier_at(0) == EXACT
    assert TIER_COLOURS[EXACT].name() in dlg._holders[0].styleSheet()
    assert dlg.chosen() == []
    dlg.check_exact()
    assert [(r, m.mpn) for r, m in dlg.chosen()] == [(0, "A-EXACT")]


@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "parts.sqlite"))
    import kicadtools.gui.main_window as mw
    monkeypatch.setattr(mw, "_LCSC_CACHE", tmp_path / ".kicadtools" / "lcsc_cache.sqlite")
    app = QApplication.instance() or QApplication([])
    board = tmp_path / "ECG.kicad_sch"
    shutil.copy(Path(__file__).parent / "data" / "ECG.kicad_sch", board)
    win = mw.MainWindow(str(board))
    yield win, board
    win.setWindowModified(False)
    win.close()
    app.processEvents()


def test_window_autopopulate_queues_checked_rows(window, monkeypatch):
    import kicadtools.gui.main_window as mw
    from PySide6.QtWidgets import QDialog
    from kicadtools.gui.parts import STATUS_QUEUED
    win, board = window
    model = win.parts_model
    # The first capacitor group of ECG, stored with the requirement's values.
    win._parts_db.set_setting("cap.voltage_min", "16V")
    win.parts_model.set_defaults(win._part_defaults())
    row = 0
    req = model.group_at(row).requirement
    assert req.searchable
    win._parts_db.accept_part(PartChoice(
        kind="capacitor", mpn="STORED-1", manufacturer="Samsung", package=req.package.value,
        params=PartParams(float(req.nominal), 100.0, "X7R", 5.0),
        digikey_pn="STORED-1-ND", lcsc="C999", jlc_library="basic"))

    def exec_checking_all(dialog):
        dialog._set_all(True)
        return QDialog.Accepted
    monkeypatch.setattr(mw.AutopopulateDialog, "exec", exec_checking_all)
    win._autopopulate()
    assert model.status(row) == STATUS_QUEUED
    assert model.chosen_mpn(row) == "STORED-1"
    assert "1 row(s) queued" in win.statusBar().currentMessage()
    assert win.isWindowModified()
    (hit,) = [h for h in win._parts_db.find_parts(
        kind="capacitor", nominal=float(req.nominal), package=req.package.value,
        rating_min=None, dielectrics=(), tolerance_max_pct=None) if h.mpn == "STORED-1"]
    assert hit.usage_count == model.group_at(row).quantity


def test_window_autopopulate_with_nothing_stored(window):
    win, _ = window
    win._autopopulate()
    assert "No stored part matches" in win.statusBar().currentMessage()
