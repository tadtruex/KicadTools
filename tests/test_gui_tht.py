"""Tests for the through-hole rows of the Parts tab (gui/parts.py), the
through-hole assign dialog (gui/tht_assign_dialog.py) and the window's
through-hole path (THT_PLAN.md step 9). No network: the search worker is
faked, and the window test works on a scratch copy of the ECG fixture, whose
pin headers are through-hole."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt, QThread, Signal  # noqa: E402

from kicadtools.gui.parts import (  # noqa: E402
    COL_DIELECTRIC,
    COL_PACKAGE,
    COL_STATUS,
    COL_VALUE,
    STATUS_ASSIGNED,
    STATUS_NO_PART,
    STATUS_NOT_SEARCHABLE,
    STATUS_QUEUED,
    STATUS_UNASSIGNED,
    THT_CLASS,
    PartsTableModel,
    is_tht,
)
from kicadtools.gui.tht_assign_dialog import (  # noqa: E402
    COL_THT_PADS,
    THT_CANDIDATE_COLUMNS,
    ThtAssignDialog,
    footprint_header,
    pad_message,
    tht_candidate_cells,
    tht_result_summary,
)
from kicadtools.icsearch import MATCH, MISMATCH  # noqa: E402
from kicadtools.namedsearch import PadCheck  # noqa: E402
from kicadtools.requirement import Member  # noqa: E402
from kicadtools.resolve import iter_bom_part_records  # noqa: E402
from kicadtools.suppliers import DigiKeyProduct  # noqa: E402
from kicadtools.thtassign import (  # noqa: E402
    CURRENT,
    DATABASE,
    ThtCandidate,
    ThtGroup,
    ThtRequirement,
    ThtSearchResult,
)
from kicadtools.thtdb import db_key  # noqa: E402
from kicadtools.thtfootprint import parse_signature  # noqa: E402

DATA = Path(__file__).parent / "data"
ECG = DATA / "ECG.kicad_sch"
HDR = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
PAD = "Passives:SolderWirePad_single_0-5mmDrill"
DIN = "StockedComponents:DIN5_MIDI"


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _group(footprint=HDR, value="Conn_01x04", refs=("J2", "J4"), pads=4, **props):
    sig = parse_signature(footprint, pads)
    allprops = {"Footprint": footprint, "Value": value, **props}
    req = ThtRequirement(signature=sig, footprint=footprint, value_text=value,
                         pad_count=pads, props=allprops)
    members = [Member(reference=r, source_file=Path("/b/x.kicad_sch"), symbol_uuid=f"u-{r}",
                      props=dict(allprops), intent=None) for r in refs]
    return ThtGroup(req, members)


def _cand(mpn="PREC004SAAN-RC", stock=10000, source="search", pads=MATCH, warning="", **params):
    product = DigiKeyProduct(mpn=mpn, manufacturer="Sullins Connector Solutions",
                             quantity_available=stock, unit_price=0.1, product_status="Active",
                             digikey_pns=[f"S{mpn}-ND"], description="CONN HEADER VERT 4POS",
                             parameters=params)
    return ThtCandidate(product=product, signature=parse_signature(HDR), value="Conn_01x04",
                        source=source, label="maker list",
                        pads=PadCheck(pads, "" if pads == MATCH else "3 pads vs 4 positions"),
                        stock_warning=warning)


def _cell(model, row, col):
    return model.data(model.index(row, col), Qt.DisplayRole)


# ---- pure helpers -----------------------------------------------------------

def test_candidate_cells():
    cells = tht_candidate_cells(_cand())
    assert len(cells) == len(THT_CANDIDATE_COLUMNS)
    assert cells[:5] == ["PREC004SAAN-RC", "Sullins Connector Solutions", "maker list",
                         "SPREC004SAAN-RC-ND", "10,000"]
    assert cells[COL_THT_PADS] == "match"


def test_summary_and_messages():
    g = _group()
    res = ThtSearchResult(requirement=g.requirement, candidates=[_cand()], total=275,
                          series="Sullins Connector Solutions PREC004SAAN-RC")
    assert tht_result_summary(res) == ("1 candidate shown of 275 at DigiKey; series "
                                       "Sullins Connector Solutions PREC004SAAN-RC.")
    stored = ThtSearchResult(requirement=g.requirement, candidates=[_cand(source=DATABASE)],
                             from_database=True)
    assert "DigiKey not searched" in tht_result_summary(stored)
    assert footprint_header(g.requirement).endswith("header 1x04, 2.54 mm, vertical; 4 pads")
    assert pad_message(_cand()) == ""
    assert pad_message(_cand(pads=MISMATCH)).startswith("Pads: mismatch")


# ---- model ------------------------------------------------------------------

def test_model_cells_and_status():
    groups = [
        _group(),
        _group(PAD, "TestPoint", refs=("TP1",), pads=1),
        _group(DIN, "DIN-5_180degree", refs=("J1",), pads=7),
        _group(refs=("J9",), MPN="X", Manufacturer="Y", DigiKeyPN="X-ND"),
    ]
    model = PartsTableModel(groups)
    assert all(is_tht(g) for g in model.groups())
    assert [model.status(r) for r in range(4)] == [
        STATUS_UNASSIGNED, STATUS_NO_PART, STATUS_NOT_SEARCHABLE, STATUS_ASSIGNED]
    assert _cell(model, 0, COL_VALUE) == "Conn_01x04"
    assert _cell(model, 0, COL_PACKAGE) == "header 1x04, 2.54 mm, vertical"
    assert _cell(model, 0, COL_DIELECTRIC) == THT_CLASS
    assert _cell(model, 1, COL_STATUS) == STATUS_NO_PART
    assert "Not searchable" in model.data(model.index(2, COL_VALUE), Qt.ToolTipRole)
    assert model.summary()[STATUS_NO_PART] == 1
    # No part rows are not tinted as work to do.
    assert model.data(model.index(1, 0), Qt.BackgroundRole) is None


# ---- dialog -----------------------------------------------------------------

def test_dialog_requests_and_shows_result():
    _app()
    g = _group()
    dlg = ThtAssignDialog(g, 5, series="")
    assert dlg.needed == 10
    sent = []
    dlg.search_requested.connect(lambda req, digikey: sent.append((req, digikey)))
    assert dlg.request_search(False)
    assert dlg.request_search(True)
    assert sent == [(None, False), (None, True)]
    dlg.show_result(ThtSearchResult(requirement=g.requirement, candidates=[_cand()]),
                    dlg.request_id)
    assert dlg.chosen().mpn == "PREC004SAAN-RC"
    # An older request's result is ignored.
    dlg.show_result(ThtSearchResult(requirement=g.requirement, candidates=[]), 1)
    assert dlg.chosen() is not None
    dlg.drop_candidate(dlg.chosen())
    assert dlg.chosen() is None


def test_dialog_cannot_accept_without_digikey_pn():
    _app()
    g = _group()
    dlg = ThtAssignDialog(g, 1)
    bare = ThtCandidate(product=DigiKeyProduct(mpn="X", manufacturer="Y"),
                        signature=g.requirement.signature, value="")
    dlg.show_result(ThtSearchResult(requirement=g.requirement, candidates=[bare]), None)
    assert dlg.chosen() is None
    assert "DigiKeyPN" in dlg.accept_problem(bare)


def test_dialog_not_searchable():
    _app()
    dlg = ThtAssignDialog(_group(DIN, "DIN-5", refs=("J1",), pads=7), 1)
    assert not dlg.request_search(False)


# ---- window -----------------------------------------------------------------

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


class _FakeThtWorker(QObject):
    found = Signal(object)
    failed = Signal(str)
    inits: list = []

    def __init__(self, *, group, run, needed, db_path, use_stored=True):
        super().__init__()
        self.group = group
        type(self).inits.append(dict(group=group, needed=needed, use_stored=use_stored, run=run))

    def run(self):
        n = self.group.requirement.signature.positions
        cand = ThtCandidate(
            product=DigiKeyProduct(mpn=f"PREC0{n:02d}SAAN-RC",
                                   manufacturer="Sullins Connector Solutions",
                                   quantity_available=5000, unit_price=0.1, product_status="Active",
                                   digikey_pns=[f"S10{n}-ND"],
                                   parameters={"Contact Finish - Mating": "Tin",
                                               "Connector Type": "Header, Breakaway"}),
            signature=self.group.requirement.signature, value=self.group.requirement.value_text,
            source=CURRENT)
        self.found.emit(ThtSearchResult(requirement=self.group.requirement, candidates=[cand]))
        QThread.currentThread().quit()


def _wait_idle(win, timeout=10.0):
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + timeout
    while (win._busy() or win._search_running) and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()


def _accepting_exec(dialog):
    from PySide6.QtWidgets import QApplication, QDialog
    deadline = time.monotonic() + 10
    while dialog.chosen() is None and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    return QDialog.Accepted


def _tht_row(model, ref):
    return next(r for r in range(model.rowCount())
                if is_tht(model.group_at(r)) and ref in model.group_at(r).references)


def test_window_assign_header_end_to_end(window, monkeypatch):
    import kicadtools.gui.main_window as mw

    win, board = window
    monkeypatch.setattr(mw, "ThtCandidateWorker", _FakeThtWorker)
    monkeypatch.setattr(mw.ThtAssignDialog, "exec", _accepting_exec)
    _FakeThtWorker.inits.clear()
    model = win.parts_model
    r = _tht_row(model, "J6")
    assert model.status(r) == STATUS_UNASSIGNED
    assert "DigiKey:" in win._digikey_label.text()
    win._parts_double_clicked(win.parts_proxy.mapFromSource(model.index(r, 0)))
    _wait_idle(win)
    (init,) = _FakeThtWorker.inits
    assert init["use_stored"] is True and init["run"] is win._header_run
    assert model.status(r) == STATUS_QUEUED
    assert "Save to write it" in win.statusBar().currentMessage()
    # Stored in the database under the footprint signature, usage recorded.
    group = model.group_at(r)
    (hit,) = win._parts_db.find_tht(*db_key(group.requirement.signature, group.requirement.value_text))
    assert hit.mpn == "PREC002SAAN-RC" and hit.usage_count == 1
    # The first header accepted sets the run's series.
    assert win._header_run.series["header"].mpn == "PREC002SAAN-RC"
    # Save writes MPN, Manufacturer and DigiKeyPN; Value and Footprint stay.
    assert win._write_in_place(win._merged_edits())
    j6 = next(rec for rec in iter_bom_part_records(board) if rec.reference == "J6")
    assert (j6.props["MPN"], j6.props["Manufacturer"], j6.props["DigiKeyPN"]) == (
        "PREC002SAAN-RC", "Sullins Connector Solutions", "S102-ND")
    orig = next(rec for rec in iter_bom_part_records(ECG) if rec.reference == "J6")
    assert (j6.props["Value"], j6.props["Footprint"]) == (orig.props["Value"], orig.props["Footprint"])
    assert win.parts_model.status(_tht_row(win.parts_model, "J6")) == STATUS_ASSIGNED


def test_window_header_makers_setting(window):
    win, _board = window
    from kicadtools.headersearch import HEADER_MAKERS
    assert win._header_makers() == HEADER_MAKERS
    win._parts_db.set_setting("hdr.manufacturers", "Samtec, Harwin")
    assert win._header_makers() == ("Samtec", "Harwin")
