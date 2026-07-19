"""Tests for the proposed-change representation (kicadtools.changes)."""
import shutil
from pathlib import Path

from kicadtools.changes import (
    ChangeSet,
    ProposedChange,
    changes_from_confirm,
)
from kicadtools.confirm import confirm_resistor
from kicadtools.intent import ResistorIntent
from kicadtools.resolve import PartRecord, iter_bom_part_records
from kicadtools.suppliers import DigiKeyProduct
from kicadtools.writer import write_properties

BOARD = Path(__file__).parent / "data" / "MotorDriver.kicad_sch"


def _resistor(**kw):
    kw.setdefault("reference", "R1")
    kw.setdefault("dnp", False)
    kw.setdefault("in_bom", True)
    return ResistorIntent(**kw)


def _record(intent, source_file=BOARD, uuid="uuid-1", **props):
    props.setdefault("Reference", intent.reference)
    return PartRecord(intent.reference, props, source_file, uuid, intent)


def _product(mpn="RC0402FR-0710KL", **params):
    return DigiKeyProduct(mpn=mpn, parameters=params)


# --------------------------------------------------------------------------- #
# changes_from_confirm
# --------------------------------------------------------------------------- #

def test_blank_fields_become_intent_fill_changes():
    intent = _resistor(value="10k")  # tolerance + power blank
    product = _product(
        Resistance="10 kOhms",
        Tolerance="±1%",
        **{"Power (Watts)": "0.063W, 1/16W"},
    )
    cs = changes_from_confirm(_record(intent, MPN="RC0402FR-0710KL"), confirm_resistor(intent, product))

    fills = {c.property_key: c.value for c in cs.changes if c.kind == "intent-fill"}
    assert fills == {"Tolerance:Preferred": "1%", "Power:Min": "0.063W"}
    assert not cs.conflicts


def test_conflict_is_recorded_but_not_a_change():
    intent = _resistor(value="10k", power_min="0.25W")  # over-spec'd vs the part
    product = _product(Resistance="10 kOhms", **{"Power (Watts)": "0.063W, 1/16W"})
    cs = changes_from_confirm(_record(intent, MPN="RC0402FR-0710KL"), confirm_resistor(intent, product))

    assert [c.label for c in cs.conflicts] == ["power rating"]
    # A conflict never becomes a write-back.
    assert all(c.property_key != "Power:Min" for c in cs.changes)
    assert not cs.write_plan().get(BOARD, {}).get("uuid-1", {}).get("Power:Min")


def test_missing_mpn_is_added_from_digikey():
    intent = _resistor(value="10k")
    # Symbol carried only a DigiKey PN — the resolved MPN should be proposed.
    cs = changes_from_confirm(
        _record(intent, **{"DigiKey PN": "311-10.0KLRCT-ND"}),
        confirm_resistor(intent, _product(Resistance="10 kOhms")),
    )
    pn_changes = [c for c in cs.changes if c.kind == "part-number"]
    assert len(pn_changes) == 1
    assert pn_changes[0].property_key == "MPN"
    assert pn_changes[0].value == "RC0402FR-0710KL"


def test_present_mpn_is_not_re_added():
    intent = _resistor(value="10k")
    cs = changes_from_confirm(
        _record(intent, MPN="RC0402FR-0710KL"),
        confirm_resistor(intent, _product(Resistance="10 kOhms")),
    )
    assert not [c for c in cs.changes if c.kind == "part-number"]


def test_fully_specified_matching_part_proposes_nothing():
    intent = _resistor(
        value="10k",
        tolerance_preferred=["1%"],
        power_min="0.063W",
        footprint="Resistor_SMD:R_0402_1005Metric",
    )
    product = _product(
        Resistance="10 kOhms",
        Tolerance="±1%",
        **{"Power (Watts)": "0.063W, 1/16W", "Supplier Device Package": "0402"},
    )
    cs = changes_from_confirm(_record(intent, MPN="RC0402FR-0710KL"), confirm_resistor(intent, product))
    assert cs.is_empty


# --------------------------------------------------------------------------- #
# ChangeSet.write_plan
# --------------------------------------------------------------------------- #

def test_write_plan_groups_by_file_and_uuid():
    a = ProposedChange("R1", BOARD, "u1", "Power:Min", "0.1W", "intent-fill", "x")
    b = ProposedChange("R1", BOARD, "u1", "Tolerance:Preferred", "1%", "intent-fill", "y")
    c = ProposedChange("R2", BOARD, "u2", "MPN", "ABC", "part-number", "z")
    cs = ChangeSet(changes=[a, b, c])
    plan = cs.write_plan()
    assert plan == {
        BOARD: {
            "u1": {"Power:Min": "0.1W", "Tolerance:Preferred": "1%"},
            "u2": {"MPN": "ABC"},
        }
    }


def test_write_plan_drops_changes_without_provenance():
    # A hand-built record (no uuid) has nowhere to write — it must not appear.
    orphan = ProposedChange("R1", None, None, "Power:Min", "0.1W", "intent-fill", "x")
    assert ChangeSet(changes=[orphan]).write_plan() == {}


def test_extend_merges_changes_and_conflicts():
    cs = ChangeSet()
    cs.extend(changes_from_confirm(
        _record(_resistor(value="10k"), MPN="M1"),
        confirm_resistor(_resistor(value="10k"), _product(Resistance="10 kOhms", Tolerance="±1%")),
    ))
    assert any(c.property_key == "Tolerance:Preferred" for c in cs.changes)


# --------------------------------------------------------------------------- #
# End-to-end: a plan actually lands on a real schematic
# --------------------------------------------------------------------------- #

def test_write_plan_applies_to_a_real_schematic(tmp_path):
    copy = tmp_path / "board.kicad_sch"
    shutil.copy(BOARD, copy)

    # Borrow a real resistor's uuid/reference so the writer can locate it.
    src = next(
        r for r in iter_bom_part_records(BOARD)
        if isinstance(r.intent, ResistorIntent) and r.symbol_uuid
    )
    intent = _resistor(reference=src.reference, value="10k")  # tolerance blank
    rec = PartRecord(src.reference, {"Reference": src.reference}, copy, src.symbol_uuid, intent)

    product = _product(Resistance="10 kOhms", Tolerance="±1%")
    plan = changes_from_confirm(rec, confirm_resistor(intent, product)).write_plan()
    assert copy in plan

    write_properties(copy, plan[copy])

    text = copy.read_text(encoding="utf-8")
    block_start = text.index(f'(uuid "{src.symbol_uuid}")')
    block = text[block_start: block_start + 4000]
    assert '(property "Tolerance:Preferred" "1%"' in block  # the fill landed
    assert '(property "MPN" "RC0402FR-0710KL"' in block      # the resolved MPN landed
