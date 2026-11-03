"""Resistor assignment: Value and Description parsing, the requirement, the
parts database, the candidate search, the write plan and the Cockpit helpers.
The shared code is tested with capacitors in the other test files; these
tests cover what the resistor part type changes."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pytest

from kicadtools.assign import assignment_plan
from kicadtools.evidence import DescriptionEvidence, PartEvidence
from kicadtools.intent import ElectricalIntent
from kicadtools.partsdb import PartChoice, PartParams, PartsDb
from kicadtools.partsearch import (
    SEARCH,
    find_candidates,
    requirement_for,
)
from kicadtools.parttypes import CAPACITOR, RESISTOR, type_of_intent
from kicadtools.requirement import (
    ANY,
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    RELAXED,
    SCHEMATIC,
    RequirementGroup,
    Member,
    group_parts,
    part_defaults,
    part_requirement,
)
from kicadtools.resolve import PartRecord
from kicadtools.suppliers import DigiKeyProduct, JlcPart
from kicadtools.units import parse_res_description, parse_res_value

DEFAULTS = part_defaults({})


# --------------------------------------------------------------------------- #
# Value and Description
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, ohms, package", [
    ("10K", 10e3, None),
    ("1k", 1e3, None),
    ("6.8K", 6.8e3, None),
    ("4k7", 4.7e3, None),
    ("4R7", 4.7, None),
    ("1", 1.0, None),
    ("330", 330.0, None),
    ("10M", 10e6, None),
    ("10kΩ", 10e3, None),
    ("R_10K", 10e3, None),
    ("R_49p9K", 49.9e3, None),
    ("R_10K_805", 10e3, "0805"),
    ("R_0805", None, "0805"),
    ("tbd", None, None),
])
def test_parse_res_value(text, ohms, package):
    got_ohms, got_pkg = parse_res_value(text)
    if ohms is None:
        assert got_ohms is None
    else:
        assert got_ohms == pytest.approx(ohms, rel=1e-9)
    assert got_pkg == package


@pytest.mark.parametrize("text, ohms, package, tol, watts", [
    ("10K, 603, 1%", 10e3, "0603", 1.0, None),
    ("49.9K, 603, 1%", 49.9e3, "0603", 1.0, None),
    ("330, 0603, 1%", 330.0, "0603", 1.0, None),
    ("10 kOhms 1/4W ±5% 0805", 10e3, "0805", 5.0, 0.25),
    ("100mW 1M", 1e6, None, None, 0.1),
    ("Resistor, small US symbol", None, None, None, None),
])
def test_parse_res_description(text, ohms, package, tol, watts):
    ev = parse_res_description(text)
    assert ev.nominal == (pytest.approx(ohms) if ohms is not None else None)
    assert ev.package == package
    assert ev.tolerance_pct == tol
    assert ev.rating == (pytest.approx(watts) if watts is not None else None)
    assert ev.dielectrics == ()


# --------------------------------------------------------------------------- #
# Requirement
# --------------------------------------------------------------------------- #

def _intent(value="10K", **props):
    data = {"Reference": "R1", "dnp": False, "in_bom": True,
            "Intent:Type": "resistor", "Value": value, **props}
    return ElectricalIntent.from_dict(data)


def test_part_type_of_resistor_intent():
    assert type_of_intent(_intent()) is RESISTOR
    assert RESISTOR.key("package") == "res.package"


def test_resistor_defaults():
    req = part_requirement(_intent(), "", DEFAULTS)
    assert req.kind == "resistor"
    assert req.nominal == pytest.approx(10e3)
    assert (req.package.value, req.package.source) == ("0603", DEFAULT)
    # No power floor stated or remembered: no limit.
    assert (req.rating_min.value, req.rating_min.source) == (None, ANY)
    assert (req.tolerance_max_pct.value, req.tolerance_max_pct.source) == (1.0, DEFAULT)
    assert req.dielectrics.value == ()
    assert req.searchable


def test_resistor_stated_and_remembered_power():
    stated = part_requirement(_intent(**{"Power:Min": "1/4W"}), "", DEFAULTS)
    assert (stated.rating_min.value, stated.rating_min.source) == (0.25, SCHEMATIC)
    remembered = part_requirement(_intent(), "", part_defaults({"res.power_min": "100mW"}))
    assert (remembered.rating_min.value, remembered.rating_min.source) == (0.1, DEFAULT)
    bad = part_requirement(_intent(**{"Power:Min": "lots"}), "", DEFAULTS)
    assert bad.problem() == "Power:Min on the schematic is not a power"


def test_resistor_library_value_and_footprint():
    req = part_requirement(_intent("R_49p9K"), "Resistor_SMD:R_0402_1005Metric", DEFAULTS)
    assert req.nominal == pytest.approx(49.9e3)
    assert (req.package.value, req.package.source) == ("0402", SCHEMATIC)


def test_resistor_description_fills_tolerance_and_warns_on_value():
    desc = DescriptionEvidence(nominal=4.7e3, package="0603", tolerance_pct=5.0)
    req = part_requirement(_intent("1K"), "", DEFAULTS, description=desc)
    assert (req.tolerance_max_pct.value, req.tolerance_max_pct.source) == (5.0, DESCRIPTION)
    assert (req.package.value, req.package.source) == ("0603", DESCRIPTION)
    assert req.warnings == ("Description says 4.7k; Value says 1k (Description not used)",)


def test_resistor_part_evidence():
    part = PartEvidence("current part X", nominal=10e3, rating=0.1, tolerance_pct=1.0,
                        package="0603")
    req = part_requirement(_intent(), "", DEFAULTS, part=part)
    assert (req.rating_min.value, req.rating_min.source) == (0.1, PART)
    assert (req.tolerance_max_pct.value, req.tolerance_max_pct.source) == (1.0, PART)
    other = PartEvidence("current part Y", nominal=100e3)
    assert part_requirement(_intent(), "", DEFAULTS, part=other).conflicts == (
        "Value says 10k; current part Y says 100k",)


def test_resistor_not_a_resistance():
    req = part_requirement(_intent("tbd"), "", DEFAULTS)
    assert req.problem() == "Value 'tbd' is not a resistance"


def _record(ref, intent_type="resistor", **props):
    data = {"Reference": ref, "dnp": False, "in_bom": True,
            "Intent:Type": intent_type, **props}
    return PartRecord(ref, data, Path("x.kicad_sch"), f"uuid-{ref}",
                      ElectricalIntent.from_dict(data))


def test_group_parts_splits_types_and_stated_power():
    recs = [_record("R1", Value="10K"), _record("R2", Value="10K"),
            _record("R3", Value="10K", **{"Power:Min": "0.25W"}),
            _record("C1", "capacitor", Value="10K")]
    groups = group_parts(recs, DEFAULTS)
    assert [(g.requirement.kind, g.references) for g in groups] == [
        ("capacitor", ["C1"]), ("resistor", ["R1", "R2"]), ("resistor", ["R3"])]


# --------------------------------------------------------------------------- #
# Parts database
# --------------------------------------------------------------------------- #

def _db() -> PartsDb:
    return PartsDb(":memory:", now=lambda: "2026-10-30T12:00:00+00:00")


def _choice(mpn="RC0603FR-0710KL", ohms=10e3, watts=0.1, tol=1.0, pkg="0603"):
    return PartChoice(kind="resistor", mpn=mpn, manufacturer="YAGEO", package=pkg,
                      params=PartParams(ohms, watts, "", tol),
                      digikey_pn="311-10.0KHRCT-ND", lcsc="C25804", jlc_library="basic")


def test_db_accept_and_find_resistor():
    db = _db()
    db.accept_part(_choice())
    db.accept_part(_choice("RC0603FR-0749K9L", ohms=49.9e3))
    hits = db.find_parts(kind="resistor", nominal=10e3, package="0603",
                         rating_min=None, dielectrics=(), tolerance_max_pct=1.0)
    assert [h.mpn for h in hits] == ["RC0603FR-0710KL"]
    assert hits[0].kind == "resistor"
    assert hits[0].params == PartParams(10e3, 0.1, "", 1.0)
    assert db.find_parts(kind="resistor", nominal=10e3, package="0603",
                         rating_min=0.25, dielectrics=(), tolerance_max_pct=None) == []
    # A resistor is never found as a capacitor of the same value.
    assert db.find_parts(kind="capacitor", nominal=10e3, package="0603",
                         rating_min=None, dielectrics=(), tolerance_max_pct=None) == []


def test_db_part_type_change_moves_its_parameters():
    db = _db()
    choice = _choice()
    db.accept_part(PartChoice(kind="capacitor", mpn=choice.mpn,
                              manufacturer=choice.manufacturer, package="0603",
                              params=PartParams(1e-7, 50.0, "X7R", 10.0)))
    db.accept_part(choice)
    rows = db.conn.execute("SELECT COUNT(*) FROM capacitor").fetchone()[0]
    assert rows == 0
    assert db.conn.execute("SELECT type FROM part").fetchone()[0] == "resistor"


# --------------------------------------------------------------------------- #
# Candidate search
# --------------------------------------------------------------------------- #

def _jlc(lcsc="C25804", mpn="RC0603FR-0710KL", *, mfr="YAGEO", ohms="10kΩ",
         watts="100mW", tol="±1%", package="0603", library="base",
         stock=500000) -> JlcPart:
    return JlcPart(lcsc=lcsc, mpn=mpn, manufacturer=mfr, library=library,
                   stock=stock, unit_price=0.0005, package=package,
                   attributes={"Resistance": ohms, "Power(Watts)": watts,
                               "Tolerance": tol, "Type": "Thick Film Resistor"})


def _dk(mpn="RC0603FR-0710KL", *, ohms="10 kOhms", watts="0.1W, 1/10W",
        tol="±1%", pns=("311-10.0KHRTR-ND", "311-10.0KHRCT-ND")) -> DigiKeyProduct:
    return DigiKeyProduct(mpn=mpn, digikey_pns=list(pns), quantity_available=900000,
                          product_status="Active", description="RES 10K OHM 1% 1/10W 0603",
                          datasheet_url="https://dk/ds.pdf",
                          parameters={"Resistance": ohms, "Power (Watts)": watts,
                                      "Tolerance": tol,
                                      "Package / Case": "0603 (1608 Metric)"})


class FakeJlc:
    def __init__(self, hits: List[JlcPart]):
        self.hits = hits
        self.calls: List[tuple] = []

    def filtered(self, value: str, package: str, category) -> List[JlcPart]:
        self.calls.append((value, package, category))
        return list(self.hits)

    def part(self, lcsc: str) -> Optional[JlcPart]:
        return next((h for h in self.hits if h.lcsc == lcsc), None)


class FakeDigiKey:
    configured = True

    def __init__(self, products: Dict[str, DigiKeyProduct]):
        self.products = products

    def product_for(self, pn: str) -> Optional[DigiKeyProduct]:
        return self.products.get(pn)


def test_resistor_search_uses_resistor_category_and_keyword():
    req = part_requirement(_intent(), "", DEFAULTS)
    jlc = FakeJlc([_jlc()])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"RC0603FR-0710KL": _dk()}), needed=10)
    assert jlc.calls == [("10kΩ", "0603", RESISTOR.jlc_categories[0])]
    assert [c.mpn for c in result.candidates] == ["RC0603FR-0710KL"]
    cand = result.candidates[0]
    assert (cand.kind, cand.source) == ("resistor", SEARCH)
    assert cand.digikey_pn == "311-10.0KHRCT-ND"
    assert cand.params == PartParams(10e3, 0.1, "", 1.0)
    assert set(result.facets) == {"rating", "tolerance"}


def test_resistor_search_checks_value_power_and_tolerance():
    req = part_requirement(_intent(**{"Power:Min": "0.125W"}), "", DEFAULTS)
    hits = [_jlc("C1", "A", ohms="10.2kΩ"), _jlc("C2", "B"),
            _jlc("C3", "C", watts="250mW", tol="±5%"), _jlc("C4", "D", watts="250mW")]
    jlc = FakeJlc(hits)
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"D": _dk("D", watts="0.25W, 1/4W")}),
                             needed=10)
    assert [c.mpn for c in result.candidates] == ["D"]
    reasons = dict(result.rejected)
    assert reasons["C1 A"] == "JLCPCB resistance 10.2k, need 10k"
    assert reasons["C2 B"] == "JLCPCB rating 100mW, need at least 125mW"
    assert reasons["C3 C"] == "JLCPCB tolerance 5%, need at most 1%"
    assert result.facets["rating"] == [("100mW", 1), ("250mW", 1)]


def test_resistor_manufacturer_list_applies():
    req = part_requirement(_intent(), "", DEFAULTS)
    jlc = FakeJlc([_jlc("C9", "0603WAF1002T5E", mfr="UNI-ROYAL(Uniroyal Elec)")])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({}), needed=10,
                             manufacturers=("Yageo", "Vishay"))
    assert result.candidates == []
    assert result.rejected == [
        ("C9 0603WAF1002T5E",
         "manufacturer 'UNI-ROYAL(Uniroyal Elec)' is not in the list")]


def test_resistor_any_power_writes_the_parts_rating():
    req = part_requirement(_intent(), "", DEFAULTS)
    jlc = FakeJlc([_jlc()])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"RC0603FR-0710KL": _dk()}), needed=10)
    written = requirement_for(req, result, result.candidates[0])
    assert (written.rating_min.value, written.rating_min.source) == (0.1, RELAXED)


# --------------------------------------------------------------------------- #
# Write plan
# --------------------------------------------------------------------------- #

def test_resistor_plan():
    rec = _record("R1", Value="10K")
    req = part_requirement(rec.intent, "", DEFAULTS)
    req = req.edited(rating_min=0.1)
    group = RequirementGroup(req, [Member("R1", rec.source_file, rec.symbol_uuid,
                                          rec.props, rec.intent)])
    jlc = FakeJlc([_jlc()])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"RC0603FR-0710KL": _dk()}), needed=10)
    props = assignment_plan(group, req, result.candidates[0])[Path("x.kicad_sch")]["uuid-R1"]
    assert props["Power:Min"] == "100mW"
    assert props["Tolerance:Preferred"] == "1%"
    assert props["Footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert props["LCSC"] == "C25804"
    assert "Class:Preferred" not in props and "Voltage:Min" not in props


# --------------------------------------------------------------------------- #
# Cockpit helpers
# --------------------------------------------------------------------------- #

pytest.importorskip("PySide6")

from kicadtools.gui.assign_dialog import facet_title, parse_field_values  # noqa: E402
from kicadtools.gui.parts import requirement_settings, sourced_cell, value_cell  # noqa: E402


def test_resistor_cells():
    req = part_requirement(_intent("R_49p9K"), "", DEFAULTS)
    assert value_cell(req) == "R_49p9K (49.9k)"
    assert sourced_cell(req.rating_min, "rating", RESISTOR) == "any"
    assert sourced_cell(req.dielectrics, "dielectric", RESISTOR) == ""
    assert sourced_cell(req.tolerance_max_pct, "tolerance", RESISTOR) == "1% (default)"
    assert facet_title("rating", RESISTOR) == "Power:Min"
    assert facet_title("rating", CAPACITOR) == "Voltage:Min"


def test_resistor_field_values():
    req = part_requirement(_intent(), "", DEFAULTS)
    # The power floor starts as "any": blank or "any" leaves it so.
    assert parse_field_values("0603", "", "", "1%", original=req) == {}
    assert parse_field_values("0603", "any", "", "1%", original=req) == {}
    assert parse_field_values("0603", "250mW", "X7R", "1%", original=req) == {
        "rating_min": 0.25}
    assert parse_field_values("0603", "lots", "", "", original=req) == \
        "Power:Min 'lots' is not a power"


def test_resistor_settings_keys():
    req = part_requirement(_intent(), "", DEFAULTS).edited(
        package="0402", rating_min=0.1, tolerance_max_pct=5.0)
    assert requirement_settings(req) == {
        "res.package": "0402", "res.power_min": "100mW", "res.tolerance": "5%"}
    assert req.package.source == EDITED


@pytest.fixture(scope="module")
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_resistor_dialog_fields(app):
    from kicadtools.gui.assign_dialog import AssignDialog
    rec = _record("R1", Value="10K")
    req = part_requirement(rec.intent, "", DEFAULTS)
    group = RequirementGroup(req, [Member("R1", rec.source_file, rec.symbol_uuid,
                                          rec.props, rec.intent)])
    dialog = AssignDialog(group, req, 25)
    assert dialog._fields["rating"].text() == "any"
    assert dialog._fields["tolerance"].text() == "1%"
    assert not dialog._fields["dielectric"].isVisibleTo(dialog)
    assert dialog.current_requirement() == req
    sent = []
    dialog.search_requested.connect(lambda r, s: sent.append((r, s)))
    # Searchable with no power floor: the first request lets assess derive it.
    assert dialog.request_search()
    assert sent == [(None, False)]
    dialog.close()
