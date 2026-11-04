from pathlib import Path

import pytest

from dataclasses import replace

from kicadtools.evidence import DescriptionEvidence, ExistingPart, PartEvidence
from kicadtools.intent import CapacitorIntent, ElectricalIntent, ResistorIntent
from kicadtools.requirement import (
    BUILTIN_DEFAULTS,
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    SCHEMATIC,
    UNSET,
    Sourced,
    part_defaults,
    part_requirement,
    dielectric_band,
    group_parts,
    tolerance_class,
)
from kicadtools.resolve import PartRecord, iter_bom_part_records

DATA = Path(__file__).parent / "data"
DEFAULTS = part_defaults({})


def _cap(value="100nF", **props):
    data = {"Reference": props.pop("Reference", "C1"), "dnp": False,
            "in_bom": True, "Intent:Type": "capacitor", "Value": value, **props}
    intent = ElectricalIntent.from_dict(data)
    assert isinstance(intent, CapacitorIntent)
    return intent


def _req(value="100nF", footprint="", defaults=DEFAULTS, **props):
    return part_requirement(_cap(value, **props), footprint, defaults)


# --------------------------------------------------------------------------- #
# Defaults and helpers
# --------------------------------------------------------------------------- #

def test_capacitor_defaults_overlay():
    d = part_defaults({"cap.package": "0402", "cap.voltage_min": "25V"})
    assert d["cap.package"] == "0402"
    assert d["cap.voltage_min"] == "25V"
    assert d["cap.dielectric.mid"] == "X7R"
    assert "cap.voltage_min" not in BUILTIN_DEFAULTS
    assert part_defaults({}) == BUILTIN_DEFAULTS


@pytest.mark.parametrize("farads,band", [
    (100e-12, "small"),
    (1e-9, "small"),
    (0.001e-6, "small"),     # "0.001uF" parses with float error
    (1.0001e-9, "mid"),
    (1.2e-9, "mid"),
    (100e-9, "mid"),
    (9.9e-6, "mid"),
    (10e-6, "bulk"),
    (10.1e-6, "bulk"),
    (100e-6, "bulk"),
])
def test_dielectric_band_boundaries(farads, band):
    assert dielectric_band(farads) == band


def test_tolerance_class():
    assert tolerance_class(["C0G", "NP0"]) == "class1"
    assert tolerance_class(["cog"]) == "class1"
    assert tolerance_class(["C0G", "X7R"]) == "class2"
    assert tolerance_class(["X7R", "X5R"]) == "class2"
    assert tolerance_class([]) == "class2"


# --------------------------------------------------------------------------- #
# Default rules
# --------------------------------------------------------------------------- #

def test_package_default():
    r = _req()
    assert (r.package.value, r.package.source) == ("0603", DEFAULT)


def test_package_default_from_setting():
    r = _req(defaults=part_defaults({"cap.package": "0805"}))
    assert (r.package.value, r.package.source) == ("0805", DEFAULT)


def test_voltage_unset_without_setting():
    r = _req()
    assert (r.rating_min.value, r.rating_min.source) == (None, UNSET)
    assert not r.searchable
    assert "Voltage:Min" in r.problem()


def test_voltage_default_from_setting():
    r = _req(defaults=part_defaults({"cap.voltage_min": "25V"}))
    assert (r.rating_min.value, r.rating_min.source) == (25.0, DEFAULT)
    assert r.searchable
    assert r.problem() == ""


def test_empty_voltage_property_is_not_stated():
    r = _req(**{"Voltage:Min": ""})
    assert r.rating_min.source == UNSET


@pytest.mark.parametrize("value,dielectrics,tol", [
    ("100pF", ("C0G", "NP0"), 5.0),
    ("1nF", ("C0G", "NP0"), 5.0),
    ("1.2nF", ("X7R",), 10.0),
    ("100nF", ("X7R",), 10.0),
    ("4.7uF", ("X7R",), 10.0),
    ("10uF", ("X7R", "X5R"), 10.0),
    ("47uF", ("X7R", "X5R"), 10.0),
])
def test_dielectric_and_tolerance_defaults(value, dielectrics, tol):
    r = _req(value)
    assert (r.dielectrics.value, r.dielectrics.source) == (dielectrics, DEFAULT)
    assert (r.tolerance_max_pct.value, r.tolerance_max_pct.source) == (tol, DEFAULT)


def test_dielectric_and_tolerance_defaults_from_settings():
    d = part_defaults({"cap.dielectric.mid": "X5R / X7S",
                            "cap.tolerance.class2": "20%"})
    r = _req("100nF", defaults=d)
    assert r.dielectrics.value == ("X5R", "X7S")
    assert r.tolerance_max_pct.value == 20.0


def test_stated_class1_dielectric_takes_class1_tolerance():
    r = _req("100nF", **{"Class:Preferred": "C0G"})
    assert r.dielectrics.value == ("C0G",)
    assert (r.tolerance_max_pct.value, r.tolerance_max_pct.source) == (5.0, DEFAULT)


# --------------------------------------------------------------------------- #
# Schematic values override defaults
# --------------------------------------------------------------------------- #

def test_capacitance_parsed():
    r = _req("4u7")
    assert r.value_text == "4u7"
    assert r.nominal == pytest.approx(4.7e-6)


def test_package_from_footprint():
    r = _req(footprint="Capacitor_SMD:C_0402_1005Metric",
             **{"Package:Preferred": "0805"})
    assert (r.package.value, r.package.source) == ("0402", SCHEMATIC)
    assert r.footprint == "Capacitor_SMD:C_0402_1005Metric"


def test_package_from_package_preferred():
    r = _req(**{"Package:Preferred": "0805, 1206"})
    assert (r.package.value, r.package.source) == ("0805", SCHEMATIC)


def test_voltage_from_schematic():
    r = _req(defaults=part_defaults({"cap.voltage_min": "25V"}),
             **{"Voltage:Min": "50"})
    assert (r.rating_min.value, r.rating_min.source) == (50.0, SCHEMATIC)


def test_dielectrics_from_schematic_normalized():
    r = _req("1nF", **{"Class:Preferred": "x7r, cog"})
    assert (r.dielectrics.value, r.dielectrics.source) == (("X7R", "C0G"), SCHEMATIC)


def test_tolerance_from_schematic_takes_loosest():
    r = _req(**{"Tolerance:Preferred": "1%, 2%"})
    assert (r.tolerance_max_pct.value, r.tolerance_max_pct.source) == (2.0, SCHEMATIC)


# --------------------------------------------------------------------------- #
# Problems
# --------------------------------------------------------------------------- #

def test_unparseable_value_not_searchable():
    r = _req("DNP-cap", **{"Voltage:Min": "16V"})
    assert r.nominal is None
    assert not r.searchable
    assert "DNP-cap" in r.problem()
    assert (r.dielectrics.value, r.dielectrics.source) == ((), UNSET)


def test_unknown_package_preferred_is_a_problem():
    r = _req(**{"Voltage:Min": "16V", "Package:Preferred": "Radial"})
    assert r.package.value == "Radial"
    assert not r.searchable
    assert "Radial" in r.problem()


def test_footprint_without_chip_size_is_a_problem():
    fp = "Capacitor_THT:CP_Radial_D6.3mm_P2.50mm"
    r = _req("47uF", footprint=fp, **{"Voltage:Min": "50"})
    assert (r.package.value, r.package.source) == (None, SCHEMATIC)
    assert not r.searchable
    assert fp in r.problem()


# --------------------------------------------------------------------------- #
# edited()
# --------------------------------------------------------------------------- #

def test_edited_replaces_and_tags():
    r = _req()
    e = r.edited(package="0402", rating_min=25, dielectrics=["x5r"],
                 tolerance_max_pct=20)
    assert (e.package.value, e.package.source) == ("0402", EDITED)
    assert (e.rating_min.value, e.rating_min.source) == (25.0, EDITED)
    assert (e.dielectrics.value, e.dielectrics.source) == (("X5R",), EDITED)
    assert (e.tolerance_max_pct.value, e.tolerance_max_pct.source) == (20.0, EDITED)
    assert e.searchable
    # the original is unchanged
    assert r.rating_min.source == UNSET
    assert r.package.source == DEFAULT


def test_edited_none_leaves_fields():
    r = _req(**{"Voltage:Min": "16"})
    e = r.edited(tolerance_max_pct=5)
    assert e.rating_min == r.rating_min
    assert e.package == r.package
    assert e.dielectrics == r.dielectrics
    assert e.tolerance_max_pct.source == EDITED


def test_edited_package_rejected_with_footprint():
    r = _req(footprint="Capacitor_SMD:C_0603_1608Metric")
    with pytest.raises(ValueError):
        r.edited(package="0402")
    assert r.edited(rating_min=10).rating_min.value == 10.0


# --------------------------------------------------------------------------- #
# group_parts on real fixtures
# --------------------------------------------------------------------------- #

def _groups(name, defaults=DEFAULTS):
    records = iter_bom_part_records(DATA / name)
    return records, group_parts(records, defaults)


def _group_with(groups, ref):
    return next(g for g in groups if ref in g.references)


def test_capacitor_groups_contain_only_capacitors_motordriver():
    records, groups = _groups("MotorDriver.kicad_sch")
    caps = [r for r in records if isinstance(r.intent, CapacitorIntent)]
    assert len(caps) < len(records)  # the fixture has non-capacitors too
    cap_groups = [g for g in groups if g.requirement.kind == "capacitor"]
    members = [m for g in cap_groups for m in g.members]
    assert all(isinstance(m.intent, CapacitorIntent) for m in members)
    assert sorted(m.reference for m in members) == sorted(r.reference for r in caps)
    assert sum(g.quantity for g in cap_groups) == len(caps)
    # Every other group is a resistor or inductor group.
    assert {g.requirement.kind for g in groups} == {"capacitor", "resistor", "inductor"}


def test_group_splits_on_stated_voltage_motordriver():
    _, groups = _groups("MotorDriver.kicad_sch")
    # 0.1uF 0402: C15-C18, C20 state Voltage:Min 16; C36-C38 state none.
    # C36 and C37 also hold an MPN and the Description "0.1uF, 25V, X7R 603";
    # C38 holds neither, so it is a separate group (revision 2 group key).
    stated = _group_with(groups, "C15")
    unstated = _group_with(groups, "C38")
    described = _group_with(groups, "C36")
    assert len({id(stated), id(unstated), id(described)}) == 3
    assert stated.references == ["C15", "C16", "C17", "C18", "C20"]
    assert unstated.references == ["C38"]
    assert described.references == ["C36", "C37"]
    assert (stated.requirement.rating_min.value,
            stated.requirement.rating_min.source) == (16.0, SCHEMATIC)
    assert unstated.requirement.rating_min.source == UNSET
    assert stated.requirement.package.value == "0402"
    d = described.requirement
    assert (d.rating_min.value, d.rating_min.source) == (25.0, DESCRIPTION)
    assert d.dielectrics == Sourced(("X7R",), DESCRIPTION)
    # the Description's "603" disagrees with the 0402 footprint: a warning
    assert d.conflicts == ()
    assert d.warnings == ("Description says 0603; Footprint says 0402 (Description not used)",)
    assert d.package == Sourced("0402", SCHEMATIC)
    assert described.existing.mpn == "CGA3E2X7R1E104K080AA"


def test_hierarchical_instances_share_uuid_motordriver():
    _, groups = _groups("MotorDriver.kicad_sch")
    g = _group_with(groups, "C1")
    assert g.references == ["C1", "C3", "C7", "C9"]
    # two subsheet symbols, each instantiated twice
    assert len({m.symbol_uuid for m in g.members}) == 2
    assert all(m.source_file.name == "HalfBridges.kicad_sch" for m in g.members)
    req = g.requirement
    assert req.dielectrics.value == ("X7R", "X5R")
    assert req.dielectrics.source == SCHEMATIC
    assert req.rating_min.value == 50.0


def test_groups_ordered_and_references_natural_sorted_motordriver():
    _, groups = _groups("MotorDriver.kicad_sch")
    from kicadtools.naturalsort import natural_key
    firsts = [g.references[0] for g in groups]
    assert firsts == sorted(firsts, key=natural_key)
    for g in groups:
        assert g.references == sorted(g.references, key=natural_key)


def test_radial_footprint_group_not_searchable_motordriver():
    _, groups = _groups("MotorDriver.kicad_sch")
    g = _group_with(groups, "C31")
    assert g.requirement.package.value is None
    assert not g.requirement.searchable


def test_groups_ecg():
    _, groups = _groups("ECG.kicad_sch")
    groups = [g for g in groups if g.requirement.kind == "capacitor"]
    by_value = {g.requirement.value_text: g for g in groups}
    assert set(by_value) == {"100nF", "1nF", "1u", "4.7n"}
    assert by_value["100nF"].references == ["C1", "C3", "C4", "C12", "C13", "C14", "C17"]
    assert by_value["1nF"].references == ["C5", "C8", "C9", "C10", "C11"]
    small = by_value["1nF"].requirement
    assert small.dielectrics.value == ("C0G", "NP0")
    assert small.tolerance_max_pct.value == 5.0
    assert by_value["4.7n"].requirement.dielectrics.value == ("X7R",)
    assert by_value["1u"].requirement.nominal == pytest.approx(1e-6)
    assert all(g.requirement.package.value == "0603" for g in groups)
    assert all(not g.assigned for g in groups)


# --------------------------------------------------------------------------- #
# Hand-built records: non-capacitors, assigned
# --------------------------------------------------------------------------- #

FULL = {"MPN": "CL10B104KB8NNNC", "Manufacturer": "Samsung",
        "DigiKeyPN": "1276-1000-1-ND", "LCSC": "C1591",
        "Footprint": "Capacitor_SMD:C_0603_1608Metric"}


def _record(ref, **props):
    data = {"Reference": ref, "dnp": False, "in_bom": True,
            "Intent:Type": "capacitor", "Value": "100nF", **props}
    return PartRecord(ref, data, Path("x.kicad_sch"), f"uuid-{ref}",
                      ElectricalIntent.from_dict(data))


def test_resistor_grouped_and_intentless_records_skipped():
    res_props = {"Reference": "R1", "dnp": False, "in_bom": True,
                 "Intent:Type": "resistor", "Value": "10k"}
    resistor = PartRecord("R1", res_props, Path("x"), "u",
                          ElectricalIntent.from_dict(res_props))
    assert isinstance(resistor.intent, ResistorIntent)
    bare = PartRecord("C9", {"Value": "1uF"}, Path("x"), "u2", None)
    groups = group_parts([resistor, bare, _record("C1")], DEFAULTS)
    assert [g.references for g in groups] == [["C1"], ["R1"]]
    assert [g.requirement.kind for g in groups] == ["capacitor", "resistor"]


def test_assigned_requires_every_field_on_every_member():
    g = group_parts([_record("C1", **FULL), _record("C2", **FULL)], DEFAULTS)
    assert len(g) == 1 and g[0].assigned

    # A missing MPN, DigiKey PN or LCSC changes the group key (revision 2),
    # so the partial record is its own group; Manufacturer is not in the key.
    for missing in FULL:
        partial = {k: v for k, v in FULL.items() if k != missing}
        groups = group_parts(
            [_record("C1", **FULL), _record("C2", **{**partial, "Footprint": FULL["Footprint"]})]
            if missing != "Footprint" else
            [_record("C1", **{**FULL, "Footprint": ""}), _record("C2", **{**FULL, "Footprint": ""})],
            DEFAULTS,
        )
        if missing in ("MPN", "DigiKeyPN", "LCSC"):
            assert len(groups) == 2, missing
            assert _group_with(groups, "C1").assigned, missing
            assert not _group_with(groups, "C2").assigned, missing
        else:
            assert len(groups) == 1, missing
            assert not groups[0].assigned, missing


def test_assigned_reads_alias_spellings():
    props = {"Manufacturer_PN": "CL10B104KB8NNNC", "Mfr": "Samsung",
             "Digi-Key_PN": "1276-1000-1-ND", "JLCPCB": "C1591",
             "Footprint": FULL["Footprint"]}
    g = group_parts([_record("C1", **props)], DEFAULTS)
    assert g[0].assigned


def test_group_key_splits_on_each_stated_property():
    base = _record("C1")
    variants = [
        _record("C2", Footprint="Capacitor_SMD:C_0402_1005Metric"),
        _record("C3", **{"Voltage:Min": "25V"}),
        _record("C4", **{"Class:Preferred": "X5R"}),
        _record("C5", **{"Tolerance:Preferred": "5%"}),
        _record("C6", **{"Package:Preferred": "0805"}),
        _record("C7", Value="1uF"),
    ]
    same = _record("C10", Value=" 100nF ")
    groups = group_parts([base, *variants, same], DEFAULTS)
    assert len(groups) == 7
    assert groups[0].references == ["C1", "C10"]
    assert [g.references[0] for g in groups] == ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]


# --------------------------------------------------------------------------- #
# Review edge cases
# --------------------------------------------------------------------------- #

def test_stated_unparseable_voltage_kept_as_schematic():
    # A stated value is never replaced by the remembered default.
    r = _req(defaults=part_defaults({"cap.voltage_min": "25V"}),
             **{"Voltage:Min": "tbd"})
    assert (r.rating_min.value, r.rating_min.source) == (None, SCHEMATIC)
    assert not r.searchable
    assert "Voltage:Min" in r.problem()


def test_stated_non_percent_tolerance_kept_as_schematic():
    r = _req("10pF", **{"Tolerance:Preferred": "±0.25pF"})
    assert (r.tolerance_max_pct.value, r.tolerance_max_pct.source) == (None, SCHEMATIC)


def test_stated_tolerance_float_and_sign():
    r = _req(**{"Tolerance:Preferred": "±0.5%, 1 %"})
    assert r.tolerance_max_pct.value == pytest.approx(1.0)


def test_stated_dielectric_with_slash_splits_and_dedupes():
    r = _req("1nF", **{"Class:Preferred": "C0G/NP0, cog"})
    assert (r.dielectrics.value, r.dielectrics.source) == (("C0G", "NP0"), SCHEMATIC)
    assert r.tolerance_max_pct.value == 5.0


def test_tolerance_class_splits_combined_entry():
    assert tolerance_class(["C0G/NP0"]) == "class1"


def test_blank_settings_fall_back_to_builtin():
    d = {"cap.package": "  ", "cap.dielectric.mid": "", "cap.tolerance.class2": ""}
    r = part_requirement(_cap("100nF"), "", d)
    assert (r.package.value, r.package.source) == ("0603", DEFAULT)
    assert (r.dielectrics.value, r.dielectrics.source) == (("X7R",), DEFAULT)
    assert r.tolerance_max_pct.value == 10.0
    # a raw settings mapping without the built-ins gives the same result
    assert part_requirement(_cap("100nF"), "", {}).package.value == "0603"


def test_package_setting_normalized():
    r = _req(defaults=part_defaults({"cap.package": "C_0805_2012Metric"}))
    assert r.package.value == "0805"


def test_micro_sign_values():
    assert _req("4.7µF").nominal == pytest.approx(4.7e-6)
    assert _req("4µ7").nominal == pytest.approx(4.7e-6)
    assert _req("10μF").dielectrics.value == ("X7R", "X5R")


def test_zero_and_empty_value_not_searchable():
    z = _req("0", **{"Voltage:Min": "16V"})
    assert not z.searchable and z.dielectrics.source == UNSET
    e = _req("", **{"Voltage:Min": "16V"})
    assert e.nominal is None
    assert e.problem() == "no Value"


def test_whitespace_footprint_treated_as_empty():
    r = _req(footprint="   ", **{"Package:Preferred": "0805"})
    assert r.footprint == ""
    assert (r.package.value, r.package.source) == ("0805", SCHEMATIC)
    assert r.edited(package="0402").package.value == "0402"


def test_edited_normalizes_package_and_dielectrics():
    e = _req().edited(package=" 0805 ", dielectrics=["X7R, x5r", "X7R"])
    assert e.package.value == "0805"
    assert e.dielectrics.value == ("X7R", "X5R")
    assert e.edited(dielectrics=[]).dielectrics == Sourced((), EDITED)


def test_voltage_never_from_net_names():
    # Rail-like text elsewhere on the symbol does not provide a voltage floor.
    r = _req(**{"Net": "+3V3", "Description": "decoupling on 5V rail"})
    assert r.rating_min.source == UNSET


def test_assigned_with_whitespace_only_fields_is_false():
    props = {**FULL, "LCSC": "   "}
    assert not group_parts([_record("C1", **props)], DEFAULTS)[0].assigned
    props = {**FULL, "Footprint": "  "}
    assert not group_parts([_record("C1", **props)], DEFAULTS)[0].assigned


def test_hierarchical_shared_uuid_members_kept_separately():
    a = PartRecord("C1", {"Reference": "C1", "Intent:Type": "capacitor", "Value": "1uF"},
                   Path("sub.kicad_sch"), "same-uuid",
                   ElectricalIntent.from_dict({"Reference": "C1", "dnp": False, "in_bom": True,
                                               "Intent:Type": "capacitor", "Value": "1uF"}))
    b = PartRecord("C2", {"Reference": "C2", "Intent:Type": "capacitor", "Value": "1uF"},
                   Path("sub.kicad_sch"), "same-uuid",
                   ElectricalIntent.from_dict({"Reference": "C2", "dnp": False, "in_bom": True,
                                               "Intent:Type": "capacitor", "Value": "1uF"}))
    (g,) = group_parts([b, a], DEFAULTS)
    assert g.references == ["C1", "C2"]
    assert g.quantity == 2
    assert {m.symbol_uuid for m in g.members} == {"same-uuid"}


def test_group_order_natural_across_groups():
    groups = group_parts(
        [_record("C10"), _record("C2", Value="1uF"), _record("C1", Value="10nF")],
        DEFAULTS,
    )
    assert [g.references[0] for g in groups] == ["C1", "C2", "C10"]


# --------------------------------------------------------------------------- #
# Revision 2: library-style Values, description and existing-part evidence
# --------------------------------------------------------------------------- #

def _ev(text):
    from kicadtools.units import parse_cap_description
    return parse_cap_description(text)


def _rq(value="100nF", footprint="", *, description=None, part=None,
        defaults=DEFAULTS, **props):
    return part_requirement(_cap(value, **props), footprint, defaults,
                                 description=description, part=part)


PART_10U = PartEvidence(label="current part 1276-3388-1-ND (DigiKey)",
                        nominal=10e-6, rating=50.0,
                        dielectrics=("X5R",), tolerance_pct=10.0,
                        package="1210")


def test_defaults_for_new_fields():
    r = _req()
    assert r.nominal_source == SCHEMATIC
    assert r.conflicts == ()


@pytest.mark.parametrize("value,farads,package", [
    ("C_4p7U", 4.7e-6, None),
    ("C_P1U", 0.1e-6, None),
    ("C_1U", 1e-6, None),
    ("C_10U_805", 10e-6, "0805"),
    ("C_22U_1210", 22e-6, "1210"),
    ("C_10U_1210", 10e-6, "1210"),
])
def test_library_style_values(value, farads, package):
    r = _rq(value)
    assert r.nominal == pytest.approx(farads)
    assert r.nominal_source == SCHEMATIC
    if package:
        assert r.package == Sourced(package, SCHEMATIC)
    else:
        assert r.package == Sourced("0603", DEFAULT)
    assert r.conflicts == ()


def test_value_suffix_below_footprint_and_package_preferred():
    # agreeing sources: the higher one is used, no conflict
    r = _rq("C_10U_805", "Capacitor_SMD:C_0805_2012Metric")
    assert r.package == Sourced("0805", SCHEMATIC) and r.conflicts == ()
    r = _rq("C_10U_805", **{"Package:Preferred": "0805"})
    assert r.package == Sourced("0805", SCHEMATIC) and r.conflicts == ()


def test_hand_built_library_value_with_description():
    rec = _record("C1", Value="C_10U_805",
                  Description="10uF, 25V, X5R, 805 package")
    (g,) = group_parts([rec], DEFAULTS)
    r = g.requirement
    assert r.nominal == pytest.approx(10e-6)
    assert r.nominal_source == SCHEMATIC
    assert r.package == Sourced("0805", SCHEMATIC)
    assert r.rating_min == Sourced(25.0, DESCRIPTION)
    assert r.dielectrics == Sourced(("X5R",), DESCRIPTION)
    assert r.conflicts == () and r.searchable


def test_description_only_fields():
    r = _rq("", description=_ev("22uF, X5R, 25V, 1210"))
    assert r.nominal == pytest.approx(22e-6)
    assert r.nominal_source == DESCRIPTION
    assert r.package == Sourced("1210", DESCRIPTION)
    assert r.rating_min == Sourced(25.0, DESCRIPTION)
    assert r.dielectrics == Sourced(("X5R",), DESCRIPTION)
    assert r.tolerance_max_pct == Sourced(10.0, DEFAULT)
    assert r.searchable


def test_description_voltage_range_takes_lowest():
    r = _rq("1uF", description=_ev("1uF, 25V - 50V"))
    assert r.rating_min == Sourced(25.0, DESCRIPTION)
    assert r.dielectrics.source == DEFAULT


def test_generic_description_is_no_evidence():
    for text in ("Unpolarized capacitor, small symbol",
                 "Polarized capacitor, small US symbol", ""):
        r = _rq("100nF", description=_ev(text))
        assert r == _req("100nF"), text


def test_part_only_fields():
    r = _rq("", part=PART_10U)
    assert r.nominal == pytest.approx(10e-6)
    assert r.nominal_source == PART
    assert r.package == Sourced("1210", PART)
    assert r.rating_min == Sourced(50.0, PART)
    assert r.dielectrics == Sourced(("X5R",), PART)
    assert r.tolerance_max_pct == Sourced(10.0, PART)
    assert r.searchable


def test_part_package_normalized():
    part = PartEvidence(label="p", package="0402 (1005 Metric)")
    assert _rq(part=part).package == Sourced("0402", PART)


def test_empty_value_without_evidence_is_unset():
    r = _rq("")
    assert r.nominal is None and r.nominal_source == UNSET
    assert r.problem() == "no Value"


def test_value_beats_part_beats_description_for_capacitance():
    desc = DescriptionEvidence(nominal=10e-6)
    part = PartEvidence(label="p", nominal=10e-6)
    assert _rq("10uF", description=desc, part=part).nominal_source == SCHEMATIC
    assert _rq("", description=desc, part=part).nominal_source == PART
    assert _rq("", description=desc).nominal_source == DESCRIPTION


def test_part_beats_description_for_voltage_dielectric_tolerance():
    desc = _ev("10uF, 25V, X6S, 1210")
    r = _rq("10uF", description=desc, part=PART_10U)
    assert r.rating_min == Sourced(50.0, PART)
    assert r.dielectrics == Sourced(("X5R",), PART)
    assert r.tolerance_max_pct == Sourced(10.0, PART)
    # part beats description for package
    assert r.package == Sourced("1210", PART)


def test_schematic_beats_part_and_description():
    r = _rq("10uF", "Capacitor_SMD:C_1210_3225Metric",
            description=_ev("10uF, 25V, X6S, 1210"), part=PART_10U,
            **{"Voltage:Min": "35V", "Class:Preferred": "X7S",
               "Tolerance:Preferred": "20%"})
    assert r.package == Sourced("1210", SCHEMATIC)
    assert r.rating_min == Sourced(35.0, SCHEMATIC)
    assert r.dielectrics == Sourced(("X7S",), SCHEMATIC)
    assert r.tolerance_max_pct == Sourced(20.0, SCHEMATIC)


def test_package_preferred_beats_value_suffix_and_evidence():
    r = _rq("C_10U_1210", description=_ev("10uF 1210"), part=PART_10U,
            **{"Package:Preferred": "1210"})
    assert r.package == Sourced("1210", SCHEMATIC)
    assert r.conflicts == ()


def test_remembered_voltage_below_evidence():
    d = part_defaults({"cap.voltage_min": "16V"})
    assert _rq(defaults=d, description=_ev("0.1uF, 25V")).rating_min == \
        Sourced(25.0, DESCRIPTION)
    assert _rq(defaults=d).rating_min == Sourced(16.0, DEFAULT)


@pytest.mark.parametrize("code,kept,allowed", [
    ("X5R", ("X5R",), ("X5R", "X7R", "X8R")),
    ("X6S", ("X6S",), ("X6S", "X7R", "X8R")),
    ("X7S", ("X7S",), ("X7S", "X7R", "X8R")),
    ("X7R", ("X7R",), ("X7R", "X8R")),
    ("C0G", ("C0G",), ("C0G", "NP0")),
    ("NP0", ("NP0",), ("NP0", "C0G")),
    ("NPO", ("NP0",), ("NP0", "C0G")),
    ("Y5V", ("Y5V",), ("Y5V",)),
])
def test_dielectric_kept_as_read_and_upgraded_when_matching(code, kept, allowed):
    """The field keeps the code as read from the part or the Description; a
    better class meets it when matching (allowed_classes)."""
    from kicadtools.requirement import allowed_classes
    p = _rq(part=PartEvidence(label="p", dielectrics=(code,)))
    assert p.dielectrics == Sourced(kept, PART)
    d = _rq(description=_ev(f"0.1uF {code}"))
    assert d.dielectrics == Sourced(kept, DESCRIPTION)
    assert allowed_classes(p.ptype, p.dielectrics.value) == allowed


def test_stated_and_default_classes_are_upgraded_when_matching():
    from kicadtools.parttypes import CAPACITOR, INDUCTOR
    from kicadtools.requirement import allowed_classes
    assert allowed_classes(CAPACITOR, ("X5R",)) == ("X5R", "X7R", "X8R")
    assert allowed_classes(CAPACITOR, ("X7R", "X5R")) == ("X7R", "X8R", "X5R")
    assert allowed_classes(INDUCTOR, ("Semi-Shielded",)) == ("Semi-Shielded", "Shielded")


def test_multiple_dielectrics_expanded_in_order_without_repeats():
    r = _rq(part=PartEvidence(label="p", dielectrics=("X5R", "X7R", "X6S")))
    assert r.dielectrics.value == ("X5R", "X7R", "X6S")


def test_class1_from_part_takes_class1_tolerance_default():
    r = _rq("100pF", part=PartEvidence(label="p", dielectrics=("C0G",)))
    assert r.tolerance_max_pct == Sourced(5.0, DEFAULT)


def test_band_default_uses_capacitance_from_description():
    r = _rq("", description=DescriptionEvidence(nominal=22e-6))
    assert r.dielectrics == Sourced(("X7R", "X5R"), DEFAULT)


# ---- conflicts ------------------------------------------------------------ #

def test_description_disagreeing_with_value_is_a_warning():
    r = _rq("10nF", description=_ev("10uF, 50V, X7R, 1210 package"))
    assert r.nominal_source == SCHEMATIC
    assert r.nominal == pytest.approx(10e-9)
    assert r.conflicts == ()
    assert r.warnings == ("Description says 10uF; Value says 10nF (Description not used)",)
    # the Description still fills the open fields
    assert r.package == Sourced("1210", DESCRIPTION)
    assert r.rating_min == Sourced(50.0, DESCRIPTION)


def test_description_fully_covered_by_part_is_not_used():
    desc = _ev("0.1uF, 25V, X7R 603")
    part = PartEvidence(label="p", nominal=100e-9, rating=16.0,
                        dielectrics=("X5R",), tolerance_pct=10.0, package="0402")
    r = _rq("0.1uF", "Capacitor_SMD:C_0402_1005Metric", description=desc, part=part)
    assert r.conflicts == ()
    assert r.warnings == ("Description says 0603; Footprint says 0402 (Description not used)",)
    assert DESCRIPTION not in (r.package.source, r.rating_min.source,
                               r.dielectrics.source, r.nominal_source)


def test_capacitance_conflict_value_vs_part_and_description_vs_part():
    part = PartEvidence(label="current part 1276-3388-1-ND (DigiKey)",
                        nominal=10e-6)
    r = _rq("10nF", description=DescriptionEvidence(nominal=22e-6),
            part=part)
    assert r.conflicts == (
        "Value says 10nF; current part 1276-3388-1-ND (DigiKey) says 10uF",
    )
    assert r.warnings == (
        "Description says 22uF; Value says 10nF (Description not used)",
    )
    assert r.problem() == r.conflicts[0]


def test_capacitance_within_tolerance_is_not_a_conflict():
    # "0.1uF" and "100nF" parse with float error
    r = _rq("0.1uF", description=_ev("100nF"),
            part=PartEvidence(label="p", nominal=1.0005e-7))
    assert r.conflicts == ()
    r = _rq("0.1uF", part=PartEvidence(label="p", nominal=1.002e-7))
    assert len(r.conflicts) == 1


def test_package_conflict_footprint_vs_part():
    part = PartEvidence(label="current part X (DigiKey)", package="0402")
    r = _rq("100nF", "Capacitor_SMD:C_0603_1608Metric", part=part)
    assert r.package == Sourced("0603", SCHEMATIC)
    assert r.conflicts == ("Footprint says 0603; current part X (DigiKey) says 0402",)
    assert not r.searchable and r.problem() == r.conflicts[0]


def test_package_conflicts_between_schematic_sources():
    r = _rq("C_10U_805", "Capacitor_SMD:C_1206_3216Metric",
            description=_ev("10uF, 805 package"))
    assert r.conflicts == ("Footprint says 1206; Value says 0805",)
    assert r.warnings == (
        "Description says 0805; Footprint says 1206 (Description not used)",)
    r = _rq("100nF", **{"Package:Preferred": "0402"},
            description=_ev("0.1uF, 0603"))
    assert r.conflicts == ()
    assert len(r.warnings) == 1


def test_unparseable_stated_values_are_not_conflicts():
    # a radial footprint states no chip size; a non-capacitance Value
    # keeps (SCHEMATIC, None)
    r = _rq("47uF", "Capacitor_THT:CP_Radial_D6.3mm_P2.50mm",
            part=PartEvidence(label="p", package="0805"))
    assert r.conflicts == () and r.package == Sourced(None, SCHEMATIC)
    r = _rq("tbd", description=_ev("10uF"))
    assert (r.nominal, r.nominal_source) == (None, SCHEMATIC)
    assert r.conflicts == ()
    assert r.problem() == "Value 'tbd' is not a capacitance"
    r = _rq(**{"Package:Preferred": "big"}, description=_ev("0.1uF 0603"))
    assert r.conflicts == ()


def test_conflicts_extend_with_replace():
    r = replace(_rq(), conflicts=("MPN and DigiKeyPN name different parts",))
    assert r.problem() == "MPN and DigiKeyPN name different parts"


# ---- Member / RequirementGroup evidence ----------------------------------- #

def test_member_existing_swapped_and_aliased_fields_as_written():
    # MotorDriver C35 / BuckConverter C9: a DigiKey PN in the MPN field and an
    # MPN in the DigiKeyPN field stay where they are written.
    rec = _record("C9", Value="10nF", MPN="1276-3388-1-ND",
                  **{"Digikey PN": "CL32B106KBJNNWE", "JLCPCB": "C13585",
                     "Mfr": ""})
    (g,) = group_parts([rec], DEFAULTS)
    assert g.members[0].existing == ExistingPart(
        mpn="1276-3388-1-ND", digikey_pn="CL32B106KBJNNWE", lcsc="C13585",
        manufacturer=None)
    assert g.existing == g.members[0].existing
    assert not g.existing.empty


def test_member_existing_empty():
    (g,) = group_parts([_record("C1", MPN="  ", LCSC="")], DEFAULTS)
    assert g.existing == ExistingPart()
    assert g.existing.empty


def test_member_and_group_description():
    (g,) = group_parts(
        [_record("C1", Value="4.7uF", Description="4.7uF, 16V, X6S 603")],
        DEFAULTS)
    assert g.description == DescriptionEvidence(
        nominal=pytest.approx(4.7e-6), rating=16.0,
        dielectrics=("X6S",), package="0603")
    assert g.requirement.dielectrics == Sourced(("X6S",), DESCRIPTION)


def test_requirement_with_part():
    rec = _record("C9", Value="10nF", Description="10uF, 50V, X7R, 1210 package",
                  MPN="1276-3388-1-ND")
    (g,) = group_parts([rec], DEFAULTS)
    assert g.requirement.conflicts == ()
    assert g.requirement.warnings == (
        "Description says 10uF; Value says 10nF (Description not used)",)
    part = PartEvidence(label="current part 1276-3388-1-ND (DigiKey)",
                        nominal=10e-6, rating=50.0,
                        dielectrics=("X7R",), package="1210")
    r = g.requirement_with_part(part, DEFAULTS)
    assert r.conflicts == (
        "Value says 10nF; current part 1276-3388-1-ND (DigiKey) says 10uF",
    )
    assert r.rating_min == Sourced(50.0, PART)
    assert r.package == Sourced("1210", PART)
    # None part: same as the grouped requirement; new defaults apply
    assert g.requirement_with_part(None, DEFAULTS) == g.requirement
    d = part_defaults({"cap.voltage_min": "16V"})
    plain = group_parts([_record("C1")], DEFAULTS)[0]
    assert plain.requirement_with_part(None, d).rating_min == Sourced(16.0, DEFAULT)


def test_requirement_with_part_uses_footprint():
    rec = _record("C1", Footprint="Capacitor_SMD:C_0603_1608Metric")
    (g,) = group_parts([rec], DEFAULTS)
    r = g.requirement_with_part(PartEvidence(label="p", package="0402"), DEFAULTS)
    assert r.footprint == "Capacitor_SMD:C_0603_1608Metric"
    assert r.conflicts == ("Footprint says 0603; p says 0402",)


def test_group_key_splits_on_existing_part_numbers_and_description():
    groups = group_parts([
        _record("C1"),
        _record("C2", MPN="CL10B104KB8NNNC"),
        _record("C3", MPN="cl10b104kb8nnnc"),          # same, case folded
        _record("C4", DigiKeyPN="1276-1000-1-ND"),
        _record("C5", **{"Digi-Key_PN": "1276-1000-1-nd"}),  # alias spelling
        _record("C6", LCSC="C1591"),
        _record("C7", JLCPCB="c1591"),
        _record("C8", Description="0.1uF, 25V, X7R 603"),
        _record("C9", Description=" 0.1uF, 25V, X7R 603 "),
        _record("C10", Description="Unpolarized capacitor, small symbol"),
        _record("C11", Description="0.1uF, 50V, X7R 603"),       # other voltage
        _record("C12", Description="100nF, 25V, X7R, 0603 package"),
    ], DEFAULTS)
    # boilerplate states nothing (C10 joins C1); only the open fields of a
    # Description count (C12 states C8's values in other words)
    assert sorted(g.references for g in groups) == sorted([
        ["C1", "C10"], ["C2", "C3"], ["C4", "C5"], ["C6", "C7"],
        ["C8", "C9", "C12"], ["C11"],
    ])


def test_group_key_ignores_description_fields_stated_elsewhere():
    fp = "Capacitor_SMD:C_0402_1005Metric"
    groups = group_parts([
        _record("C1", Footprint=fp, **{"Voltage:Min": "16V", "Class:Preferred": "X7R"}),
        _record("C2", Footprint=fp, Description="0.1uF, 25V, X7R 603",
                **{"Voltage:Min": "16V", "Class:Preferred": "X7R"}),
    ], DEFAULTS)
    assert [g.references for g in groups] == [["C1", "C2"]]


def test_group_key_ignores_description_when_part_number_present():
    # MotorDriver C30 / C34: same DigiKey and LCSC numbers, different Descriptions
    groups = group_parts([
        _record("C30", Value="1uF", DigiKeyPN="1276-1445-1-ND", LCSC="C52923",
                Description="1uF, 25V - 50V"),
        _record("C34", Value="1uF", DigiKeyPN="1276-1445-1-ND", LCSC="C52923",
                Description="1uF, 25V, X7R 402"),
        _record("C40", Value="1uF", Description="1uF, 25V - 50V"),
        _record("C41", Value="1uF", Description="1uF, 25V, X7R 402"),
    ], DEFAULTS)
    assert sorted(g.references for g in groups) == [["C30", "C34"], ["C40"], ["C41"]]


def test_ecg_existing_part_and_generic_description():
    _, groups = _groups("ECG.kicad_sch")
    for g in groups:
        assert g.requirement.conflicts == ()
        assert g.requirement.nominal_source == SCHEMATIC


# ---- Review: shorthand sizes and the real board values -------------------- #

def test_three_digit_package_preferred_normalized_and_compared():
    r = _rq("100nF", **{"Package:Preferred": "402", "Voltage:Min": "16V"})
    assert r.package == Sourced("0402", SCHEMATIC)
    assert r.searchable
    r = _rq("100nF", "Capacitor_SMD:C_0402_1005Metric",
            **{"Package:Preferred": "402"})
    assert r.conflicts == ()
    r = _rq("100nF", "Capacitor_SMD:C_0603_1608Metric",
            **{"Package:Preferred": "402"})
    assert r.conflicts == ("Footprint says 0603; Package:Preferred says 0402",)


def test_three_digit_package_from_part_setting_and_edit():
    r = _rq("100nF", part=PartEvidence(label="p", package="805"))
    assert r.package == Sourced("0805", PART)
    r = _rq("100nF", defaults=part_defaults({"cap.package": "402"}))
    assert r.package == Sourced("0402", DEFAULT)
    assert _rq("100nF").edited(package="805").package == Sourced("0805", EDITED)


def test_same_capacitance_in_other_units_is_not_a_conflict():
    part = PartEvidence(label="p", nominal=100e-9, package="0603")
    r = _rq("0.1uF", description=_ev("100nF, 25V, X7R, 0603"), part=part)
    assert r.conflicts == ()
    r = _rq("C_P1U", description=_ev("0.1uF, 25V, X7R 603"), part=part)
    assert r.conflicts == ()


# Each real Value / Description pair from the plan's board table, without a
# footprint: (value, description, capacitance, cap source, package, voltage,
# dielectrics, conflicts).
@pytest.mark.parametrize("value,desc,farads,pkg,volts,diel,conflicts", [
    ("C_4p7U", "4.7uF, 16V, X6S 603", 4.7e-6, Sourced("0603", DESCRIPTION),
     Sourced(16.0, DESCRIPTION), Sourced(("X6S",), DESCRIPTION), ()),
    ("C_10U_805", "10uF, 25V, X5R, 805 package", 10e-6,
     Sourced("0805", SCHEMATIC), Sourced(25.0, DESCRIPTION),
     Sourced(("X5R",), DESCRIPTION), ()),
    ("100nF", "Unpolarized capacitor, small symbol", 100e-9,
     Sourced("0603", DEFAULT), Sourced(None, UNSET), Sourced(("X7R",), DEFAULT), ()),
    ("10uF", "Polarized capacitor, small US symbol", 10e-6,
     Sourced("0603", DEFAULT), Sourced(None, UNSET),
     Sourced(("X7R", "X5R"), DEFAULT), ()),
    ("0.1uF", "0.1uF, 25V, X7R 603", 0.1e-6, Sourced("0603", DESCRIPTION),
     Sourced(25.0, DESCRIPTION), Sourced(("X7R",), DESCRIPTION), ()),
    ("10nF", "10uF, 50V, X7R, 1210 package", 10e-9,
     Sourced("1210", DESCRIPTION), Sourced(50.0, DESCRIPTION),
     Sourced(("X7R",), DESCRIPTION), ()),
    ("1uF", "1uF, 25V - 50V", 1e-6, Sourced("0603", DEFAULT),
     Sourced(25.0, DESCRIPTION), Sourced(("X7R",), DEFAULT), ()),
    ("C_22U_1210", "22uF, X5R, 25V, 1210", 22e-6, Sourced("1210", SCHEMATIC),
     Sourced(25.0, DESCRIPTION), Sourced(("X5R",), DESCRIPTION), ()),
    ("C_10U_1210", "10uF, 50V, X7R, 1210 package", 10e-6,
     Sourced("1210", SCHEMATIC), Sourced(50.0, DESCRIPTION),
     Sourced(("X7R",), DESCRIPTION), ()),
    ("C_1U", "1uF, 25V - 50V", 1e-6, Sourced("0603", DEFAULT),
     Sourced(25.0, DESCRIPTION), Sourced(("X7R",), DEFAULT), ()),
    ("C_P1U", "0.1uF, 25V, X7R, 0603", 0.1e-6, Sourced("0603", DESCRIPTION),
     Sourced(25.0, DESCRIPTION), Sourced(("X7R",), DESCRIPTION), ()),
    ("47uF", "", 47e-6, Sourced("0603", DEFAULT), Sourced(None, UNSET),
     Sourced(("X7R", "X5R"), DEFAULT), ()),
])
def test_real_board_values_and_descriptions(value, desc, farads, pkg, volts,
                                            diel, conflicts):
    r = _rq(value, description=_ev(desc))
    assert r.nominal == pytest.approx(farads)
    assert r.nominal_source == SCHEMATIC
    assert (r.package, r.rating_min, r.dielectrics) == (pkg, volts, diel)
    assert r.conflicts == conflicts


def test_buckconverter_c9_value_part_and_description_conflicts():
    part = PartEvidence(label="current part 1276-3388-1-ND (DigiKey)",
                        nominal=10e-6, rating=50.0,
                        dielectrics=("X7R",), tolerance_pct=10.0,
                        package="1210 (3225 Metric)")
    r = _rq("10nF", description=_ev("10uF, 50V, X7R, 1210 package"), part=part)
    assert r.conflicts == (
        "Value says 10nF; current part 1276-3388-1-ND (DigiKey) says 10uF",
    )
    assert r.warnings == (
        "Description says 10uF; Value says 10nF (Description not used)",)
    assert r.problem() == r.conflicts[0]
    # a stated Value is never replaced by the evidence
    assert (r.nominal, r.nominal_source) == (10e-9, SCHEMATIC)


def test_stated_schematic_values_never_replaced_by_evidence():
    part = PartEvidence(label="p", nominal=1e-6, rating=100.0,
                        dielectrics=("C0G",), tolerance_pct=1.0, package="0805")
    r = _rq("C_1U_805", **{"Voltage:Min": "tbd", "Class:Preferred": "X7R",
                           "Tolerance:Preferred": "±0.25pF"},
            description=_ev("1uF, 50V, X5R, 805"), part=part)
    assert r.conflicts == ()
    assert r.package == Sourced("0805", SCHEMATIC)
    assert r.rating_min == Sourced(None, SCHEMATIC)
    assert r.dielectrics == Sourced(("X7R",), SCHEMATIC)
    assert r.tolerance_max_pct == Sourced(None, SCHEMATIC)


# --- stated fields grouped by meaning (2026-11-04) -------------------------------

FP0402 = "Capacitor_SMD:C_0402_1005Metric"


def test_class_written_with_and_without_upgrades_is_one_group():
    """MotorDriver C13 (`X5R, X7R`, written before revision 6) and C30
    (`X5R`, written after) both mean X5R or better."""
    a = _record("C13", Footprint=FP0402, **{"Class:Preferred": "X5R, X7R"})
    b = _record("C30", Footprint=FP0402, **{"Class:Preferred": "X5R"})
    c = _record("C31", Footprint=FP0402, **{"Class:Preferred": "X7R"})
    groups = group_parts([a, b, c], DEFAULTS)
    assert [g.references for g in groups] == [["C13", "C30"], ["C31"]]


def test_rating_and_tolerance_grouped_by_value_not_text():
    a = _record("C1", Footprint=FP0402, **{"Voltage:Min": "25V", "Tolerance:Preferred": "10%"})
    b = _record("C2", Footprint=FP0402, **{"Voltage:Min": "25 V", "Tolerance:Preferred": "10 %"})
    c = _record("C3", Footprint=FP0402, **{"Voltage:Min": "16V", "Tolerance:Preferred": "10%"})
    groups = group_parts([a, b, c], DEFAULTS)
    assert [g.references for g in groups] == [["C1", "C2"], ["C3"]]


def test_unparsed_stated_text_still_compared_as_written():
    a = _record("C1", Footprint=FP0402, **{"Voltage:Min": "see note"})
    b = _record("C2", Footprint=FP0402, **{"Voltage:Min": "see notes"})
    assert len(group_parts([a, b], DEFAULTS)) == 2

