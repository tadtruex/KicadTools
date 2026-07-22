"""Unit tests for the electrical-parameter editor's pure logic.

Only the field/default helpers are tested here — they run without a QApplication,
matching how the other GUI tests exercise the table models.
"""
import pytest

pytest.importorskip("PySide6")

from kicadtools.intent import ElectricalIntent, ResistorIntent  # noqa: E402
from kicadtools.gui.param_editor import (  # noqa: E402
    BASE_FIELDS,
    context_rows,
    edits_from,
    fields_for,
    prefilled_values,
)


def _resistor(**kw):
    kw.setdefault("reference", "R1")
    kw.setdefault("dnp", False)
    kw.setdefault("in_bom", True)
    kw.setdefault("intent_type", "resistor")
    kw.setdefault("value", "10K")
    return ResistorIntent(**kw)


def test_fields_for_resistor_has_base_plus_resistor_fields():
    keys = [f.prop_key for f in fields_for(_resistor())]
    assert keys == [
        "Intent:Type",
        "Package:Preferred",
        "Mfr:Preferred",
        "Tolerance:Preferred",
        "Power:Min",
    ]


def test_editable_fields_never_target_kicad_owned_properties():
    # KiCad's own fields must never be write targets — they are context only.
    kicad_owned = {"Value", "Footprint", "Datasheet", "Reference"}
    keys = {f.prop_key for f in fields_for(_resistor())}
    assert keys.isdisjoint(kicad_owned)


def test_fields_for_unclassified_is_base_only():
    base = ElectricalIntent(reference="U1", dnp=False, in_bom=True)
    assert [f.prop_key for f in fields_for(base)] == [f.prop_key for f in BASE_FIELDS]


def test_resistor_defaults_fill_blank_fields():
    pre = prefilled_values(_resistor())  # blank package/tolerance/mfr
    assert pre["Package:Preferred"] == "0603"
    assert pre["Tolerance:Preferred"] == "1%"
    assert pre["Mfr:Preferred"] == "Yageo, Bourns, Samsung"
    assert pre["Intent:Type"] == "resistor"
    # A field with no default stays blank.
    assert pre["Power:Min"] == ""


def test_kicad_value_is_context_not_editable():
    r = _resistor()  # value="10K"
    assert "Value" not in prefilled_values(r)  # resistance is never written
    assert ("KiCad Value", "10K") in context_rows(r)


def test_specified_fields_are_not_overwritten_by_defaults():
    r = _resistor(
        package_preferred=["0402"],
        tolerance_preferred=["0.1%"],
        mfr_preferred=["Vishay"],
    )
    pre = prefilled_values(r)
    assert pre["Package:Preferred"] == "0402"
    assert pre["Tolerance:Preferred"] == "0.1%"
    assert pre["Mfr:Preferred"] == "Vishay"


def test_unclassified_gets_no_type_defaults():
    base = ElectricalIntent(reference="U1", dnp=False, in_bom=True)
    pre = prefilled_values(base)
    assert pre["Package:Preferred"] == ""  # no resistor default leaks onto a non-resistor
    assert pre["Mfr:Preferred"] == ""


def test_edits_from_drops_blanks_and_trims():
    got = edits_from(
        {"Package:Preferred": " 0603 ", "Power:Min": "", "Mfr:Preferred": "   "}
    )
    assert got == {"Package:Preferred": "0603"}
