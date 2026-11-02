from pathlib import Path

import pytest

from kicadtools.components import build_intent, get_components
from kicadtools.icrequirement import (
    IC_KIND,
    NO_FOOTPRINT,
    NO_PART_NUMBER,
    IcRequirement,
    Identifiers,
    group_ics,
    ic_group_key,
    ic_identifiers,
    ic_requirement,
    normalize_identifier,
    value_mpns,
)
from kicadtools.intent import ElectricalIntent, IcIntent, LedIntent
from kicadtools.requirement import group_parts
from kicadtools.resolve import PartRecord, iter_bom_part_records

DATA = Path(__file__).parent / "data"


# ----------------------------------------------------------------------
# value_mpns
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("MotorControl_STM32L552CCT6_V2", ("STM32L552CCT6",)),
        ("LMC7101BYM5", ("LMC7101BYM5",)),
        ("ABX00027", ("ABX00027",)),
        ("AD8232ACPZ-R7", ("AD8232ACPZ-R7",)),
        ("LTC6992CS6-2", ("LTC6992CS6-2",)),
        ("DRV8313RHHR", ("DRV8313RHHR",)),
        ("1515 rgb led", ()),
        ("Arduino Nano", ()),
        ("", ()),
        ("~", ()),
    ],
)
def test_value_mpns_on_board_values(value, expected):
    assert value_mpns(value) == expected


def test_value_mpns_longest_first_ties_keep_value_order():
    assert value_mpns("AB123,LM358N XY789 LDL212PV33R") == (
        "LDL212PV33R", "LM358N", "AB123", "XY789",
    )


def test_value_mpns_minimum_length_is_five():
    assert value_mpns("A1234") == ("A1234",)
    assert value_mpns("A123") == ()


def test_value_mpns_needs_a_letter_and_a_digit():
    assert value_mpns("12345 ABCDE") == ()


def test_value_mpns_drops_repeats_compared_normalized():
    assert value_mpns("lm358n LM358N") == ("lm358n",)


def test_value_mpns_splits_on_whitespace_underscore_comma_only():
    # Hyphen, "#" and "/" are part of real MPNs and stay inside the token.
    assert value_mpns("LTC6992HS6-2#TRMPBF\tfoo") == ("LTC6992HS6-2#TRMPBF",)
    assert value_mpns("X_Y,ZZ 12") == ()


def test_value_mpns_ignores_non_strings():
    assert value_mpns(None) == ()
    assert value_mpns(42) == ()


def test_normalize_identifier():
    assert normalize_identifier(" ldl212 pv33r ") == "LDL212PV33R"
    assert normalize_identifier(None) == ""
    assert normalize_identifier("") == ""


# ----------------------------------------------------------------------
# ic_identifiers
# ----------------------------------------------------------------------

def test_ic_identifiers_reads_aliases_and_value():
    ids = ic_identifiers({
        "Value": "MotorControl_STM32L552CCT6_V2",
        "Manufacturer_PN": "STM32L552CCT6",
        "DigiKey PN": "497-STM32L552CCT6-ND",
        "DigikeyPN": "",
        "JLCPCB": "C123",
    })
    assert ids == Identifiers(
        mpn="STM32L552CCT6",
        digikey_pn="497-STM32L552CCT6-ND",
        lcsc="C123",
        value_mpns=("STM32L552CCT6",),
    )
    assert not ids.empty


def test_ic_identifiers_empty_when_nothing_mpn_shaped():
    ids = ic_identifiers({"Value": "Arduino Nano", "MPN": "  "})
    assert ids == Identifiers()
    assert ids.empty


@pytest.mark.parametrize(
    "props",
    [{"MPN": "X"}, {"DigiKeyPN": "X"}, {"LCSC": "C1"}, {"Value": "LM358N"}],
)
def test_any_single_identifier_is_enough(props):
    assert not ic_identifiers(props).empty


def test_identifiers_key_is_normalized():
    a = Identifiers(mpn="ldl212pv33r", digikey_pn="497-19548-2-nd",
                    value_mpns=("ldl212pv33r",))
    b = Identifiers(mpn="LDL212PV33R ", digikey_pn="497-19548-2-ND",
                    value_mpns=("LDL212PV33R",))
    assert a.key() == b.key()


# ----------------------------------------------------------------------
# IcRequirement
# ----------------------------------------------------------------------

def test_requirement_from_props():
    req = ic_requirement({
        "Value": " LMC7101BYM5 ",
        "Footprint": "Package_TO_SOT_SMD:SOT-23-5",
    })
    assert req.kind == IC_KIND == "ic"
    assert req.value_text == "LMC7101BYM5"
    assert req.footprint == "Package_TO_SOT_SMD:SOT-23-5"
    assert req.identifiers.value_mpns == ("LMC7101BYM5",)
    assert req.package is None and req.pad_count is None
    assert req.searchable
    assert req.problem() == ""


def test_requirement_no_part_number():
    req = ic_requirement({"Value": "Arduino Nano", "Footprint": "X:Y"})
    assert not req.searchable
    assert req.problem() == NO_PART_NUMBER == "no part number"


def test_requirement_no_footprint():
    req = ic_requirement({"Value": "LMC7101BYM5", "Footprint": ""})
    assert req.problem() == NO_FOOTPRINT == "no footprint"


def test_requirement_no_part_number_reported_before_no_footprint():
    assert ic_requirement({"Value": "op amp"}).problem() == NO_PART_NUMBER


def test_requirement_carries_package_and_pad_count():
    # The search step fills these in; any object stands in for a PackageInfo.
    pkg = object()
    req = IcRequirement(identifiers=Identifiers(mpn="X"), footprint="F:G",
                        package=pkg, pad_count=36)
    assert req.package is pkg
    assert req.pad_count == 36


# ----------------------------------------------------------------------
# Grouping
# ----------------------------------------------------------------------

def _rec(ref, **props):
    props = {"Reference": ref, "dnp": False, "in_bom": True, **props}
    return PartRecord(reference=ref, props=props, source_file=Path("x.kicad_sch"),
                      symbol_uuid=f"uuid-{ref}", intent=build_intent(props))


def test_same_identifiers_and_footprint_group_together():
    groups = group_ics([
        _rec("U10", Value="DRV8313RHHR", Footprint="TI:RHH0036C"),
        _rec("U2", Value="drv8313rhhr", Footprint="TI:RHH0036C"),
    ])
    assert len(groups) == 1
    g = groups[0]
    assert g.references == ["U2", "U10"]
    assert g.quantity == 2
    assert g.kind == "ic"
    assert g.requirement.identifiers.value_mpns == ("DRV8313RHHR",)


def test_same_part_on_different_footprints_is_two_groups():
    # The two LDL212 symbols: one DigiKey PN, footprints of different pitch.
    groups = group_ics([
        _rec("U4", Value="LDL212PV33R", DigiKeyPN="497-19548-2-ND",
             Footprint="STMicro:LDL212_2x2mm_Pitch0.5mm"),
        _rec("U2", Value="LDL212PV33R", DigiKeyPN="497-19548-2-ND",
             Footprint="Package_DFN_QFN:DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm"),
    ])
    assert [g.references for g in groups] == [["U2"], ["U4"]]


def test_different_part_numbers_are_separate_groups():
    groups = group_ics([
        _rec("U1", Value="LTC6992CS6-2", Footprint="F:TSOT-23-6"),
        _rec("U2", Value="LTC6992CS6-2", Footprint="F:TSOT-23-6",
             DigiKeyPN="161-LTC6992HS6-2"),
    ])
    assert len(groups) == 2


def test_digikey_pn_alias_spellings_group_together():
    groups = group_ics([
        _rec("U1", Value="LM358N", Footprint="F:G", **{"DigiKey PN": "LM358N-ND"}),
        _rec("U2", Value="LM358N", Footprint="F:G", DigiKeyPN="lm358n-nd"),
    ])
    assert len(groups) == 1


def test_value_text_differing_only_outside_mpn_tokens_groups_together():
    groups = group_ics([
        _rec("U1", Value="Board_STM32L552CCT6_V2", Footprint="F:LQFP-48"),
        _rec("U2", Value="STM32L552CCT6", Footprint="F:LQFP-48"),
    ])
    assert len(groups) == 1


def test_unsearchable_parts_with_different_values_are_not_merged():
    groups = group_ics([
        _rec("U1", Value="Arduino Nano", Footprint="F:Custom"),
        _rec("U2", Value="OLED display", Footprint="F:Custom"),
        _rec("U3", Value="arduino nano", Footprint="F:Custom"),
    ])
    assert [g.references for g in groups] == [["U1", "U3"], ["U2"]]
    assert all(g.requirement.problem() == NO_PART_NUMBER for g in groups)


def test_group_key_includes_footprint():
    a = ic_group_key({"Value": "LM358N", "Footprint": "A:B"})
    b = ic_group_key({"Value": "LM358N", "Footprint": "A:C"})
    assert a != b


def test_group_ics_skips_other_types():
    groups = group_ics([
        _rec("R1", Value="10k", Footprint="Resistor_SMD:R_0603_1608Metric"),
        _rec("U1", Value="LM358N", Footprint="F:G"),
        PartRecord(reference="U9", props={"Value": "LM358N"},
                   source_file=Path("x"), symbol_uuid=None, intent=None),
    ])
    assert [g.references for g in groups] == [["U1"]]


def test_group_parts_still_ignores_ics():
    recs = [
        _rec("R1", Value="10k", Footprint="Resistor_SMD:R_0603_1608Metric"),
        _rec("U1", Value="LM358N", Footprint="F:G"),
    ]
    assert isinstance(recs[1].intent, IcIntent)
    groups = group_parts(recs, {})
    assert [g.references for g in groups] == [["R1"]]


def test_assigned_needs_mpn_manufacturer_digikey_and_footprint_not_lcsc():
    full = dict(Value="LM358N", Footprint="F:G", MPN="LM358N",
                Manufacturer="TI", DigiKeyPN="296-1-ND")
    assert group_ics([_rec("U1", **full)])[0].assigned
    no_mfr = {k: v for k, v in full.items() if k != "Manufacturer"}
    assert not group_ics([_rec("U1", **no_mfr)])[0].assigned
    no_fp = dict(full, Footprint="")
    assert not group_ics([_rec("U1", **no_fp)])[0].assigned


def test_group_existing_part_numbers():
    g = group_ics([_rec("U1", Value="DRV8313RHHR", Footprint="F:G",
                        **{"Digi-Key_PN": "296-44591-2-ND", "LCSC": "C165154"})])[0]
    assert g.existing.digikey_pn == "296-44591-2-ND"
    assert g.existing.lcsc == "C165154"


# ----------------------------------------------------------------------
# Fixtures: guessed "ic" parts now build an IcIntent
# ----------------------------------------------------------------------

def test_guessed_u_parts_on_ecg_are_ic_intents():
    comps = {c.reference: c for c in get_components(DATA / "ECG.kicad_sch")}
    for ref in ("U1", "U2", "U3"):
        u = comps[ref]
        assert isinstance(u, IcIntent), ref
        assert u.intent_guessed is True
        assert u.classification_note is None
    assert comps["U1"].digikey_pn == "505-AD8232ACPZ-R7CT-ND"
    assert comps["U3"].value == "ABX00027"


def test_motordriver_leds_on_u_refs_stay_leds():
    comps = get_components(DATA / "MotorDriver.kicad_sch")
    for ref in ("U5", "U6", "U7", "U8"):
        assert isinstance(next(c for c in comps if c.reference == ref), LedIntent)


def test_no_guessed_u_part_is_left_unclassified_on_fixtures():
    for name in ("ECG", "MotorDriver"):
        for c in get_components(DATA / f"{name}.kicad_sch"):
            if c.reference.startswith("U"):
                assert type(c) is not ElectricalIntent, (name, c.reference)


def test_ecg_ic_groups():
    groups = group_ics(iter_bom_part_records(DATA / "ECG.kicad_sch"))
    by_ref = {g.references[0]: g for g in groups}
    assert set(by_ref) == {"U1", "U2", "U3"}
    assert by_ref["U2"].requirement.identifiers.value_mpns == ("LMC7101BYM5",)
    assert by_ref["U3"].requirement.identifiers.value_mpns == ("ABX00027",)
    assert all(g.requirement.searchable for g in groups)


def test_motordriver_ic_groups():
    groups = group_ics(iter_bom_part_records(DATA / "MotorDriver.kicad_sch"))
    refs = [g.references for g in groups]
    assert ["U1", "U2"] in refs
    u9 = next(g for g in groups if "U9" in g.references)
    assert u9.requirement.identifiers.value_mpns == ("STM32L552CCT6",)
    assert u9.requirement.identifiers.digikey_pn == "497-STM32L552CCT6-ND"
    u1 = next(g for g in groups if "U1" in g.references)
    assert u1.requirement.identifiers.lcsc == "C165154"
    # LEDs on U references are not ICs.
    assert not any(r in g.references for g in groups for r in ("U5", "U6", "U7", "U8"))


def test_multi_unit_symbol_counts_once():
    # MotorDriver U9: the STM32 symbol is drawn as units 1 and 2. Each unit
    # is a member (its own uuid, written by the plan); the part is placed once.
    groups = group_ics(iter_bom_part_records(DATA / "MotorDriver.kicad_sch"))
    u9 = next(g for g in groups if "U9" in g.references)
    assert u9.references == ["U9"]
    assert u9.quantity == 1
    assert len(u9.members) == 2
    assert len({m.symbol_uuid for m in u9.members}) == 2
    u1 = next(g for g in groups if "U1" in g.references)
    assert (u1.references, u1.quantity) == (["U1", "U2"], 2)


def test_quantity_counts_distinct_references():
    def rec(ref, uuid):
        r = _rec(ref, Value="DRV8313RHHR", Footprint="TexasInstruments:RHH0036C")
        r.symbol_uuid = uuid
        return r
    (g,) = group_ics([rec("U10", "u1"), rec("U2", "u2"), rec("U10", "u3")])
    assert g.references == ["U2", "U10"]
    assert g.quantity == 2
    assert [m.symbol_uuid for m in g.members] == ["u1", "u2", "u3"]
