"""Tests for the resistor confirm/fill layer (kicadtools.confirm)."""
from pathlib import Path

import pytest

from kicadtools.confirm import (
    Outcome,
    confirm_resistor,
    parse_percent,
    parse_resistance,
    parse_watts,
)
from kicadtools.intent import ResistorIntent
from kicadtools.resolve import PartRecord, Resolver
from kicadtools.suppliers import DigiKeyProduct

BOARD = Path(__file__).parent / "data" / "MotorDriver.kicad_sch"


# --------------------------------------------------------------------------- #
# Value normalizers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text, ohms",
    [
        ("10 kOhms", 10_000.0),
        ("10k", 10_000.0),
        ("10K", 10_000.0),
        ("4.7 kOhms", 4_700.0),
        ("4k7", 4_700.0),
        ("4R7", 4.7),
        ("1 MOhms", 1_000_000.0),
        ("100 mOhms", 0.1),
        ("0", 0.0),
        ("470", 470.0),
    ],
)
def test_parse_resistance(text, ohms):
    assert parse_resistance(text) == pytest.approx(ohms)


def test_parse_resistance_agrees_across_renderings():
    assert parse_resistance("10 kOhms") == parse_resistance("10k")


@pytest.mark.parametrize("bad", [None, "", "abc", "NA"])
def test_parse_resistance_unparseable_is_none(bad):
    assert parse_resistance(bad) is None


@pytest.mark.parametrize(
    "text, pct", [("±1%", 1.0), ("1%", 1.0), ("5 %", 5.0), ("0.1%", 0.1)]
)
def test_parse_percent(text, pct):
    assert parse_percent(text) == pytest.approx(pct)


@pytest.mark.parametrize(
    "text, watts",
    [
        ("0.063W, 1/16W", 0.063),   # compound: first token wins
        ("1/16W", 0.0625),
        ("1/4W", 0.25),
        ("0.25W", 0.25),
        ("0.5 Watts", 0.5),
    ],
)
def test_parse_watts(text, watts):
    assert parse_watts(text) == pytest.approx(watts)


# --------------------------------------------------------------------------- #
# confirm_resistor — the field-by-field verdict
# --------------------------------------------------------------------------- #

def _resistor(**kw):
    kw.setdefault("reference", "R1")
    kw.setdefault("dnp", False)
    kw.setdefault("in_bom", True)
    return ResistorIntent(**kw)


def _product(**params):
    return DigiKeyProduct(mpn="RC0402FR-0710KL", parameters=params)


def _outcomes(result):
    return {c.attr: c.outcome for c in result.checks}


def test_all_fields_confirm_when_they_agree():
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
    result = confirm_resistor(intent, product)
    assert result.ok
    assert not result.fills
    assert all(o is Outcome.CONFIRMED for o in _outcomes(result).values())


def test_resistance_mismatch_is_a_conflict():
    intent = _resistor(value="1k")
    product = _product(Resistance="10 kOhms")
    result = confirm_resistor(intent, product)
    assert not result.ok
    assert _outcomes(result)["value"] is Outcome.CONFLICT


def test_underpowered_part_conflicts_on_power_floor():
    # power_min is a floor: a 1/16W part cannot satisfy a 0.25W minimum.
    intent = _resistor(value="10k", power_min="0.25W")
    product = _product(Resistance="10 kOhms", **{"Power (Watts)": "0.063W, 1/16W"})
    result = confirm_resistor(intent, product)
    assert _outcomes(result)["power_min"] is Outcome.CONFLICT
    assert not result.ok


def test_tighter_tolerance_than_required_is_accepted():
    # Author accepts 5%; a 1% part is tighter, so it confirms rather than clashes.
    intent = _resistor(value="10k", tolerance_preferred=["5%"])
    product = _product(Resistance="10 kOhms", Tolerance="±1%")
    result = confirm_resistor(intent, product)
    assert _outcomes(result)["tolerance_preferred"] is Outcome.CONFIRMED


def test_blank_fields_become_fills_with_canonical_values():
    # Only Value given; DigiKey supplies tolerance and power to fill.
    intent = _resistor(value="10k")
    product = _product(
        Resistance="10 kOhms",
        Tolerance="±1%",
        **{"Power (Watts)": "0.063W, 1/16W"},
    )
    result = confirm_resistor(intent, product)
    fills = {c.attr: c.proposed for c in result.fills}
    assert fills == {"tolerance_preferred": "1%", "power_min": "0.063W"}
    assert result.ok  # fills are improvements, not conflicts


def test_missing_digikey_parameter_is_unknown_not_conflict():
    # DigiKey didn't report power; the spec'd power_min can neither confirm nor fill.
    intent = _resistor(value="10k", power_min="0.25W")
    product = _product(Resistance="10 kOhms")
    result = confirm_resistor(intent, product)
    assert _outcomes(result)["power_min"] is Outcome.UNKNOWN
    assert result.ok


def test_package_confirms_from_footprint_name():
    intent = _resistor(value="10k", footprint="Resistor_SMD:R_0402_1005Metric")
    product = _product(
        Resistance="10 kOhms", **{"Supplier Device Package": "0402"}
    )
    assert _outcomes(confirm_resistor(intent, product))["footprint"] is Outcome.CONFIRMED


def test_package_mismatch_is_a_conflict():
    intent = _resistor(value="10k", footprint="Resistor_SMD:R_0603_1608Metric")
    product = _product(
        Resistance="10 kOhms", **{"Supplier Device Package": "0402"}
    )
    assert _outcomes(confirm_resistor(intent, product))["footprint"] is Outcome.CONFLICT


# --------------------------------------------------------------------------- #
# Resolver.confirm — the network-facing wiring
# --------------------------------------------------------------------------- #

class FakeConfirmDigiKey:
    """A DigiKey stub exposing product_for, keyed by DK PN or MPN."""

    def __init__(self, product=None):
        self.product = product
        self.calls = []

    def product_for(self, key):
        self.calls.append(key)
        return self.product

    def mpn_for(self, key):  # unused here, kept for interface parity
        return self.product.mpn if self.product else None


def _record(intent, **props):
    props.setdefault("Reference", "R1")
    return PartRecord("R1", props, BOARD, "uuid-1", intent)


def test_resolver_confirm_looks_up_and_confirms():
    intent = _resistor(value="10k", footprint="Resistor_SMD:R_0402_1005Metric")
    product = _product(
        Resistance="10 kOhms", **{"Supplier Device Package": "0402"}
    )
    dk = FakeConfirmDigiKey(product)
    r = Resolver(digikey=dk)
    result = r.confirm(_record(intent, **{"DigiKey PN": "311-10.0KLRCT-ND"}))
    assert result is not None and result.ok
    assert dk.calls == ["311-10.0KLRCT-ND"]  # DK PN preferred as the lookup key


def test_resolver_confirm_falls_back_to_mpn_key():
    dk = FakeConfirmDigiKey(_product(Resistance="10 kOhms"))
    r = Resolver(digikey=dk)
    r.confirm(_record(_resistor(value="10k"), MPN="RC0402FR-0710KL"))
    assert dk.calls == ["RC0402FR-0710KL"]


def test_resolver_confirm_skips_non_resistor():
    from kicadtools.intent import CapacitorIntent

    cap = CapacitorIntent(reference="C1", dnp=False, in_bom=True, value="100nF")
    dk = FakeConfirmDigiKey(_product(Resistance="10 kOhms"))
    r = Resolver(digikey=dk)
    assert r.confirm(_record(cap, MPN="X")) is None
    assert dk.calls == []  # no lookup for an unsupported type


def test_resolver_confirm_skips_when_no_part_number():
    dk = FakeConfirmDigiKey(_product())
    r = Resolver(digikey=dk)
    assert r.confirm(_record(_resistor(value="10k"), Value="10k")) is None
    assert dk.calls == []


def test_resolver_confirm_none_when_digikey_misses():
    dk = FakeConfirmDigiKey(product=None)
    r = Resolver(digikey=dk)
    assert r.confirm(_record(_resistor(value="10k"), MPN="bogus")) is None
    assert dk.calls == ["bogus"]
