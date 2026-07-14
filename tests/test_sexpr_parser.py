from pathlib import Path

from kicadtools.sexpr_parser import has_instantiated_sheets

DATA = Path(__file__).parent / "data"


def test_hierarchical_schematic_has_instantiated_sheets():
    # MotorDriver instantiates HalfBridges.kicad_sch as a subsheet.
    assert has_instantiated_sheets(DATA / "MotorDriver.kicad_sch") is True


def test_flat_schematic_has_no_instantiated_sheets():
    assert has_instantiated_sheets(DATA / "resistor_test.kicad_sch") is False
    assert has_instantiated_sheets(DATA / "ECG.kicad_sch") is False
