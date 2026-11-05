"""LED plan step 5: the write plan, the Parts-tab LED row, the LED dialog,
Autopopulate, and the window's LED path with a fake worker."""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QThread, Signal

from kicadtools.autopopulate import EXACT, UNSURE, find_matches
from kicadtools.gui.led_assign_dialog import LedAssignDialog, edited_requirement
from kicadtools.gui.parts import (
    COL_DIELECTRIC,
    COL_MPN,
    COL_RATING,
    COL_VALUE,
    STATUS_ASSIGNED,
    STATUS_QUEUED,
    STATUS_UNASSIGNED,
    PartsTableModel,
    is_led,
)
from kicadtools.icsearch import ic_assignment_plan
from kicadtools.ledassign import led_assignment_plan
from kicadtools.ledrequirement import LedEvidence, group_leds
from kicadtools.ledsearch import LedCandidate
from kicadtools.partsdb import PartParams, PartsDb
from kicadtools.partsearch import SEARCH, SearchResult
from kicadtools.requirement import EDITED, part_defaults
from kicadtools.resolve import PartRecord, iter_bom_part_records

DATA = Path(__file__).parent / "data"
ECG = DATA / "ECG.kicad_sch"
DEFAULTS = part_defaults({})


def _ecg_led_group():
    plain, _ = group_leds(iter_bom_part_records(ECG), DEFAULTS)
    return plain[0]


def _cand(mpn="LTST-C190KRKT", dominant=631.0):
    ev = LedEvidence("x", package="0603", dominant=(dominant,), labels=("red",),
                     intensity_mcd=(99.0,), vf=(2.0,), lens="clear")
    return LedCandidate(kind="led", mpn=mpn, manufacturer="Lite-On", package="0603",
                        params=PartParams(dominant, None, "red", None), lcsc="C94869",
                        jlc_library="extended", jlc_preferred=False, jlc_stock=50_000,
                        jlc_price=0.01, digikey_pn=f"{mpn}CT-ND", digikey_stock=9000,
                        source=SEARCH, led=ev)


# --- write plan ---------------------------------------------------------- #

def test_led_plan_writes_part_numbers_and_colour_read_from_the_value():
    group = _ecg_led_group()
    req = group.requirement.edited(intensity_min=50)
    plan = led_assignment_plan(group, req, _cand())
    members = [p for f in plan.values() for p in f.values()]
    assert len(members) == 2
    props = members[0]
    assert props["MPN"] == "LTST-C190KRKT"
    assert props["DigiKeyPN"] == "LTST-C190KRKTCT-ND"
    assert props["LCSC"] == "C94869"
    assert props["Manufacturer"] == "Lite-On"
    assert props["Color:Preferred"] == "red"
    assert props["Intensity:Min"] == "50mcd"
    assert props["Intent:Type"] == "led"
    assert props["DigikeyPN"] is None            # alias spelling removed
    assert "Footprint" not in props and "Value" not in props


def test_led_plan_refuses_missing_lcsc():
    with pytest.raises(ValueError, match="LCSC"):
        c = _cand()
        c.lcsc = None
        led_assignment_plan(_ecg_led_group(), _ecg_led_group().requirement, c)


def test_named_led_stays_an_led_when_assigned():
    p = {"Reference": "D1", "dnp": False, "in_bom": True, "Value": "LED_RG_LTSTC155GEKT",
         "Footprint": "StockedComponents:LTST-C155GEKT"}
    from kicadtools.components import build_intent
    rec = PartRecord("D1", p, Path("b.kicad_sch"), "u1",
                     build_intent(p, Path("b.kicad_sch"), "u1"))
    assert rec.intent.intent_guessed
    _, (named,) = group_leds([rec], DEFAULTS)

    class Cand:
        mpn, manufacturer, digikey_pn, lcsc, at_jlcpcb = (
            "LTST-C155GEKT", "Lite-On", "160-1185-1-ND", "C1", True)
    (props,) = ic_assignment_plan(named, Cand())[Path("b.kicad_sch")].values()
    assert props["Intent:Type"] == "led"


# --- Parts-tab row and dialog ------------------------------------------- #

def test_parts_row_cells():
    group = _ecg_led_group()
    model = PartsTableModel([group])
    assert is_led(group)
    assert model.cell_text(0, COL_VALUE) == "red (value)"
    assert model.cell_text(0, COL_RATING) == "any"
    assert model.cell_text(0, COL_DIELECTRIC) == "615–645 nm"
    assert model.status(0) == STATUS_UNASSIGNED


def test_edited_requirement_from_field_texts():
    req = _ecg_led_group().requirement
    out = edited_requirement(req, color="deep red", wavelength="", intensity="80",
                             vf="2.4V", lens="clear")
    assert out.colors.value == ("deep red",) and out.colors.source == EDITED
    assert out.intensity_min.value == 80.0
    assert out.vf_max.value == pytest.approx(2.4)
    assert out.lenses.value == ("clear",)
    again = edited_requirement(out, color="deep red", wavelength="", intensity="",
                               vf="2.4V", lens="any")
    assert again.intensity_min.value is None and again.lenses.value == ()
    with pytest.raises(ValueError):
        edited_requirement(req, color="", wavelength="", intensity="bright",
                           vf="", lens="any")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_dialog_first_request_none_then_edited(app):
    group = _ecg_led_group()
    dialog = LedAssignDialog(group, group.requirement, 10)
    sent = []
    dialog.search_requested.connect(lambda req, sup: sent.append((req, sup)))
    assert dialog.needed == 20
    dialog.request_search(False)
    assert sent == [(None, False)]
    dialog.show_result(SearchResult([_cand()], requirement=group.requirement),
                       dialog.request_id)
    assert dialog.chosen().mpn == "LTST-C190KRKT"
    dialog._intensity.setText("50mcd")
    dialog._search_clicked()
    req, sup = sent[-1]
    assert sup is True and req.intensity_min.value == 50.0


# --- Autopopulate --------------------------------------------------------- #

def test_autopopulate_offers_a_stored_led_of_the_right_colour(tmp_path):
    db = PartsDb(tmp_path / "parts.sqlite")
    db.accept_part(_cand().choice())
    db.accept_part(_cand("LTST-GREEN", dominant=525.0).choice())
    group = _ecg_led_group()
    (row,) = find_matches([group], db, open_rows=[0])
    assert [m.mpn for m in row.matches] == ["LTST-C190KRKT"]
    assert row.best.tier == UNSURE          # intensity, Vf and lens not stated
    db.accept_part(_cand("150060RS75000").choice())
    (row,) = find_matches([group], db, open_rows=[0])
    assert row.best.mpn == "150060RS75000" and row.best.tier == EXACT


# --- window --------------------------------------------------------------- #

class _FakeLedWorker(QObject):
    found = Signal(object)
    failed = Signal(str)
    inits: list = []
    run_threads: list = []

    def __init__(self, *, group, defaults, requirement, needed, db_path, listing,
                 search_suppliers=False, cache_path=None, existing=None):
        super().__init__()
        self.group = group
        type(self).inits.append(dict(requirement=requirement, needed=needed,
                                     search_suppliers=search_suppliers))

    def run(self):
        type(self).run_threads.append(threading.get_ident())
        self.found.emit(SearchResult([_cand()], requirement=self.group.requirement))
        QThread.currentThread().quit()


def _accepting_exec(dialog):
    from PySide6.QtWidgets import QApplication, QDialog
    deadline = time.monotonic() + 10
    while dialog.chosen() is None and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    return QDialog.Accepted


def _wait_idle(win, timeout=10.0):
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + timeout
    while (win._busy() or win._search_running) and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()
    assert not win._busy() and not win._search_running


def test_window_assigns_an_led_end_to_end(tmp_path, monkeypatch, app):
    import kicadtools.gui.main_window as mw
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "parts.sqlite"))
    monkeypatch.setattr(mw, "_LCSC_CACHE", tmp_path / ".kicadtools" / "lcsc_cache.sqlite")
    monkeypatch.setattr(mw, "LedCandidateWorker", _FakeLedWorker)
    monkeypatch.setattr(mw.LedAssignDialog, "exec", _accepting_exec)
    _FakeLedWorker.inits.clear()
    _FakeLedWorker.run_threads.clear()
    board = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, board)
    win = mw.MainWindow(str(board))
    try:
        model = win.parts_model
        r = next(i for i in range(model.rowCount()) if is_led(model.group_at(i)))
        win._parts_double_clicked(win.parts_proxy.mapFromSource(model.index(r, 0)))
        _wait_idle(win)
        assert model.status(r) == STATUS_QUEUED
        assert model.cell_text(r, COL_MPN) == "LTST-C190KRKT"
        (init,) = _FakeLedWorker.inits
        assert init["requirement"] is None and init["search_suppliers"] is False
        assert threading.get_ident() not in _FakeLedWorker.run_threads
        (hit,) = win._parts_db.find_leds("0603")
        assert hit.mpn == "LTST-C190KRKT" and hit.usage_count == 2

        assert win._write_in_place(win._merged_edits())
        after = {rec.reference: rec for rec in iter_bom_part_records(board)}
        for ref in ("D1", "D4"):
            assert after[ref].props["MPN"] == "LTST-C190KRKT"
            assert after[ref].props["LCSC"] == "C94869"
            assert after[ref].props["Color:Preferred"] == "red"
            assert after[ref].props["Value"] == "LED_Red"
        r = next(i for i in range(win.parts_model.rowCount())
                 if is_led(win.parts_model.group_at(i)))
        assert win.parts_model.status(r) == STATUS_ASSIGNED
    finally:
        win.setWindowModified(False)
        win.close()
        app.processEvents()
