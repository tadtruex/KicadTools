import difflib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import sexpdata

from kicadtools.assign import FOOTPRINTS, assignment_plan
from kicadtools.intent import CapacitorIntent
from kicadtools.evidence import PartEvidence
from kicadtools.requirement import (
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    RELAXED,
    SCHEMATIC,
    UNSET,
    CapacitorRequirement,
    Member,
    RequirementGroup,
    Sourced,
    capacitor_defaults,
    capacitor_requirement,
    group_capacitors,
)
from kicadtools.resolve import iter_bom_part_records
from kicadtools.writer import _find_node_end, write_properties

DATA = Path(__file__).parent / "data"
ECG = DATA / "ECG.kicad_sch"
SRC = Path("/board/Board.kicad_sch")

CAND = SimpleNamespace(
    mpn="CL10B104KB8NNNC",
    manufacturer="Samsung Electro-Mechanics",
    digikey_pn="1276-1000-1-ND",
    lcsc="C1591",
    package="0603",
)


def _req(*, package=Sourced("0603", SCHEMATIC),
         voltage=Sourced(16.0, SCHEMATIC),
         dielectrics=Sourced(("X7R",), SCHEMATIC),
         tolerance=Sourced(10.0, SCHEMATIC),
         footprint="Capacitor_SMD:C_0603_1608Metric"):
    return CapacitorRequirement(
        value_text="100nF",
        capacitance_f=100e-9,
        package=package,
        voltage_min_v=voltage,
        dielectrics=dielectrics,
        tolerance_max_pct=tolerance,
        footprint=footprint,
    )


def _member(ref="C1", uuid="u1", *, guessed=False, source=SRC, **props):
    base = {"Reference": ref, "Value": "100nF",
            "Footprint": "Capacitor_SMD:C_0603_1608Metric"}
    base.update(props)
    intent = CapacitorIntent(reference=ref, value="100nF", dnp=False, in_bom=True)
    intent.intent_guessed = guessed
    return Member(ref, source, uuid, base, intent)


def _plan(members, req):
    return assignment_plan(RequirementGroup(req, members), req, CAND)


def test_explicit_type_and_stated_fields_write_only_the_part():
    plan = _plan([_member()], _req())
    assert plan == {SRC: {"u1": {
        "MPN": "CL10B104KB8NNNC",
        "Manufacturer": "Samsung Electro-Mechanics",
        "DigiKeyPN": "1276-1000-1-ND",
        "LCSC": "C1591",
    }}}


def test_guessed_type_writes_intent_type():
    props = _plan([_member(guessed=True)], _req())[SRC]["u1"]
    assert props["Intent:Type"] == "capacitor"


def test_default_fields_are_written_in_terse_form():
    req = _req(voltage=Sourced(16.0, DEFAULT),
               dielectrics=Sourced(("X7R", "X5R"), DEFAULT),
               tolerance=Sourced(10.0, DEFAULT))
    props = _plan([_member()], req)[SRC]["u1"]
    assert props["Voltage:Min"] == "16V"
    assert props["Class:Preferred"] == "X7R, X5R"
    assert props["Tolerance:Preferred"] == "10%"


def test_edited_fields_are_written():
    req = _req(footprint="", package=Sourced("0603", DEFAULT)).edited(
        voltage_min_v=6.3, dielectrics=["C0G"], tolerance_max_pct=5.0,
    )
    props = _plan([_member(Footprint="")], req)[SRC]["u1"]
    assert props["Voltage:Min"] == "6.3V"
    assert props["Class:Preferred"] == "C0G"
    assert props["Tolerance:Preferred"] == "5%"


def test_stated_and_unset_fields_are_not_written():
    req = _req(voltage=Sourced(None, SCHEMATIC),
               tolerance=Sourced(None, UNSET))
    props = _plan([_member()], req)[SRC]["u1"]
    for key in ("Voltage:Min", "Class:Preferred", "Tolerance:Preferred"):
        assert key not in props


def test_present_footprint_is_left_unchanged():
    props = _plan([_member()], _req())[SRC]["u1"]
    assert "Footprint" not in props


def test_empty_footprint_is_filled_from_the_package():
    req = _req(footprint="", package=Sourced("0805", DEFAULT))
    props = _plan([_member(Footprint="")], req)[SRC]["u1"]
    assert props["Footprint"] == FOOTPRINTS["0805"]
    assert "Package:Preferred" not in props  # default package: not written


def test_missing_footprint_property_is_filled():
    m = _member()
    del m.props["Footprint"]
    req = _req(footprint="", package=Sourced("0402", SCHEMATIC))
    assert _plan([m], req)[SRC]["u1"]["Footprint"] == FOOTPRINTS["0402"]


def test_edited_package_without_footprint_writes_package_preferred():
    req = _req(footprint="", package=Sourced("0603", DEFAULT)).edited(
        package="1206")
    props = _plan([_member(Footprint="")], req)[SRC]["u1"]
    assert props["Package:Preferred"] == "1206"
    assert props["Footprint"] == FOOTPRINTS["1206"]


def test_edited_package_with_member_footprint_skips_package_preferred():
    # The requirement was built without a footprint, but this member has one.
    req = _req(footprint="", package=Sourced("0603", DEFAULT)).edited(
        package="0805")
    props = _plan([_member()], req)[SRC]["u1"]
    assert "Package:Preferred" not in props
    assert "Footprint" not in props


def test_alias_spellings_are_removed():
    m = _member(**{"Digikey PN": "", "Manufacturer_PN": "OLD-MPN",
                   "DigikeyPN": "1276-9999-1-ND", "LCSC": "", "Sim.Pins": ""})
    props = _plan([m], _req())[SRC]["u1"]
    assert props["Digikey PN"] is None
    assert props["Manufacturer_PN"] is None
    assert props["DigikeyPN"] is None
    assert props["LCSC"] == "C1591"  # canonical key: set, not removed
    assert props["DigiKeyPN"] == "1276-1000-1-ND"
    assert "Sim.Pins" not in props  # not a part-number field


def test_unknown_package_without_footprint_raises():
    req = _req(footprint="", package=Sourced("2220", DEFAULT))
    with pytest.raises(ValueError, match="2220"):
        _plan([_member(Footprint="")], req)


def test_unknown_package_with_footprint_is_fine():
    req = _req(package=Sourced(None, SCHEMATIC),
               footprint="Capacitor_THT:CP_Radial_D5.0mm_P2.00mm")
    m = _member(Footprint="Capacitor_THT:CP_Radial_D5.0mm_P2.00mm")
    assert "Footprint" not in _plan([m], req)[SRC]["u1"]


def test_members_without_provenance_are_skipped():
    members = [_member("C1", "u1"), _member("C2", None),
               _member("C3", "u3", source=None)]
    plan = _plan(members, _req())
    assert list(plan) == [SRC]
    assert list(plan[SRC]) == ["u1"]


def test_members_are_grouped_by_file():
    other = Path("/board/Sub.kicad_sch")
    plan = _plan([_member("C1", "u1"), _member("C2", "u2", source=other),
                  _member("C3", "u3")], _req())
    assert set(plan) == {SRC, other}
    assert set(plan[SRC]) == {"u1", "u3"}
    assert set(plan[other]) == {"u2"}


def test_plan_reads_only_the_documented_candidate_attributes():
    class Strict:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def __getattr__(self, name):
            raise AssertionError(f"read {name}")

    cand = Strict(mpn="M", manufacturer="X", digikey_pn="D", lcsc="C1",
                  package="0603")
    req = _req()
    plan = assignment_plan(RequirementGroup(req, [_member()]), req, cand)
    assert plan[SRC]["u1"]["MPN"] == "M"


def test_hierarchical_instances_with_one_uuid_give_one_entry():
    # A subsheet placed twice: two references, one symbol in one file.
    sub = Path("/board/Sub.kicad_sch")
    members = [_member("C1", "u1", source=sub, guessed=True),
               _member("C101", "u1", source=sub, guessed=True)]
    plan = _plan(members, _req())
    assert list(plan) == [sub]
    assert list(plan[sub]) == ["u1"]
    assert plan[sub]["u1"]["Intent:Type"] == "capacitor"


@pytest.mark.parametrize("attr", ["mpn", "manufacturer", "digikey_pn", "lcsc"])
@pytest.mark.parametrize("empty", [None, "", "  "])
def test_candidate_without_a_part_number_raises(attr, empty):
    cand = SimpleNamespace(**{**vars(CAND), attr: empty})
    req = _req()
    with pytest.raises(ValueError, match="required"):
        assignment_plan(RequirementGroup(req, [_member()]), req, cand)


def test_candidate_values_are_stripped():
    cand = SimpleNamespace(**{**vars(CAND), "mpn": " CL10B104KB8NNNC\n",
                              "lcsc": "C1591 "})
    req = _req()
    props = assignment_plan(RequirementGroup(req, [_member()]), req, cand)[SRC]["u1"]
    assert props["MPN"] == "CL10B104KB8NNNC"
    assert props["LCSC"] == "C1591"


def test_fractional_values_are_written_without_trailing_zeros():
    req = _req(voltage=Sourced(6.3, DEFAULT), tolerance=Sourced(2.5, DEFAULT))
    props = _plan([_member()], req)[SRC]["u1"]
    assert props["Voltage:Min"] == "6.3V"
    assert props["Tolerance:Preferred"] == "2.5%"


def test_dielectrics_are_written_normalized():
    req = _req(dielectrics=Sourced(("cog", "NP0", "C0G"), DEFAULT))
    props = _plan([_member()], req)[SRC]["u1"]
    assert props["Class:Preferred"] == "C0G, NP0"


def test_stated_values_are_never_written_even_when_defaults_differ():
    # All three requirement fields stated on the schematic: none is written.
    req = _req(voltage=Sourced(50.0, SCHEMATIC),
               dielectrics=Sourced(("C0G",), SCHEMATIC),
               tolerance=Sourced(1.0, SCHEMATIC))
    props = _plan([_member(**{"Voltage:Min": "50V"})], req)[SRC]["u1"]
    assert set(props) == {"MPN", "Manufacturer", "DigiKeyPN", "LCSC"}


def test_whitespace_footprint_counts_as_empty():
    req = _req(footprint="", package=Sourced("0603", DEFAULT))
    props = _plan([_member(Footprint="  ")], req)[SRC]["u1"]
    assert props["Footprint"] == FOOTPRINTS["0603"]


def test_case_and_spacing_variants_of_canonical_keys_are_removed():
    m = _member(**{"lcsc": "C9", "MPN ": "", "Mfr": "Old Co"})
    props = _plan([m], _req())[SRC]["u1"]
    assert props["lcsc"] is None
    assert props["MPN "] is None
    assert props["Mfr"] is None
    assert props["LCSC"] == "C1591"
    assert props["Manufacturer"] == "Samsung Electro-Mechanics"


def test_part_values_are_identical_across_members():
    plan = _plan([_member("C1", "u1"), _member("C2", "u2")], _req())
    assert plan[SRC]["u1"] == plan[SRC]["u2"]


# --- end to end ---------------------------------------------------------------

def _without_properties(text):
    """*text* with every ``(property …)`` node and its line removed."""
    out, pos = [], 0
    while True:
        p = text.find("(property ", pos)
        if p == -1:
            return "".join(out) + text[pos:]
        line_start = text.rfind("\n", 0, p) + 1
        end = _find_node_end(text, p)
        out.append(text[pos:line_start])
        pos = end + 1 if text[end:end + 1] == "\n" else end


def _group(groups, ref):
    return next(g for g in groups if ref in g.references)


def test_end_to_end_on_ecg(tmp_path):
    board = tmp_path / ECG.name
    shutil.copy(ECG, board)
    orig = board.read_text(encoding="utf-8")

    defaults = capacitor_defaults({"cap.voltage_min": "16V"})
    group = _group(group_capacitors(iter_bom_part_records(board), defaults), "C16")
    req = group.requirement
    assert req.voltage_min_v == Sourced(16.0, DEFAULT)
    assert not group.assigned

    cand = SimpleNamespace(mpn="CL10B472KB8NNNC",
                           manufacturer="Samsung Electro-Mechanics",
                           digikey_pn="1276-1098-1-ND", lcsc="C1593",
                           package="0603")
    plan = assignment_plan(group, req, cand)
    assert set(plan) == {board}
    for path, uuid_to_props in plan.items():
        write_properties(path, uuid_to_props)

    out = board.read_text(encoding="utf-8")
    tree = sexpdata.loads(out)
    assert tree[0] == sexpdata.Symbol("kicad_sch")

    after = _group(group_capacitors(iter_bom_part_records(board), {}), "C16")
    assert after.references == group.references
    assert after.assigned
    r = after.requirement
    assert r.voltage_min_v == Sourced(16.0, SCHEMATIC)
    assert r.dielectrics == Sourced(("X7R",), SCHEMATIC)
    assert r.tolerance_max_pct == Sourced(10.0, SCHEMATIC)
    assert r.package == Sourced("0603", SCHEMATIC)
    props = after.members[0].props
    assert props["MPN"] == "CL10B472KB8NNNC"
    assert props["LCSC"] == "C1593"
    assert props["DigiKeyPN"] == "1276-1098-1-ND"
    assert "Manufacturer_PN" not in props
    assert "DigikeyPN" not in props

    # Outside (property …) nodes the file is byte-identical, and the diff
    # holds both additions and removals.
    assert _without_properties(out) == _without_properties(orig)
    diff = list(difflib.ndiff(orig.splitlines(), out.splitlines()))
    assert any(ln.startswith("+ ") for ln in diff)
    assert any(ln.startswith("- ") for ln in diff)
    removed = "\n".join(ln for ln in diff if ln.startswith("- "))
    assert '(property "Manufacturer_PN"' in removed


def test_relaxed_tolerance_is_written_as_the_parts_own():
    plan = _plan([_member()], _req(tolerance=Sourced(20.0, RELAXED)))
    assert plan[SRC]["u1"]["Tolerance:Preferred"] == "20%"


def test_relaxed_tolerance_without_a_value_is_not_written():
    plan = _plan([_member()], _req(tolerance=Sourced(None, RELAXED)))
    assert "Tolerance:Preferred" not in plan[SRC]["u1"]


# --- revision 2: part and description sources ---------------------------------

@pytest.mark.parametrize("source", [PART, DESCRIPTION])
def test_part_and_description_fields_are_written(source):
    req = _req(voltage=Sourced(25.0, source),
               dielectrics=Sourced(("X5R", "X7R"), source),
               tolerance=Sourced(10.0, source))
    props = _plan([_member()], req)[SRC]["u1"]
    assert props["Voltage:Min"] == "25V"
    assert props["Class:Preferred"] == "X5R, X7R"
    assert props["Tolerance:Preferred"] == "10%"


def test_requirement_from_description_and_part_is_written():
    # Description gives the voltage; the existing part gives dielectric and
    # tolerance (part outranks description for both).
    m = _member(Value="10uF", Description="10uF, 25V, X5R, 805 package",
                Footprint="Capacitor_SMD:C_0805_2012Metric")
    group = group_capacitors([SimpleNamespace(
        reference=m.reference, source_file=m.source_file,
        symbol_uuid=m.symbol_uuid, props=m.props,
        intent=CapacitorIntent(reference="C1", value="10uF", dnp=False,
                               in_bom=True))], {})[0]
    part = PartEvidence(label="current part 587-2985-1-ND (DigiKey)",
                        capacitance_f=10e-6, dielectrics=("X5R",),
                        tolerance_pct=10.0, package="0805")
    req = group.requirement_with_part(part, {})
    assert req.voltage_min_v == Sourced(25.0, DESCRIPTION)
    assert req.dielectrics.source == PART
    assert req.tolerance_max_pct == Sourced(10.0, PART)
    props = assignment_plan(group, req, CAND)[SRC]["u1"]
    assert props["Voltage:Min"] == "25V"
    assert props["Class:Preferred"] == "X5R, X7R"
    assert props["Tolerance:Preferred"] == "10%"


def test_current_part_fixes_swapped_part_number_fields():
    # BuckConverter: the MPN field holds a DigiKey PN, the DigiKeyPN field an
    # MPN, and a lower-case alias repeats the DigiKey PN.
    m = _member(Value="10uF", MPN="1276-3388-1-ND",
                DigiKeyPN="CL32B106KBJNNWE",
                **{"Digikey PN": "1276-3388-1-ND"})
    current = SimpleNamespace(mpn="CL32B106KBJNNWE",
                              manufacturer="Samsung Electro-Mechanics",
                              digikey_pn="1276-3388-1-ND", lcsc="C77102",
                              package="1210", source="current")
    req = _req()
    props = assignment_plan(RequirementGroup(req, [m]), req, current)[SRC]["u1"]
    assert props["MPN"] == "CL32B106KBJNNWE"
    assert props["DigiKeyPN"] == "1276-3388-1-ND"
    assert props["Manufacturer"] == "Samsung Electro-Mechanics"
    assert props["LCSC"] == "C77102"
    assert props["Digikey PN"] is None
    # Each value is written once, under its canonical key.
    written = [k for k, v in props.items() if v == "1276-3388-1-ND"]
    assert written == ["DigiKeyPN"]
    assert [k for k, v in props.items() if v == "CL32B106KBJNNWE"] == ["MPN"]


def test_value_size_suffix_without_footprint_fills_the_footprint():
    intent = CapacitorIntent(reference="C1", value="C_10U_805", dnp=False,
                             in_bom=True)
    req = capacitor_requirement(intent, "", {})
    assert req.package == Sourced("0805", SCHEMATIC)
    assert req.capacitance_f == pytest.approx(10e-6)
    m = _member(Value="C_10U_805", Footprint="")
    props = assignment_plan(RequirementGroup(req, [m]), req, CAND)[SRC]["u1"]
    assert props["Footprint"] == "Capacitor_SMD:C_0805_2012Metric"
    assert "Package:Preferred" not in props  # stated on the Value


def test_conflicting_requirement_is_refused():
    # BuckConverter: Value 10nF against a 10 uF existing part.
    req = CapacitorRequirement(
        value_text="10nF", capacitance_f=10e-9,
        package=Sourced("1210", SCHEMATIC),
        voltage_min_v=Sourced(50.0, PART),
        dielectrics=Sourced(("X7R",), PART),
        tolerance_max_pct=Sourced(10.0, PART),
        footprint="Capacitor_SMD:C_1210_3225Metric",
        conflicts=("Value says 10nF; current part 1276-3388-1-ND (DigiKey) "
                   "says 10uF",),
    )
    with pytest.raises(ValueError, match="10nF"):
        _plan([_member()], req)


@pytest.mark.parametrize("source", [PART, DESCRIPTION])
def test_capacitance_from_part_or_description_fills_an_empty_value(source):
    req = CapacitorRequirement(
        value_text="", capacitance_f=4.7e-6,
        package=Sourced("0603", SCHEMATIC),
        voltage_min_v=Sourced(16.0, SCHEMATIC),
        dielectrics=Sourced(("X7R",), SCHEMATIC),
        tolerance_max_pct=Sourced(10.0, SCHEMATIC),
        footprint="Capacitor_SMD:C_0603_1608Metric",
        capacitance_source=source,
    )
    props = _plan([_member(Value="")], req)[SRC]["u1"]
    assert props["Value"] == "4.7uF"


def test_stated_value_is_never_replaced():
    # A capacitance tagged from the part never overwrites a Value on the symbol.
    req = CapacitorRequirement(
        value_text="", capacitance_f=4.7e-6,
        package=Sourced("0603", SCHEMATIC),
        voltage_min_v=Sourced(16.0, SCHEMATIC),
        dielectrics=Sourced(("X7R",), SCHEMATIC),
        tolerance_max_pct=Sourced(10.0, SCHEMATIC),
        footprint="Capacitor_SMD:C_0603_1608Metric",
        capacitance_source=PART,
    )
    props = _plan([_member(Value="C_4p7U")], req)[SRC]["u1"]
    assert "Value" not in props
    props = _plan([_member()], _req())[SRC]["u1"]
    assert "Value" not in props


# Real Value / Description / Footprint triples from the boards, with the
# Voltage:Min the plan writes (from the Description, else the remembered 16V).
_REAL = [
    ("C_4p7U", "4.7uF, 16V, X6S 603", "Capacitor_SMD:C_0603_1608Metric", "16V"),
    ("C_10U_805", "10uF, 25V, X5R, 805 package", "", "25V"),
    ("100nF", "Unpolarized capacitor, small symbol",
     "Capacitor_SMD:C_0603_1608Metric", "16V"),
    ("0.1uF", "0.1uF, 25V, X7R 603", "Capacitor_SMD:C_0603_1608Metric", "25V"),
    ("1uF", "1uF, 25V - 50V", "Capacitor_SMD:C_0603_1608Metric", "25V"),
    ("C_22U_1210", "22uF, X5R, 25V, 1210", "", "25V"),
    ("C_P1U", "0.1uF, 25V, X7R, 0603", "Capacitor_SMD:C_0603_1608Metric", "25V"),
    ("10uF", "10uF, 50V, X7R, 1210 package",
     "Capacitor_SMD:C_1210_3225Metric", "50V"),
    ("4.7n", "", "Capacitor_SMD:C_0402_1005Metric", "16V"),
    ("1u", "Polarized capacitor, small US symbol",
     "Capacitor_SMD:C_0805_2012Metric", "16V"),
]


@pytest.mark.parametrize("value,description,footprint,volts", _REAL)
def test_real_symbols_keep_stated_fields(value, description, footprint, volts):
    props = {"Reference": "C1", "Value": value, "Description": description,
             "Footprint": footprint}
    rec = SimpleNamespace(
        reference="C1", source_file=SRC, symbol_uuid="u1", props=props,
        intent=CapacitorIntent(reference="C1", value=value, dnp=False,
                               in_bom=True))
    defaults = capacitor_defaults({"cap.voltage_min": "16V"})
    group = group_capacitors([rec], defaults)[0]
    req = group.requirement
    assert req.searchable, req.problem()
    out = assignment_plan(group, req, CAND)[SRC]["u1"]
    # Value, Description and a stated footprint are never rewritten.
    assert "Value" not in out
    assert "Description" not in out
    assert "Package:Preferred" not in out
    if footprint:
        assert "Footprint" not in out
    else:
        assert out["Footprint"] == FOOTPRINTS[req.package.value]
    assert out["Voltage:Min"] == volts
