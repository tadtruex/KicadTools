"""Inductor assignment: units, Value and Description parsing, the
requirement, the parts database, the candidate search, the write plan and the
Cockpit helpers. The shared code is tested with capacitors and resistors in
the other test files; these tests cover what the inductor part type changes:
the saturation-current floor, shielding in the class field, and body-size
packages for power inductors."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pytest

from kicadtools.assign import assignment_plan
from kicadtools.classify import guess_intent_type
from kicadtools.evidence import PartEvidence
from kicadtools.intent import ElectricalIntent, InductorIntent
from kicadtools.partsdb import PartChoice, PartParams, PartsDb
from kicadtools.partsearch import SEARCH, find_candidates, requirement_for
from kicadtools.parttypes import INDUCTOR, supplier_value, type_of_intent
from kicadtools.requirement import (
    ANY,
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    RELAXED,
    SCHEMATIC,
    UNSET,
    Member,
    RequirementGroup,
    group_parts,
    part_defaults,
    part_requirement,
)
from kicadtools.resolve import PartRecord
from kicadtools.suppliers import DigiKeyProduct, JlcPart
from kicadtools.units import (
    body_size,
    fmt_amps,
    fmt_henries,
    parse_amps,
    parse_ind_description,
    parse_ind_value,
    parse_inductance,
    same_body,
    shielding_tokens,
    shielding_upgrades,
)

DEFAULTS = part_defaults({})
COILCRAFT_FP = "Inductor_SMD:L_Coilcraft_XxL4040"


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, henries", [
    ("10uH", 10e-6), ("150 nH", 150e-9), ("4.7µH", 4.7e-6), ("4u7", 4.7e-6),
    ("1u", 1e-6), ("1mH", 1e-3), ("4R7", 4.7e-6), ("R47", 0.47e-6),
    ("100n", 100e-9), ("10", None), ("tbd", None),
])
def test_parse_inductance(text, henries):
    got = parse_inductance(text)
    assert got == (pytest.approx(henries) if henries is not None else None)


def test_fmt_henries_and_amps():
    assert [fmt_henries(v) for v in (10e-6, 150e-9, 4.7e-6, 1e-3)] == [
        "10uH", "150nH", "4.7uH", "1mH"]
    assert [fmt_amps(v) for v in (0.84, 1.5, 13.5)] == ["840mA", "1.5A", "13.5A"]


@pytest.mark.parametrize("text, amps", [
    ("1.3A", 1.3), ("840mA", 0.84), ("22.3 A", 22.3), ("13p5A", 13.5),
    ("-", None), ("", None),
])
def test_parse_amps(text, amps):
    assert parse_amps(text) == (pytest.approx(amps) if amps is not None else None)


@pytest.mark.parametrize("text, size", [
    ("SMD,4x4mm", "4x4mm"),
    ('0.157" L x 0.157" W (4.00mm x 4.00mm)', "4x4mm"),
    ("SMD,5.8x5.2mm", "5.8x5.2mm"),
    ("3.2 x 3.5 mm", "3.5x3.2mm"),
    ("4x4", "4x4mm"),
    ("0603", None),
])
def test_body_size(text, size):
    assert body_size(text) == size


def test_same_body_allows_rounding():
    assert same_body("4x4mm", "4.1x4.1mm")
    assert not same_body("4x4mm", "4.5x4mm")


@pytest.mark.parametrize("text, codes", [
    ("Magnetic Shielded Inductor", ("Shielded",)),
    ("Unshielded Inductor", ("Unshielded",)),
    ("Semi-Shielded", ("Semi-Shielded",)),
    ("Non-Shielded", ("Unshielded",)),
    ("Multilayer inductor", ()),
    ("shielded, semi", ("Shielded", "Semi-Shielded")),
])
def test_shielding_tokens(text, codes):
    assert shielding_tokens(text) == codes


def test_shielding_upgrades():
    assert shielding_upgrades("Unshielded") == ("Unshielded", "Semi-Shielded", "Shielded")
    assert shielding_upgrades("Semi-Shielded") == ("Semi-Shielded", "Shielded")
    assert shielding_upgrades("Shielded") == ("Shielded",)


@pytest.mark.parametrize("text, henries, package", [
    ("22uH", 22e-6, None),
    ("1u", 1e-6, None),
    ("L_10U_1A", 10e-6, None),
    ("L_4p7U_13p5A", 4.7e-6, None),
    ("L_10U_0805", 10e-6, "0805"),
    ("L-US6000-XXX-RC", None, None),
])
def test_parse_ind_value(text, henries, package):
    got, pkg = parse_ind_value(text)
    assert got == (pytest.approx(henries) if henries is not None else None)
    assert pkg == package


def test_parse_ind_description():
    ev = parse_ind_description("Inductor 4.7 uH, 13.5A")
    assert ev.nominal == pytest.approx(4.7e-6)
    assert ev.rating == pytest.approx(13.5)
    ev = parse_ind_description("10uH 20% shielded 4x4mm")
    assert (ev.tolerance_pct, ev.dielectrics, ev.package) == (20.0, ("Shielded",), "4x4mm")
    assert parse_ind_description("Inductor, small symbol").nominal is None


def test_supplier_value_skips_dashes():
    attrs = {"Current - Saturation (Isat)": "-", "Current Rating": "50mA"}
    names = ("Current - Saturation(Isat)", "Current - Saturation (Isat)", "Current Rating")
    assert supplier_value(attrs.get, names) == "50mA"


def test_ferrite_bead_on_an_inductor_footprint_is_a_ferrite():
    fb = guess_intent_type({"Reference": "FB1", "Value": "FB_120_100MHz",
                            "Footprint": "Inductor_SMD:L_0603_1608Metric"})
    assert fb.intent_type == "ferrite"
    bead = guess_intent_type({"Reference": "L2", "Description": "Ferrite bead",
                              "Footprint": "Inductor_SMD:L_0603_1608Metric"})
    assert bead.intent_type == "ferrite"
    ind = guess_intent_type({"Reference": "L1", "Footprint": COILCRAFT_FP})
    assert ind.intent_type == "inductor"


# --------------------------------------------------------------------------- #
# Requirement
# --------------------------------------------------------------------------- #

def _intent(value="10uH", **props):
    data = {"Reference": "L1", "dnp": False, "in_bom": True,
            "Intent:Type": "inductor", "Value": value, **props}
    return ElectricalIntent.from_dict(data)


def test_inductor_intent_reads_its_properties():
    i = _intent(**{"Current:Min": "1.5A", "Shielding:Preferred": "Semi-Shielded",
                   "Tolerance:Preferred": "20%"})
    assert isinstance(i, InductorIntent)
    assert (i.current_min, i.shielding_preferred, i.tolerance_preferred) == (
        "1.5A", ["Semi-Shielded"], ["20%"])
    assert type_of_intent(i) is INDUCTOR


def test_inductor_defaults_with_a_chip_footprint():
    req = part_requirement(_intent(), "Inductor_SMD:L_0805_2012Metric", DEFAULTS)
    assert req.kind == "inductor"
    assert (req.package.value, req.package.source) == ("0805", SCHEMATIC)
    assert (req.rating_min.value, req.rating_min.source) == (None, ANY)
    assert (req.dielectrics.value, req.dielectrics.source) == (("Shielded",), DEFAULT)
    assert (req.tolerance_max_pct.value, req.tolerance_max_pct.source) == (20.0, DEFAULT)
    assert req.searchable


def test_power_inductor_footprint_needs_a_body_size():
    req = part_requirement(_intent(), COILCRAFT_FP, DEFAULTS)
    assert (req.package.value, req.package.source) == (None, UNSET)
    assert req.problem() == (
        f"footprint '{COILCRAFT_FP}' names no chip size; enter the body size (4x4mm)")
    edited = req.edited(package="4x4")
    assert (edited.package.value, edited.package.source) == ("4x4mm", EDITED)
    assert edited.searchable


def test_chip_footprint_fixes_an_inductor_package():
    req = part_requirement(_intent(), "Inductor_SMD:L_0603_1608Metric", DEFAULTS)
    with pytest.raises(ValueError):
        req.edited(package="4x4mm")


def test_body_size_from_package_preferred_part_and_description():
    stated = part_requirement(_intent(**{"Package:Preferred": "4x4mm"}),
                              COILCRAFT_FP, DEFAULTS)
    assert (stated.package.value, stated.package.source) == ("4x4mm", SCHEMATIC)
    part = PartEvidence(label="current part", nominal=10e-6, rating=2.8,
                        dielectrics=("Shielded",), tolerance_pct=20.0, package="4x4mm")
    from_part = part_requirement(_intent(), COILCRAFT_FP, DEFAULTS, part=part)
    assert (from_part.package.value, from_part.package.source) == ("4x4mm", PART)
    assert (from_part.rating_min.value, from_part.rating_min.source) == (2.8, PART)
    desc = parse_ind_description("Inductor 10 uH, 1A, 4x4mm")
    from_desc = part_requirement(_intent(), COILCRAFT_FP, DEFAULTS, description=desc)
    assert (from_desc.package.value, from_desc.package.source) == ("4x4mm", DESCRIPTION)
    assert (from_desc.rating_min.value, from_desc.rating_min.source) == (1.0, DESCRIPTION)


def test_body_size_conflict_with_the_part():
    part = PartEvidence(label="current part X", nominal=10e-6, package="5x5mm")
    req = part_requirement(_intent(**{"Package:Preferred": "4x4mm"}), COILCRAFT_FP,
                           DEFAULTS, part=part)
    assert req.conflicts == ("Package:Preferred says 4x4mm; current part X says 5x5mm",)


def test_stated_current_and_shielding():
    req = part_requirement(_intent(**{"Current:Min": "2A",
                                      "Shielding:Preferred": "Semi-Shielded"}),
                           "", DEFAULTS)
    assert (req.rating_min.value, req.rating_min.source) == (2.0, SCHEMATIC)
    assert (req.dielectrics.value, req.dielectrics.source) == (("Semi-Shielded",), SCHEMATIC)


def test_inductor_without_footprint_or_default_package():
    req = part_requirement(_intent("1u"), "", DEFAULTS)
    assert (req.package.value, req.package.source) == (None, UNSET)
    assert req.problem() == "no package"
    remembered = part_requirement(_intent("1u"), "", part_defaults({"ind.package": "0805"}))
    assert (remembered.package.value, remembered.package.source) == ("0805", DEFAULT)


def _record(ref, intent_type="inductor", **props):
    data = {"Reference": ref, "dnp": False, "in_bom": True,
            "Intent:Type": intent_type, **props}
    return PartRecord(ref, data, Path("x.kicad_sch"), f"uuid-{ref}",
                      ElectricalIntent.from_dict(data))


def test_group_parts_splits_stated_current():
    recs = [_record("L1", Value="10uH"), _record("L2", Value="10uH"),
            _record("L3", Value="10uH", **{"Current:Min": "2A"}),
            _record("R1", "resistor", Value="10K")]
    groups = group_parts(recs, DEFAULTS)
    assert [(g.requirement.kind, g.references) for g in groups] == [
        ("inductor", ["L1", "L2"]), ("inductor", ["L3"]), ("resistor", ["R1"])]


# --------------------------------------------------------------------------- #
# Parts database
# --------------------------------------------------------------------------- #

def _db() -> PartsDb:
    return PartsDb(":memory:", now=lambda: "2026-11-03T12:00:00+00:00")


def _choice(mpn="SRN4018-100M", pkg="4x4mm", amps=1.4, shielding="Shielded"):
    return PartChoice(kind="inductor", mpn=mpn, manufacturer="Bourns", package=pkg,
                      params=PartParams(10e-6, amps, shielding, 20.0),
                      digikey_pn="SRN4018-100MCT-ND", lcsc="C503307",
                      jlc_library="extended")


def test_db_accept_and_find_inductor_by_body_size():
    db = _db()
    db.accept_part(_choice())
    find = dict(kind="inductor", nominal=10e-6, rating_min=1.0,
                dielectrics=("Shielded",), tolerance_max_pct=20.0)
    assert [h.mpn for h in db.find_parts(package="4.1x4.1mm", **find)] == ["SRN4018-100M"]
    assert db.find_parts(package="5x5mm", **find) == []
    assert db.find_parts(package="4x4mm", **{**find, "rating_min": 2.0}) == []
    assert db.find_parts(package="4x4mm", **{**find, "dielectrics": ("Unshielded",)}) == []
    hit = db.find_parts(package="4x4mm", **find)[0]
    assert hit.params == PartParams(10e-6, 1.4, "Shielded", 20.0)


# --------------------------------------------------------------------------- #
# Candidate search
# --------------------------------------------------------------------------- #

def _jlc(lcsc="C503307", mpn="SRN4018-100M", *, mfr="BOURNS", henries="10uH",
         isat="1.4A", rated="1.3A", tol="±20%", package="SMD,4x4mm",
         kind="Magnetic Shielded Inductor", stock=50000) -> JlcPart:
    attrs = {"Inductance": henries, "Current - Saturation(Isat)": isat,
             "Current Rating": rated, "Tolerance": tol}
    if kind:
        attrs["Type"] = kind
    return JlcPart(lcsc=lcsc, mpn=mpn, manufacturer=mfr, library="expand",
                   stock=stock, unit_price=0.1, package=package, attributes=attrs)


def _dk(mpn="SRN4018-100M", *, henries="10 µH", isat="1.4A", rated="1.3 A",
        shielding="Shielded", case="Nonstandard",
        size='0.157" L x 0.157" W (4.00mm x 4.00mm)') -> DigiKeyProduct:
    return DigiKeyProduct(mpn=mpn, digikey_pns=["SRN4018-100MTR-ND", "SRN4018-100MCT-ND"],
                          quantity_available=13000, product_status="Active",
                          description="FIXED IND 10UH 1.3A 180 MOHM SMD",
                          datasheet_url="https://dk/ds.pdf",
                          parameters={"Inductance": henries, "Tolerance": "±20%",
                                      "Current - Saturation (Isat)": isat,
                                      "Current Rating (Amps)": rated,
                                      "Shielding": shielding,
                                      "Package / Case": case, "Size / Dimension": size})


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


def _power_req(**props):
    return part_requirement(_intent(**props), COILCRAFT_FP, DEFAULTS).edited(package="4x4mm")


def test_body_size_search_uses_the_power_category_without_a_package_filter():
    jlc = FakeJlc([_jlc()])
    result = find_candidates(_power_req(), db=_db(), jlc_filtered=jlc.filtered,
                             jlc_part=jlc.part,
                             digikey=FakeDigiKey({"SRN4018-100M": _dk()}), needed=10)
    assert jlc.calls == [("10uH", "", ("Inductors, Coils, Chokes", "Power Inductors"))]
    assert [c.mpn for c in result.candidates] == ["SRN4018-100M"]
    cand = result.candidates[0]
    assert (cand.kind, cand.source, cand.package) == ("inductor", SEARCH, "4x4mm")
    assert cand.params == PartParams(pytest.approx(10e-6), 1.4, "Shielded", 20.0)
    assert set(result.facets) == {"rating", "dielectric", "tolerance"}


def test_chip_search_uses_both_inductor_categories():
    req = part_requirement(_intent("1u"), "Inductor_SMD:L_0805_2012Metric", DEFAULTS)
    jlc = FakeJlc([])
    find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                    digikey=FakeDigiKey({}), needed=10, search_suppliers=True)
    assert jlc.calls == [
        ("1uH", "0805", ("Inductors, Coils, Chokes", "Inductors (SMD)")),
        ("1uH", "0805", ("Inductors, Coils, Chokes", "Power Inductors")),
    ]


def test_search_checks_saturation_current_and_body_size():
    req = _power_req(**{"Current:Min": "1.5A"})
    hits = [_jlc("C1", "A", isat="1.4A"), _jlc("C2", "B", package="SMD,5x5mm"),
            _jlc("C3", "C", isat="2A", kind="Unshielded Inductor"),
            _jlc("C4", "D", isat="2A", package="SMD,4.1x4.1mm")]
    jlc = FakeJlc(hits)
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"D": _dk("D", isat="2A")}), needed=10)
    assert [c.mpn for c in result.candidates] == ["D"]
    reasons = dict(result.rejected)
    assert reasons["C1 A"] == "JLCPCB rating 1.4A, need at least 1.5A"
    assert reasons["C2 B"] == "JLCPCB package 'SMD,5x5mm', need 4x4mm"
    assert reasons["C3 C"] == "JLCPCB shielding Unshielded is not one of Shielded"


def test_rated_current_is_used_where_saturation_is_not_listed():
    req = part_requirement(_intent("1u", **{"Current:Min": "100mA"}),
                           "Inductor_SMD:L_0805_2012Metric", DEFAULTS)
    hit = _jlc("C9", "MLF2012", henries="1uH", isat="-", rated="80mA", package="0805",
               kind="")
    jlc = FakeJlc([hit])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({}), needed=10)
    assert dict(result.rejected)["C9 MLF2012"] == "JLCPCB rating 80mA, need at least 100mA"


def test_shielding_unlisted_at_jlcpcb_is_taken_from_digikey():
    req = part_requirement(_intent("1u"), "Inductor_SMD:L_0805_2012Metric", DEFAULTS)
    hit = _jlc("C9", "LQM21", henries="1uH", isat="-", rated="600mA", package="0805",
               kind="")
    jlc = FakeJlc([hit])
    product = _dk("LQM21", henries="1 µH", isat="-", rated="600mA",
                  case="0805 (2012 Metric)", size='0.079" L x 0.049" W (2.00mm x 1.25mm)')
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"LQM21": product}), needed=10)
    assert [c.mpn for c in result.candidates] == ["LQM21"]
    assert result.candidates[0].params.dielectric == "Shielded"
    assert result.candidates[0].package == "0805"


def test_any_current_writes_the_parts_rating():
    req = _power_req()
    jlc = FakeJlc([_jlc()])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"SRN4018-100M": _dk()}), needed=10)
    written = requirement_for(req, result, result.candidates[0])
    assert (written.rating_min.value, written.rating_min.source) == (1.4, RELAXED)


# --------------------------------------------------------------------------- #
# Write plan
# --------------------------------------------------------------------------- #

def _group(rec, req):
    return RequirementGroup(req, [Member(rec.reference, rec.source_file, rec.symbol_uuid,
                                         rec.props, rec.intent)])


def test_power_inductor_plan_writes_the_size_and_keeps_the_footprint():
    rec = _record("L1", Value="10uH", Footprint=COILCRAFT_FP)
    req = part_requirement(rec.intent, COILCRAFT_FP, DEFAULTS).edited(
        package="4x4mm", rating_min=1.0)
    jlc = FakeJlc([_jlc()])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"SRN4018-100M": _dk()}), needed=10)
    props = assignment_plan(_group(rec, req), req, result.candidates[0])[
        Path("x.kicad_sch")]["uuid-L1"]
    assert props["Current:Min"] == "1A"
    assert props["Shielding:Preferred"] == "Shielded"
    assert props["Tolerance:Preferred"] == "20%"
    assert props["Package:Preferred"] == "4x4mm"
    assert props["MPN"] == "SRN4018-100M"
    assert "Footprint" not in props
    assert "Class:Preferred" not in props


def test_power_inductor_plan_without_footprint_writes_none():
    rec = _record("L1", Value="10uH")
    req = part_requirement(rec.intent, "", DEFAULTS).edited(package="4x4mm")
    jlc = FakeJlc([_jlc()])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"SRN4018-100M": _dk()}), needed=10)
    props = assignment_plan(_group(rec, req), req, result.candidates[0])[
        Path("x.kicad_sch")]["uuid-L1"]
    assert "Footprint" not in props
    assert props["Package:Preferred"] == "4x4mm"


def test_chip_inductor_plan_writes_the_chip_footprint():
    rec = _record("L1", Value="1u")
    req = part_requirement(rec.intent, "", DEFAULTS).edited(package="0805")
    hit = _jlc("C9", "LQM21", henries="1uH", isat="-", rated="600mA", package="0805",
               kind="")
    product = _dk("LQM21", henries="1 µH", isat="-", rated="600mA",
                  case="0805 (2012 Metric)", size="")
    jlc = FakeJlc([hit])
    result = find_candidates(req, db=_db(), jlc_filtered=jlc.filtered, jlc_part=jlc.part,
                             digikey=FakeDigiKey({"LQM21": product}), needed=10)
    props = assignment_plan(_group(rec, req), req, result.candidates[0])[
        Path("x.kicad_sch")]["uuid-L1"]
    assert props["Footprint"] == "Inductor_SMD:L_0805_2012Metric"
    assert props["Package:Preferred"] == "0805"


# --------------------------------------------------------------------------- #
# Cockpit helpers
# --------------------------------------------------------------------------- #

pytest.importorskip("PySide6")

from kicadtools.gui.assign_dialog import facet_title, parse_field_values  # noqa: E402
from kicadtools.gui.parts import (  # noqa: E402
    STATUS_UNASSIGNED,
    PartsTableModel,
    requirement_settings,
    sourced_cell,
    value_cell,
)


def test_inductor_cells():
    req = part_requirement(_intent("L_4p7U_13p5A"), "Inductor_SMD:L_0805_2012Metric",
                           DEFAULTS)
    assert value_cell(req) == "L_4p7U_13p5A (4.7uH)"
    assert sourced_cell(req.rating_min, "rating", INDUCTOR) == "any"
    assert sourced_cell(req.dielectrics, "dielectric", INDUCTOR) == "Shielded (default)"
    assert sourced_cell(req.tolerance_max_pct, "tolerance", INDUCTOR) == "20% (default)"
    assert facet_title("rating", INDUCTOR) == "Current:Min"
    assert facet_title("dielectric", INDUCTOR) == "Shielding"


def test_power_inductor_row_is_unassigned_not_unsearchable():
    rec = _record("L1", Value="10uH", Footprint=COILCRAFT_FP)
    groups = group_parts([rec], DEFAULTS)
    model = PartsTableModel(groups)
    assert model.status(0) == STATUS_UNASSIGNED


def test_inductor_field_values():
    req = part_requirement(_intent(), COILCRAFT_FP, DEFAULTS)
    assert parse_field_values("4x4", "", "Shielded", "20%", original=req) == {
        "package": "4x4mm"}
    assert parse_field_values("", "", "Shielded", "20%", original=req) == (
        "Enter the package (a chip size or a body size such as 4x4mm) to search")
    assert parse_field_values("4x4mm", "2A", "semi", "20%", original=req) == {
        "package": "4x4mm", "rating_min": 2.0, "dielectrics": ("Semi-Shielded",)}
    assert parse_field_values("4x4mm", "", "wound", "", original=req) == (
        "Shielding 'wound' names no shielding such as Shielded")
    assert parse_field_values("XGL", "", "", "", original=req) == (
        "Package 'XGL' is not a chip size such as 0603 or a body size such as 4x4mm")


def test_inductor_settings_keys():
    req = part_requirement(_intent(), "", DEFAULTS).edited(
        package="4x4mm", rating_min=1.5, dielectrics=("Semi-Shielded",),
        tolerance_max_pct=30.0)
    assert requirement_settings(req) == {
        "ind.package": "4x4mm", "ind.current_min": "1.5A",
        "ind.shielding": "Semi-Shielded", "ind.tolerance": "30%"}


@pytest.fixture(scope="module")
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_inductor_dialog_fields(app):
    from kicadtools.gui.assign_dialog import AssignDialog
    rec = _record("L1", Value="10uH", Footprint=COILCRAFT_FP)
    req = part_requirement(rec.intent, COILCRAFT_FP, DEFAULTS)
    dialog = AssignDialog(_group(rec, req), req, 25)
    assert dialog._fields["package"].isEnabled()
    assert dialog._fields["package"].text() == ""
    assert dialog._fields["dielectric"].isVisibleTo(dialog)
    assert dialog._fields["dielectric"].text() == "Shielded"
    assert dialog._fields["rating"].text() == "any"
    dialog._fields["package"].setText("4x4")
    assert dialog.current_requirement().package.value == "4x4mm"
    dialog.close()
