from pathlib import Path

from kicadtools.components import get_components
from kicadtools.intent import CapacitorIntent, ElectricalIntent, ResistorIntent

DATA = Path(__file__).parent / "data"


def test_untyped_resistor_falls_back_to_base_intent():
    # This schematic's R1 carries no custom `Intent:Type` property, so it should
    # be read as a plain (base) ElectricalIntent rather than a ResistorIntent.
    components = get_components(DATA / "resistor_test.kicad_sch")
    r1 = next(c for c in components if c.reference == "R1")
    assert type(r1) is ElectricalIntent
    assert r1.intent_type == "Unknown"


def test_subsheet_instantiated_twice_produces_two_components():
    # A part defined once on the HalfBridges subsheet, instantiated twice via
    # MotorDriver, must appear once per instance.
    components = get_components(DATA / "MotorDriver.kicad_sch")
    caps = [c for c in components if c.reference in ("C7", "C1")]
    assert {c.reference for c in caps} == {"C7", "C1"}


def test_motordriver_component_counts():
    components = get_components(DATA / "MotorDriver.kicad_sch")
    resistors = [c for c in components if isinstance(c, ResistorIntent)]
    capacitors = [c for c in components if isinstance(c, CapacitorIntent)]
    assert len(resistors) == 18
    assert len(capacitors) == 33
