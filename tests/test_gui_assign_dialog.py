"""Tests for the assign dialog (gui/assign_dialog.py): the pure helpers, and the
dialog's request numbering and result handling under an offscreen
QApplication (no network: results are delivered by hand)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from kicadtools.partsearch import Candidate, SearchResult  # noqa: E402
from kicadtools.gui.assign_dialog import (  # noqa: E402
    CANDIDATE_COLUMNS,
    candidate_cells,
    field_label,
    field_text,
    parse_field_values,
    result_summary,
    stock_shortfall,
    symbol_count,
)
from kicadtools.partsdb import PartParams  # noqa: E402
from kicadtools.parttypes import CAPACITOR  # noqa: E402
from kicadtools.requirement import (  # noqa: E402
    ANY,
    DEFAULT,
    EDITED,
    SCHEMATIC,
    UNSET,
    PartRequirement,
    Member,
    RequirementGroup,
    Sourced,
)


def _req(*, footprint="", package=("0603", DEFAULT), volts=(None, UNSET),
         diel=(("X7R",), DEFAULT), tol=(10.0, DEFAULT)) -> PartRequirement:
    return PartRequirement(kind="capacitor",
        value_text="100nF",
        nominal=100e-9,
        package=Sourced(*package),
        rating_min=Sourced(*volts),
        dielectrics=Sourced(*diel),
        tolerance_max_pct=Sourced(*tol),
        footprint=footprint,
    )


def _group(req=None, refs=("C1", "C2", "C10")) -> RequirementGroup:
    members = [Member(r, Path("b.kicad_sch"), f"u-{r}", {}, None) for r in refs]
    return RequirementGroup(req or _req(), members)


def _cand(**kw) -> Candidate:
    base = dict(kind="capacitor",
        mpn="CL10B104KB8NNNC", manufacturer="Samsung", package="0603",
        params=PartParams(100e-9, 50.0, "X7R", 10.0),
        lcsc="C1591", jlc_library="basic", jlc_preferred=True,
        jlc_stock=100_000, jlc_price=0.0021,
        digikey_pn="1276-1000-1-ND", digikey_stock=50_000, source="search",
    )
    base.update(kw)
    return Candidate(**base)


# ---- field_label / field_text ----------------------------------------------

def test_field_label_per_source():
    assert field_label(Sourced("0603", SCHEMATIC), 3) == "from the schematic"
    assert field_label(Sourced(16.0, DEFAULT), 3) == "default — written to 3 symbols"
    assert field_label(Sourced(16.0, EDITED), 1) == "edited — written to 1 symbol"
    assert field_label(Sourced(None, UNSET), 2) == "required — written to 2 symbols"


def test_field_text():
    assert field_text(Sourced(16.0, DEFAULT), "rating", CAPACITOR) == "16V"
    assert field_text(Sourced(None, UNSET), "rating", CAPACITOR) == ""
    assert field_text(Sourced(("X7R", "X5R"), DEFAULT), "dielectric", CAPACITOR) == "X7R, X5R"
    assert field_text(Sourced(5.0, DEFAULT), "tolerance", CAPACITOR) == "5%"
    assert field_text(Sourced("0805", SCHEMATIC), "package", CAPACITOR) == "0805"


# ---- parse_field_values ----------------------------------------------------

def test_parse_requires_voltage_when_unset():
    out = parse_field_values("0603", "", "X7R", "10%", original=_req())
    assert isinstance(out, str) and "Voltage:Min" in out


def test_parse_only_changed_fields():
    out = parse_field_values("0603", "16", "X7R", "10%", original=_req())
    assert out == {"rating_min": 16.0}
    out = parse_field_values("0805", "16V", "x7r, X5R", "20", original=_req())
    assert out == {"package": "0805", "rating_min": 16.0,
                   "dielectrics": ("X7R", "X5R"), "tolerance_max_pct": 20.0}


def test_parse_unchanged_default_voltage_kept():
    orig = _req(volts=(25.0, DEFAULT))
    assert parse_field_values("0603", "25V", "X7R", "10%", original=orig) == {}


def test_parse_blank_or_any_clears_a_field():
    orig = _req(volts=(25.0, DEFAULT))
    assert parse_field_values("0603", "", "X7R", "10%", original=orig) == {
        "clear": ("rating_min",)}
    assert parse_field_values("0603", "25V", "any", "", original=orig) == {
        "clear": ("dielectrics", "tolerance_max_pct")}
    cleared = orig.edited(clear=("rating_min",))
    assert cleared.rating_min == Sourced(None, ANY) and cleared.searchable
    # already cleared: blank changes nothing
    assert parse_field_values("0603", "", "X7R", "10%", original=cleared) == {}
    # no value to begin with: blank is an error, "any" clears
    unset = _req(volts=(None, UNSET))
    assert "Voltage:Min" in parse_field_values("0603", "", "X7R", "10%", original=unset)
    assert parse_field_values("0603", "Any", "X7R", "10%", original=unset) == {
        "clear": ("rating_min",)}


def test_parse_errors():
    assert "Package" in parse_field_values("big", "16", "", "", original=_req())
    assert "Voltage:Min" in parse_field_values("", "lots", "", "", original=_req())
    assert "Tolerance" in parse_field_values("", "16", "", "tight", original=_req())


def test_parse_ignores_package_with_footprint():
    orig = _req(footprint="Capacitor_SMD:C_0603_1608Metric", package=("0603", SCHEMATIC))
    out = parse_field_values("0805", "16", "X7R", "10%", original=orig)
    assert out == {"rating_min": 16.0}
    orig.edited(**out)  # does not raise: package is not passed


# ---- candidate cells -------------------------------------------------------

def test_candidate_cells():
    cells = candidate_cells(_cand())
    assert len(cells) == len(CANDIDATE_COLUMNS)
    row = dict(zip(CANDIDATE_COLUMNS, cells))
    assert row["MPN"] == "CL10B104KB8NNNC"
    assert row["Source"] == "search"
    assert row["LCSC"] == "C1591"
    assert row["JLC library"] == "Basic (preferred)"
    assert row["JLC stock"] == "100,000"
    assert row["DigiKey PN"] == "1276-1000-1-ND"
    assert row["DigiKey stock"] == "50,000"
    assert row["Unit price"] == "$0.0021"
    assert row["Value"] == "100nF"
    assert row["Rating"] == "50V"
    assert row["Class"] == "X7R"
    assert row["Tolerance"] == "10%"


def test_candidate_cells_unknowns():
    cand = _cand(source="database", usage_count=3, jlc_stock=None, digikey_pn=None,
                 digikey_stock=None, jlc_price=float("inf"), jlc_library="extended",
                 jlc_preferred=False,
                 params=PartParams(100e-9, 50.0, "X7R", None))
    row = dict(zip(CANDIDATE_COLUMNS, candidate_cells(cand)))
    assert row["Source"] == "database (3 uses)"
    assert row["JLC library"] == "Extended"
    assert row["JLC stock"] == row["DigiKey PN"] == row["DigiKey stock"] == ""
    assert row["Unit price"] == row["Tolerance"] == ""


def test_stock_shortfall():
    assert stock_shortfall(_cand(), 1000) == ""
    assert "JLCPCB has 5" in stock_shortfall(_cand(jlc_stock=5), 10)
    assert "DigiKey has 0" in stock_shortfall(_cand(digikey_stock=0), 10)
    assert stock_shortfall(_cand(jlc_stock=None, digikey_stock=None), 10) == ""


def test_symbol_count_dedupes_shared_uuid():
    g = _group()
    g.members.append(Member("C11", Path("b.kicad_sch"), "u-C1", {}, None))
    g.members.append(Member("C12", None, None, {}, None))
    assert symbol_count(g) == 3


def test_result_summary():
    r = SearchResult([_cand(source="database"), _cand(mpn="X")], examined=40,
                     rejected=[("C1 X", "voltage")], dropped_by_limit=2,
                     problem="something went wrong")
    text = result_summary(r)
    assert text.startswith("2 candidates (1 from the database, 1 from supplier search)")
    assert "40 search hits checked" in text
    assert "rejected 1" in text
    assert "Something went wrong." in text


# ---- dialog (offscreen) ----------------------------------------------------

@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_dialog_waits_for_voltage_then_requests(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(), 10, None)
    assert dialog.needed == 30
    requests = []
    dialog.search_requested.connect(lambda req, sup: requests.append((req, sup)))
    assert dialog.request_search() is False
    assert requests == []
    assert dialog.current_requirement() is None

    dialog._fields["rating"].setText("16V")
    assert dialog.request_search(True) is True
    req, sup = requests[-1]
    assert sup is True
    assert req.rating_min == Sourced(16.0, EDITED)
    assert req.dielectrics.source == DEFAULT
    assert dialog.request_id == 1
    dialog._refresh_notes()
    assert dialog._notes["rating"].text() == "edited — written to 3 symbols"
    assert dialog._notes["dielectric"].text() == "default — written to 3 symbols"


def test_dialog_ignores_stale_results(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(volts=(16.0, DEFAULT)), 1, None)
    assert dialog.request_search() and dialog.request_search()
    assert dialog.request_id == 2
    dialog.show_result(SearchResult([_cand(mpn="OLD")]), 1)
    assert dialog.chosen() is None
    dialog.show_result(SearchResult([_cand(mpn="NEW"), _cand(mpn="TWO")]), 2)
    assert dialog.chosen().mpn == "NEW"          # first row selected
    assert dialog._accept_btn.isEnabled()
    dialog.show_result(SearchResult([]), 2)
    assert dialog.chosen() is None
    assert not dialog._accept_btn.isEnabled()


def test_dialog_package_disabled_with_footprint(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    req = _req(footprint="Capacitor_SMD:C_0603_1608Metric", package=("0603", SCHEMATIC))
    dialog = AssignDialog(_group(req), req, 1, None)
    assert not dialog._fields["package"].isEnabled()
    assert dialog._notes["package"].text() == "from the schematic"


# ---- review additions --------------------------------------------------------

def test_field_label_unreadable_stated_value():
    assert field_label(Sourced(None, SCHEMATIC), 2) == "from the schematic, not readable"


def test_parse_never_edits_stated_fields():
    orig = _req(package=("0805", SCHEMATIC), volts=(50.0, SCHEMATIC),
                diel=(("C0G",), SCHEMATIC), tol=(1.0, SCHEMATIC))
    assert parse_field_values("0603", "25V", "X7R", "10%", original=orig) == {}
    # A stated value that does not parse stays as stated, even with text typed.
    orig = _req(volts=(None, SCHEMATIC))
    assert parse_field_values("0603", "16V", "X7R", "10%", original=orig) == {}
    assert not orig.edited().searchable


def test_parse_dielectric_errors():
    out = parse_field_values("0603", "16", " , / ", "10%", original=_req())
    assert isinstance(out, str) and "Dielectric" in out
    out = parse_field_values("0603", "16", "", "10%", original=_req(diel=((), UNSET)))
    assert isinstance(out, str) and "dielectric" in out
    out = parse_field_values("0603", "16", "cog / np0", "", original=_req())
    assert out["dielectrics"] == ("C0G", "NP0")


def test_parse_float_tolerance_compared_with_tolerance():
    orig = _req(tol=(0.1 + 0.2, DEFAULT))       # 0.30000000000000004
    assert parse_field_values("0603", "16", "X7R", "0.3%", original=orig) == {
        "rating_min": 16.0
    }


def test_result_summary_no_double_period():
    text = result_summary(SearchResult([], problem="DigiKey credentials are not set."))
    assert text.endswith("DigiKey credentials are not set.")
    assert ".." not in text
    assert "not checked" in result_summary(SearchResult([], dropped_by_limit=3))


def test_dialog_stated_fields_read_only(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    req = _req(volts=(50.0, SCHEMATIC), diel=(("C0G",), SCHEMATIC))
    dialog = AssignDialog(_group(req), req, 1, None)
    assert dialog._fields["rating"].isReadOnly()
    assert dialog._fields["dielectric"].isReadOnly()
    assert not dialog._fields["tolerance"].isReadOnly()
    assert not dialog._fields["package"].isReadOnly()
    # Text forced into a read-only field is ignored.
    dialog._fields["rating"].setText("6.3V")
    assert dialog.current_requirement() == req


def test_dialog_accept_blocked_after_field_edit(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(volts=(16.0, DEFAULT)), 1, None)
    assert dialog.request_search()
    dialog.show_result(SearchResult([_cand()]), dialog.request_id)
    assert dialog.chosen() is not None and dialog._accept_btn.isEnabled()

    # An edit whose search has not finished: the shown part was checked for
    # 16 V, not 100 V, so it cannot be accepted.
    dialog._fields["rating"].setText("100V")
    dialog._field_edited("100V")
    assert dialog.chosen() is None
    assert not dialog._accept_btn.isEnabled()
    dialog.accept()
    assert dialog.result() != dialog.DialogCode.Accepted

    # A field that does not parse also blocks Accept.
    dialog._fields["rating"].setText("lots")
    dialog._field_edited("lots")
    assert dialog.chosen() is None

    # Back to the searched requirement: Accept is available again.
    dialog._fields["rating"].setText("16V")
    dialog._field_edited("16V")
    assert dialog.chosen() is not None
    dialog.done(0)
    assert not dialog._debounce.isActive()


def test_dialog_new_result_after_edit_enables_accept(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(volts=(16.0, DEFAULT)), 1, None)
    dialog.request_search()
    dialog.show_result(SearchResult([_cand()]), dialog.request_id)
    dialog._fields["rating"].setText("25V")
    dialog._field_edited("25V")
    assert dialog.request_search()
    dialog.show_result(SearchResult([_cand(mpn="NEW")]), dialog.request_id)
    assert dialog.chosen().mpn == "NEW"
    assert dialog.current_requirement().rating_min == Sourced(25.0, EDITED)


def test_dialog_candidate_without_plan_cannot_be_accepted(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(volts=(16.0, DEFAULT)), 1, None)
    dialog.request_search()
    dialog.show_result(SearchResult([_cand(digikey_pn=None)]), dialog.request_id)
    assert dialog.chosen() is None
    assert "DigiKeyPN" in dialog._accept_btn.toolTip()

    # No footprint on the symbols and no FOOTPRINTS entry for the package.
    req = _req(volts=(16.0, DEFAULT), package=("2220", DEFAULT))
    dialog = AssignDialog(_group(req), req, 1, None)
    dialog.request_search()
    dialog.show_result(SearchResult([_cand(package="2220")]), dialog.request_id)
    assert dialog.chosen() is None
    assert "footprint" in dialog._accept_btn.toolTip()


def test_dialog_drop_candidate(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(volts=(16.0, DEFAULT)), 1, None)
    dialog.request_search()
    first, second = _cand(mpn="A"), _cand(mpn="B")
    dialog.show_result(SearchResult([first, second]), dialog.request_id)
    dialog.drop_candidate(first)
    assert dialog._table.rowCount() == 1
    assert dialog._candidates == [second]
    assert dialog._table.item(0, 0).text() == "B"


def test_dialog_debounce_reruns_last_search_kind(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(volts=(16.0, DEFAULT)), 1, None)
    requests = []
    dialog.search_requested.connect(lambda req, sup: requests.append(sup))
    dialog.request_search(True)
    dialog._debounce.setInterval(0)
    dialog._fields["tolerance"].setText("5%")
    dialog._field_edited("5%")
    deadline = time.monotonic() + 5
    while len(requests) < 2 and time.monotonic() < deadline:
        app.processEvents()
    assert requests == [True, True]


# ---- revision 2: current part, adopted requirement, conflicts -------------------

from dataclasses import replace as _replace  # noqa: E402

from kicadtools.partsearch import CURRENT, CurrentOption, ExistingLookup  # noqa: E402
from kicadtools.evidence import ExistingPart  # noqa: E402
from kicadtools.gui.assign_dialog import (  # noqa: E402
    conflict_text,
    current_part_text,
    value_header,
)
from kicadtools.requirement import DESCRIPTION, PART  # noqa: E402

_DK = {"DigiKeyPN": "1276-1000-1-ND"}


def _group_with_part(req=None, props=_DK) -> RequirementGroup:
    members = [Member(r, Path("b.kicad_sch"), f"u-{r}", dict(props), None)
               for r in ("C1", "C2")]
    return RequirementGroup(req or _req(), members)


def _merged(**kw) -> PartRequirement:
    """The requirement assess returns once the current part is known."""
    base = dict(rating_min=Sourced(50.0, PART),
                dielectrics=Sourced(("X7R",), PART),
                tolerance_max_pct=Sourced(10.0, PART))
    base.update(kw)
    return _replace(_req(), **base)


def test_field_label_part_and_description():
    assert field_label(Sourced(50.0, PART), 2) == "from current part — written to 2 symbols"
    assert field_label(Sourced("0805", DESCRIPTION), 1) == \
        "from description — written to 1 symbol"


def test_candidate_cells_current_source():
    assert candidate_cells(_cand(source=CURRENT))[2] == "current part"


def test_result_summary_counts_current_part():
    r = SearchResult([_cand(source=CURRENT), _cand(source="database"), _cand(mpn="X")])
    assert result_summary(r).startswith(
        "3 candidates (the current part, 1 from the database, 1 from supplier search)")


def test_current_part_text():
    assert current_part_text(ExistingPart()) == "none on the schematic"
    assert current_part_text(ExistingPart(digikey_pn="1276-1000-1-ND")) == \
        "DigiKeyPN 1276-1000-1-ND (looking up…)"
    lookup = ExistingLookup(notes=["1276-1000-1-ND at DigiKey: CL10B104KB8NNNC",
                                   "LCSC C1591"])
    assert current_part_text(ExistingPart(digikey_pn="x"), lookup) == \
        "1276-1000-1-ND at DigiKey: CL10B104KB8NNNC\nLCSC C1591"
    assert current_part_text(ExistingPart(), ExistingLookup()) == "none on the schematic"


def test_conflict_text_and_value_header():
    assert conflict_text(_req()) == ""
    req = _replace(_req(), conflicts=("Value says 10nF; Description says 10uF",))
    assert "Value says 10nF; Description says 10uF" in conflict_text(req)
    warned = _replace(_req(), warnings=("Description says 0603; Footprint says 0402 "
                                        "(Description not used)",))
    assert conflict_text(warned) == warned.warnings[0]
    assert "Conflict" not in conflict_text(warned)
    assert value_header(_req()) == "100nF"
    assert value_header(_replace(_req(), value_text="", nominal=1e-6,
                                 nominal_source=DESCRIPTION)) == "1uF (from description)"


def test_dialog_current_part_row(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group(), _req(), 1, None)
    assert dialog._current_label.text() == "none on the schematic"
    dialog = AssignDialog(_group_with_part(), _req(), 1, None)
    assert "1276-1000-1-ND" in dialog._current_label.text()
    lookup = ExistingLookup(notes=["found CL10B104KB8NNNC"])
    assert dialog.request_search()
    dialog.show_result(SearchResult([], existing=lookup), dialog.request_id)
    assert dialog._current_label.text() == "found CL10B104KB8NNNC"
    assert dialog.existing_lookup is lookup


def test_dialog_first_request_sends_none(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    # Not searchable as it stands (no voltage), but the existing part may
    # supply one: the first request goes out with no requirement.
    dialog = AssignDialog(_group_with_part(), _req(), 1, None)
    requests = []
    dialog.search_requested.connect(lambda req, sup: requests.append((req, sup)))
    assert dialog.request_search(True)
    assert requests == [(None, True)]
    # After a result, each request sends the dialog's requirement.
    dialog.show_result(SearchResult([], requirement=_merged()), dialog.request_id)
    assert dialog.request_search()
    assert requests[-1] == (_merged(), False)


def test_dialog_adopts_merged_requirement(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group_with_part(), _req(), 1, None)
    assert dialog.request_search()
    current = _cand(source=CURRENT, mpn="CURRENT")
    other = _cand(mpn="OTHER")
    lookup = ExistingLookup(notes=["n"])
    dialog.show_result(SearchResult([other, current], requirement=_merged(),
                                    existing=lookup), dialog.request_id)
    assert dialog.original == _merged()
    assert dialog._fields["rating"].text() == "50V"
    assert dialog._notes["rating"].text() == "from current part — written to 2 symbols"
    assert dialog._notes["package"].text() == "default — written to 2 symbols"
    # The current part is selected and can be accepted against the merged requirement.
    assert dialog.chosen().mpn == "CURRENT"
    assert dialog.current_requirement() == _merged()
    assert dialog.requirement_for(dialog.chosen()) == _merged()
    assert dialog._table.item(1, 2).text() == "current part"


def test_dialog_keeps_user_edit_over_result_requirement(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    dialog = AssignDialog(_group_with_part(), _req(), 1, None)
    assert dialog.request_search()
    dialog._fields["rating"].setText("16V")
    dialog._field_edited("16V")
    dialog._debounce.stop()
    requests = []
    dialog.search_requested.connect(lambda req, sup: requests.append(req))
    dialog.show_result(SearchResult([_cand()], requirement=_merged()), dialog.request_id)
    # The merged requirement is the new base; the typed voltage is kept over it.
    assert dialog.original == _merged()
    assert dialog._fields["rating"].text() == "16V"
    assert dialog.current_requirement() == _merged().edited(rating_min=16.0)
    assert dialog._notes["dielectric"].text() == "from current part — written to 2 symbols"
    # The shown candidates belong to the merged requirement, not the edit, so
    # the edited one is requested at once.
    assert dialog.chosen() is None
    assert requests[-1] == _merged().edited(rating_min=16.0)


def test_dialog_shows_conflicts_and_cannot_search(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    line = "Value says 10nF; current part 1276-3388-1-ND (DigiKey) says 10uF"
    dialog = AssignDialog(_group_with_part(), _req(volts=(25.0, DEFAULT)), 1, None)
    assert dialog.request_search()
    conflicted = _merged(conflicts=(line,))
    dialog.show_result(SearchResult([], problem=line, requirement=conflicted,
                                    existing=ExistingLookup()),
                       dialog.request_id)
    assert dialog.original == conflicted
    assert not dialog._conflicts.isHidden()
    assert line in dialog._conflicts.text()
    assert "#c07000" in dialog._conflicts.styleSheet()
    assert not dialog._search_btn.isEnabled()
    assert dialog.request_search() is False
    assert line in dialog._status.text()


def test_dialog_opened_on_conflict_row_shows_it(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    line = "Value says 10nF; Description says 10uF"
    req = _replace(_req(volts=(25.0, DEFAULT)), conflicts=(line,))
    dialog = AssignDialog(_group(req), req, 1, None)
    assert line in dialog._conflicts.text()
    assert not dialog._search_btn.isEnabled()
    assert dialog.request_search() is False


# ---- review of the Cockpit stage ---------------------------------------------

def test_parse_package_accepts_three_digit_size():
    assert parse_field_values("805", "16", "X7R", "10%", original=_req()) == {
        "package": "0805", "rating_min": 16.0}


def test_dialog_merges_lookup_from_edited_request(app):
    """The user edits before the first result: the request sends the edited
    requirement, so assess returns it as is. With *defaults* the dialog rebuilds
    the base from the lookup's evidence and conflicts and keeps the edit."""
    from kicadtools.evidence import PartEvidence
    from kicadtools.intent import CapacitorIntent
    from kicadtools.requirement import part_defaults, part_requirement

    defaults = part_defaults({"cap.voltage_min": "16V"})
    intent = CapacitorIntent(reference="C1", dnp=False, in_bom=True, value="100nF")
    base = part_requirement(intent, "", defaults)
    members = [Member("C1", Path("b.kicad_sch"), "u-C1", dict(_DK), intent)]
    group = RequirementGroup(base, members)
    dialog = AssignDialog_(group, base, defaults)
    requests = []
    dialog.search_requested.connect(lambda req, sup: requests.append(req))
    assert dialog.request_search()                       # None: first request
    dialog._fields["tolerance"].setText("5%")
    dialog._field_edited("5%")
    assert dialog.request_search()                       # edited, not merged
    edited = requests[-1]
    assert edited.rating_min == Sourced(16.0, DEFAULT)

    evidence = PartEvidence(label="current part 1276-1000-1-ND (DigiKey)",
                            nominal=100e-9, rating=50.0,
                            dielectrics=("X7R",), tolerance_pct=10.0, package="0603")
    lookup = ExistingLookup(options=[CurrentOption(("DigiKeyPN",), evidence)],
                            notes=["found"])
    # The None request's result is stale; its lookup is still used.
    dialog.show_result(SearchResult([_cand(mpn="OLD")], existing=lookup), 1)
    assert dialog.existing_lookup is lookup
    assert dialog.original.rating_min == Sourced(50.0, PART)
    assert dialog._fields["tolerance"].text() == "5%"
    assert dialog.current_requirement().tolerance_max_pct == Sourced(5.0, EDITED)
    assert dialog.current_requirement().rating_min == Sourced(50.0, PART)
    # The merged, edited requirement is requested again.
    assert requests[-1] == dialog.current_requirement()
    assert dialog.request_id == 3
    # The result of request 2 (edited, unmerged) is shown but cannot be accepted.
    dialog.show_result(SearchResult([_cand()], requirement=edited), 2)
    assert dialog._candidates == []          # stale: ignored
    dialog.show_result(SearchResult([_cand()], requirement=requests[-1]), 3)
    assert dialog.chosen() is not None


def test_dialog_lookup_warning_from_edited_request_does_not_block(app):
    from kicadtools.evidence import PartEvidence
    from kicadtools.intent import CapacitorIntent
    from kicadtools.requirement import part_defaults, part_requirement

    defaults = part_defaults({"cap.voltage_min": "16V"})
    intent = CapacitorIntent(reference="C1", dnp=False, in_bom=True, value="100nF")
    base = part_requirement(intent, "", defaults)
    group = RequirementGroup(base, [Member("C1", Path("b.kicad_sch"), "u-C1",
                                           dict(_DK), intent)])
    dialog = AssignDialog_(group, base, defaults)
    dialog._fields["rating"].setText("25V")
    dialog._field_edited("25V")
    assert dialog.request_search()                    # edited from the start
    # the existing part is 10 uF; the Value says 100nF: not used, a warning
    stale = PartEvidence(label="current part Y (DigiKey)", nominal=10e-6)
    lookup = ExistingLookup(options=[CurrentOption(("DigiKeyPN",), stale)])
    sent = dialog.current_requirement()
    dialog.show_result(SearchResult([_cand()], requirement=sent, existing=lookup),
                       dialog.request_id)
    line = ("Value says 100nF; current part Y (DigiKey) says 10uF "
            "(current part Y (DigiKey) not used)")
    assert dialog.original.conflicts == ()
    assert dialog.original.warnings == (line,)
    assert line in dialog._conflicts.text()
    assert dialog._search_btn.isEnabled()
    assert dialog._fields["rating"].text() == "25V"


def test_dialog_reopened_with_recorded_lookup(app):
    lookup = ExistingLookup(notes=["recorded"])
    from kicadtools.gui.assign_dialog import AssignDialog
    dialog = AssignDialog(_group_with_part(), _merged(), 1, None, existing=lookup)
    assert dialog.existing_lookup is lookup
    assert dialog._current_label.text() == "recorded"
    requests = []
    dialog.search_requested.connect(lambda req, sup: requests.append(req))
    assert dialog.request_search()
    assert requests == [None]
    assert dialog._status.text() == "Searching the parts database…"


def test_dialog_reopened_queued_row_keeps_its_edit(app):
    """A queued row's requirement holds an edit; the first request sends None
    and its result (the group's base) does not discard the edit."""
    from kicadtools.gui.assign_dialog import AssignDialog
    queued = _merged().edited(rating_min=100.0)
    dialog = AssignDialog(_group_with_part(), queued, 1, None)
    requests = []
    dialog.search_requested.connect(lambda req, sup: requests.append(req))
    assert dialog.request_search()
    assert requests == [None]
    dialog.show_result(SearchResult([_cand()], requirement=_merged()), dialog.request_id)
    assert dialog.original == _merged()
    assert dialog.current_requirement() == queued
    assert requests[-1] == queued
    assert dialog.chosen() is None


def AssignDialog_(group, req, defaults):
    from kicadtools.gui.assign_dialog import AssignDialog
    return AssignDialog(group, req, 1, None, defaults=defaults)


# ---- values popup ---------------------------------------------------------------

def test_facet_rows_cleared_fields_or_all_when_nothing_found():
    from kicadtools.gui.assign_dialog import facet_rows

    facets = {"rating": [("16V", 3)], "dielectric": [("X5R", 5), ("X7R", 1)],
              "tolerance": [("10%", 6)]}
    req = _req(volts=(16.0, DEFAULT))
    assert facet_rows(SearchResult([], facets=facets), req) == [
        ("rating", [("16V", 3)]), ("dielectric", [("X5R", 5), ("X7R", 1)]),
        ("tolerance", [("10%", 6)])]
    assert facet_rows(SearchResult([_cand()], facets=facets), req) == []
    cleared = req.edited(clear=("dielectrics",))
    assert facet_rows(SearchResult([_cand()], facets=facets), cleared) == [
        ("dielectric", [("X5R", 5), ("X7R", 1)])]
    assert facet_rows(SearchResult([]), cleared) == []


def test_dialog_values_popup_fills_the_field(app):
    from kicadtools.gui.assign_dialog import AssignDialog

    req = _req(volts=(16.0, DEFAULT)).edited(clear=("dielectrics",))
    dialog = AssignDialog(_group(req), req, 1, None)
    assert not dialog._values_btn.isEnabled()
    facets = {"rating": [], "dielectric": [("X5R", 5)], "tolerance": []}
    dialog.show_result(SearchResult([], requirement=req, facets=facets, pool_size=9))
    assert dialog._values_btn.isEnabled()
    popup = dialog._facet_popup
    assert popup is not None and popup.isVisible()
    top = popup._tree.topLevelItem(0)
    assert (top.text(0), top.child(0).text(0), top.child(0).text(1)) == ("Dielectric", "X5R", "5")
    popup._double_clicked(top.child(0), 0)
    assert dialog._fields["dielectric"].text() == "X5R"
    assert dialog.current_requirement().dielectrics == Sourced(("X5R",), EDITED)
    popup.close()
