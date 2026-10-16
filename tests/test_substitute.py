"""Tests for the substitute search (kicadtools.substitute) — offline throughout:
the JLCPCB and DigiKey lookups are injected fakes."""
import pytest

from kicadtools.intent import ElectricalIntent
from kicadtools.substitute import (
    ALTERNATES_KEY,
    STOCK_LOW,
    STOCK_OK,
    STOCK_OUT,
    build_spec,
    evaluate,
    find_substitutes,
    merge_alternates,
    package_code,
    search_substitutes,
    stock_level,
)
from kicadtools.suppliers import DigiKeyProduct, JlcPart

CAP_FP = "Capacitor_SMD:C_0603_1608Metric"
RES_FP = "Resistor_SMD:R_0603_1608Metric"


def _intent(**props):
    props.setdefault("Reference", "X1")
    props.setdefault("dnp", False)
    props.setdefault("in_bom", True)
    return ElectricalIntent.from_dict(props)


def _cap(lcsc, *, cap="100nF", volts="25V", diel="X7R", tol="±10%", pkg="0603",
         stock=1000, lib="expand", price=0.01, **kw):
    attrs = {"Capacitance": cap, "Voltage Rating": volts,
             "Temperature Coefficient": diel, "Tolerance": tol}
    attrs = {k: v for k, v in attrs.items() if v is not None}
    return JlcPart(lcsc=lcsc, mpn=f"MPN-{lcsc}", package=pkg, stock=stock,
                   library=lib, unit_price=price, attributes=attrs, **kw)


def _res(lcsc, *, ohms="49.9kΩ", tol="±1%", power="100mW", pkg="0603", stock=1000,
         lib="expand", price=0.01):
    return JlcPart(lcsc=lcsc, mpn=f"MPN-{lcsc}", package=pkg, stock=stock, library=lib,
                   unit_price=price,
                   attributes={"Resistance": ohms, "Tolerance": tol, "Power(Watts)": power})


# ---- package / stock -------------------------------------------------------

@pytest.mark.parametrize("text, code", [
    ("Capacitor_SMD:C_0603_1608Metric", "0603"),
    ("Resistor_SMD:R_0805_2012Metric", "0805"),
    ("0603 (1608 Metric)", "0603"),
    ("0402", "0402"),
    ("Inductor_SMD:L_Coilcraft_XxL4040", None),
    ("Lib0603:SOT-23", None),  # the library name never counts
    ("", None),
])
def test_package_code(text, code):
    assert package_code(text) == code


def test_stock_level():
    assert stock_level(None, 25) is None
    assert stock_level(JlcPart("C1", stock=0), 25) == STOCK_OUT
    assert stock_level(JlcPart("C1", stock=500, no_buy_reason="discontinued"), 25) == STOCK_OUT
    assert stock_level(JlcPart("C1", stock=10), 25) == STOCK_LOW
    assert stock_level(JlcPart("C1", stock=25), 25) == STOCK_OK


# ---- spec building ---------------------------------------------------------

def test_spec_from_jlcpcb_attributes():
    current = _cap("C1", cap="100nF", volts="25V", diel="X7R", tol="±10%")
    spec = build_spec("capacitor", CAP_FP, part_attrs=current.attributes, part_source="jlcpcb")
    assert spec.searchable
    assert spec.package == "0603" and spec.package_source == "footprint"
    shown = {c.label: c.shown for c in spec.constraints}
    assert shown == {"Capacitance": "100nF", "Voltage": "≥ 25V",
                     "Dielectric": "X7R", "Tolerance": "≤ 10%"}
    assert all(c.source == "current part (JLCPCB)" for c in spec.constraints)
    assert spec.keywords() == ["100nF 0603 X7R"]


def test_spec_from_digikey_parameters_uses_digikey_names():
    params = {"Capacitance": "0.1 µF", "Voltage - Rated": "25V",
              "Temperature Coefficient": "X7R", "Tolerance": "±10%"}
    spec = build_spec("capacitor", CAP_FP, part_attrs=params, part_source="digikey")
    shown = {c.label: c.shown for c in spec.constraints}
    assert shown["Capacitance"] == "100nF"
    assert shown["Voltage"] == "≥ 25V"
    assert {c.source for c in spec.constraints} == {"current part (DigiKey)"}


def test_intent_fills_what_the_part_lacks():
    intent = _intent(**{"Intent:Type": "capacitor", "Value": "100n", "Voltage:Min": "50V"})
    spec = build_spec("capacitor", CAP_FP, part_attrs={"Capacitance": "100nF"},
                      part_source="jlcpcb", intent=intent)
    by = {c.label: c for c in spec.constraints}
    assert by["Capacitance"].source == "current part (JLCPCB)"
    assert by["Voltage"].source == "intent" and by["Voltage"].shown == "≥ 50V"


def test_intent_alone_when_no_current_part():
    intent = _intent(**{"Intent:Type": "resistor", "Value": "4k7",
                        "Tolerance:Preferred": "1%, 5%", "Power:Min": "0.1W"})
    spec = build_spec("resistor", RES_FP, intent=intent)
    shown = {c.label: c.shown for c in spec.constraints}
    # The loosest preferred tolerance is the ceiling.
    assert shown == {"Resistance": "4.7k", "Tolerance": "≤ 5%", "Power": "≥ 100mW"}


def test_unparseable_symbol_value_is_missing():
    # Library-style values like "C_P1U" aren't a capacitance.
    intent = _intent(**{"Intent:Type": "capacitor", "Value": "C_P1U"})
    spec = build_spec("capacitor", CAP_FP, intent=intent)
    assert not spec.searchable
    assert spec.missing == ["Capacitance", "Voltage"]
    assert "Capacitance, Voltage" in spec.problem()


def test_capacitor_needs_a_voltage():
    spec = build_spec("capacitor", CAP_FP, part_attrs={"Capacitance": "1uF"},
                      part_source="jlcpcb")
    assert spec.missing == ["Voltage"]


def test_no_package_is_unsearchable_unless_the_part_names_one():
    attrs = {"Resistance": "10kΩ"}
    spec = build_spec("resistor", "Custom:Weird", part_attrs=attrs, part_source="jlcpcb")
    assert not spec.searchable and "package" in spec.problem()
    spec = build_spec("resistor", "Custom:Weird", part_attrs=attrs, part_source="jlcpcb",
                      part_package="0402")
    assert spec.package == "0402" and spec.package_source == "current part (JLCPCB)"


def test_unsupported_type():
    spec = build_spec("inductor", "Inductor_SMD:L_0603_1608Metric")
    assert not spec.searchable and "resistors and capacitors" in spec.problem()


def test_dielectric_upgrades_from_current_part_but_not_from_intent():
    spec = build_spec("capacitor", CAP_FP, part_source="jlcpcb",
                      part_attrs={"Capacitance": "4.7uF", "Voltage Rating": "16V",
                                  "Temperature Coefficient": "X5R"})
    diel = next(c for c in spec.constraints if c.label == "Dielectric")
    assert diel.choices == ("X5R", "X7R")
    assert spec.keywords() == ["4.7uF 0603 X5R", "4.7uF 0603 X7R"]

    intent = _intent(**{"Intent:Type": "capacitor", "Value": "4.7u", "Voltage:Min": "16V",
                        "Class:Preferred": "X5R"})
    spec = build_spec("capacitor", CAP_FP, intent=intent)
    diel = next(c for c in spec.constraints if c.label == "Dielectric")
    assert diel.choices == ("X5R",)  # the author's list is taken as written


# ---- matching --------------------------------------------------------------

def _cap_spec():
    return build_spec("capacitor", CAP_FP, part_source="jlcpcb",
                      part_attrs=_cap("C0").attributes)


def test_evaluate_accepts_equal_and_better_ratings():
    spec = _cap_spec()
    assert evaluate(spec, _cap("C1")) is not None
    assert evaluate(spec, _cap("C2", volts="50V", tol="±5%")) is not None  # higher V, tighter tol
    cand = evaluate(spec, _cap("C3", cap="0.1uF"))  # same value, other spelling
    assert cand is not None
    assert [c.label for c in cand.checks] == [
        "Package", "Capacitance", "Voltage", "Dielectric", "Tolerance"]


@pytest.mark.parametrize("part", [
    _cap("C1", cap="1uF"),            # wrong value
    _cap("C1", volts="16V"),          # below the voltage floor
    _cap("C1", tol="±20%"),           # looser tolerance
    _cap("C1", diel="X5R"),           # worse dielectric
    _cap("C1", pkg="0402"),           # wrong package
    _cap("C1", volts=None),           # rating unknown -> not acceptable
])
def test_evaluate_rejects(part):
    assert evaluate(_cap_spec(), part) is None


def test_find_substitutes_ranks_and_filters():
    spec = build_spec("resistor", RES_FP, part_source="jlcpcb",
                      part_attrs=_res("C0").attributes)
    parts = [
        _res("C0", lib="base", stock=10**6),            # the current part: excluded
        _res("C1", lib="expand", stock=5000, price=0.001),
        _res("C2", lib="base", stock=5000, price=0.002),
        _res("C3", lib="base", stock=10, price=0.0001),  # not enough for the build
        _res("C4", lib="base", stock=0),                 # not orderable
        _res("C5", ohms="10kΩ"),                         # not equivalent
        _res("C2", lib="base", stock=5000, price=0.002),  # duplicate hit
    ]
    got = [c.part.lcsc for c in find_substitutes(spec, parts, needed=25, exclude=["C0"])]
    # Enough stock first, then Basic before Extended; short-stock parts last.
    assert got == ["C2", "C1", "C3"]


# ---- orchestration ---------------------------------------------------------

class FakeDigiKey:
    configured = True

    def __init__(self, product):
        self.product = product
        self.calls = []

    def product_for(self, pn):
        self.calls.append(pn)
        return self.product


def test_search_uses_current_jlcpcb_part():
    current = _cap("C0")
    searched = []

    def search(kw):
        searched.append(kw)
        return [_cap("C1", lib="base", stock=10**6), current]

    dk = FakeDigiKey(None)
    result = search_substitutes(
        intent=_intent(**{"Intent:Type": "capacitor", "Value": "C_P1U"}),
        footprint=CAP_FP, lcsc="C0", mpn="M", digikey_pn="D", needed=50,
        part_lookup={"C0": current}.get, search=search, digikey=dk,
    )
    assert searched == ["100nF 0603 X7R"]
    assert [c.part.lcsc for c in result.candidates] == ["C1"]
    assert dk.calls == []  # JLCPCB had attributes, so DigiKey wasn't asked


def test_search_falls_back_to_digikey_for_a_placeholder():
    placeholder = JlcPart("C9900013546", mpn="CGA3E2X7R1E104K080AA", package="0603",
                          no_buy_reason="This product is no longer manufactured.")
    product = DigiKeyProduct(mpn="CGA3E2X7R1E104K080AA", parameters={
        "Capacitance": "0.1 µF", "Voltage - Rated": "25V",
        "Temperature Coefficient": "X7R", "Tolerance": "±10%"})
    dk = FakeDigiKey(product)
    result = search_substitutes(
        intent=_intent(**{"Intent:Type": "capacitor", "Value": "C_P1U"}),
        footprint=CAP_FP, lcsc="C9900013546", mpn="CGA3E2X7R1E104K080AA",
        digikey_pn="445-5667-1-ND", needed=50,
        part_lookup={"C9900013546": placeholder}.get,
        search=lambda kw: [_cap("C14663", volts="50V", lib="base", stock=10**7)],
        digikey=dk,
    )
    assert dk.calls == ["CGA3E2X7R1E104K080AA"]  # MPN preferred over the DigiKey PN
    assert result.current is placeholder
    assert [c.part.lcsc for c in result.candidates] == ["C14663"]


def test_unsearchable_spec_does_not_search():
    def search(kw):
        raise AssertionError("should not search")

    result = search_substitutes(
        intent=_intent(**{"Intent:Type": "capacitor", "Value": "C_P1U"}),
        footprint=CAP_FP, lcsc="", mpn="", digikey_pn="", needed=25,
        part_lookup=lambda c: None, search=search, digikey=None,
    )
    assert not result.spec.searchable and result.candidates == []


# ---- Alternates ------------------------------------------------------------

def test_merge_alternates():
    assert ALTERNATES_KEY == "Alternates"
    assert merge_alternates("", "311-1341-1-ND") == "311-1341-1-ND"
    assert merge_alternates(None, "A") == "A"
    assert merge_alternates("A, B", "C") == "A, B, C"
    assert merge_alternates("A,b", "B") == "A, b"  # already listed (case-insensitive)
