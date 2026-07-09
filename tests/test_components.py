from pathlib import Path

from kicadtools.components import get_components
from kicadtools.intent import (
    CapacitorIntent,
    ConnectorIntent,
    ElectricalIntent,
    LedIntent,
    ResistorIntent,
)

DATA = Path(__file__).parent / "data"


def test_untyped_resistor_is_guessed():
    # This schematic's R1 carries no custom `Intent:Type` property, so its type
    # is inferred from the other properties rather than left unclassified.
    components = get_components(DATA / "resistor_test.kicad_sch")
    r1 = next(c for c in components if c.reference == "R1")
    assert isinstance(r1, ResistorIntent)
    assert r1.intent_guessed is True
    assert r1.guess_basis  # records which signal was used


def test_explicit_intent_type_is_not_guessed():
    # MotorDriver parts carry explicit Intent:Type properties, so they must be
    # classified directly (not flagged as guessed).
    components = get_components(DATA / "MotorDriver.kicad_sch")
    resistors = [c for c in components if isinstance(c, ResistorIntent)]
    assert resistors
    assert all(r.intent_guessed is False for r in resistors)


def test_subsheet_instantiated_twice_produces_two_components():
    # A part defined once on the HalfBridges subsheet, instantiated twice via
    # MotorDriver, must appear once per instance.
    components = get_components(DATA / "MotorDriver.kicad_sch")
    caps = [c for c in components if c.reference in ("C7", "C1")]
    assert {c.reference for c in caps} == {"C7", "C1"}


def test_guessed_connector_is_classified():
    # J13 (QwiicH) carries no explicit Intent:Type, but its Connector_* footprint
    # is enough to guess it — and there is now a ConnectorIntent to receive it.
    components = get_components(DATA / "MotorDriver.kicad_sch")
    j13 = next(c for c in components if c.reference == "J13")
    assert isinstance(j13, ConnectorIntent)
    assert j13.intent_guessed is True
    assert j13.classification_note is None  # no longer "no classifier yet"
    assert j13.positions == 4


def test_rgb_led_on_custom_footprint_is_classified():
    # U5 is a "1515 rgb led": custom footprint (no signal) and a U refdes that
    # would otherwise read as an IC — the LED text in its Value rescues it.
    components = get_components(DATA / "MotorDriver.kicad_sch")
    u5 = next(c for c in components if c.reference == "U5")
    assert isinstance(u5, LedIntent)
    assert u5.intent_guessed is True
    assert u5.color == "rgb"
    assert u5.classification_note is None


def test_motordriver_component_counts():
    components = get_components(DATA / "MotorDriver.kicad_sch")
    resistors = [c for c in components if isinstance(c, ResistorIntent)]
    capacitors = [c for c in components if isinstance(c, CapacitorIntent)]
    assert len(resistors) == 18
    assert len(capacitors) == 33
