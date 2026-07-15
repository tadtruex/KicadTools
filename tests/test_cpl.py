"""Tests for the JLCPCB CPL / pick-and-place export (kicadtools.cpl + the `cpl` CLI)."""
import io
from pathlib import Path

from kicadtools import cli
from kicadtools.cpl import (
    CPL_FIELDS,
    CplLine,
    build_cpl,
    write_cpl_csv,
)
from kicadtools.pcb import (
    Pad,
    Placement,
    parse_footprint_placements,
    read_aux_origin,
)
from kicadtools.rotations import RotationDb, default_rotation_db

DATA = Path(__file__).parent / "data"
FLAT_BOARD = DATA / "ECG.kicad_sch"

# A resistor's two symmetric pads: centroid sits exactly on the anchor.
_R_PADS = (Pad(-0.825, 0.0, 0.8, 0.95, 0.0), Pad(0.825, 0.0, 0.8, 0.95, 0.0))


def _placement(ref="R1", x=100.0, y=50.0, angle=0.0, side="top",
               footprint="Resistor_SMD:R_0603_1608Metric", pads=_R_PADS):
    return Placement(ref, footprint, x, y, angle, side, pads)


# ---- centroid / coordinates ------------------------------------------

def test_symmetric_centroid_is_the_anchor():
    line = build_cpl([_placement(x=100.0, y=50.0)])[0]
    assert round(line.mid_x, 6) == 100.0
    assert round(line.mid_y, 6) == -50.0  # Y is inverted (KiCad down -> JLCPCB up)


def test_aux_origin_is_subtracted():
    line = build_cpl([_placement(x=100.0, y=50.0)], aux_origin=(10.0, 20.0))[0]
    assert round(line.mid_x, 6) == 90.0
    assert round(line.mid_y, 6) == -30.0  # -(50 - 20)


def test_centroid_survives_rotation_for_symmetric_part():
    # Rotating a symmetric footprint doesn't move its centroid off the anchor.
    line = build_cpl([_placement(x=7.0, y=3.0, angle=90.0)])[0]
    assert round(line.mid_x, 6) == 7.0
    assert round(line.mid_y, 6) == -3.0


def test_no_pads_falls_back_to_anchor():
    line = build_cpl([_placement(pads=())])[0]
    assert round(line.mid_x, 6) == 100.0


# ---- rotation / layer ------------------------------------------------

def test_top_layer_rotation_is_the_footprint_angle():
    line = build_cpl([_placement(angle=90.0)])[0]
    assert line.layer == "Top"
    assert line.rotation == 90.0


def test_bottom_layer_flips_rotation_and_labels_bottom():
    line = build_cpl([_placement(angle=45.0, side="bottom")])[0]
    assert line.layer == "Bottom"
    assert line.rotation == 135.0  # (180 - 45) % 360


def test_rotation_normalized_into_0_360():
    line = build_cpl([_placement(angle=-90.0)])[0]
    assert line.rotation == 270.0


def test_family_offset_applied():
    # SOIC-8 is in the built-in (both-tools-agree) table at +270.
    p = _placement(footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", angle=0.0)
    assert build_cpl([p])[0].rotation == 270.0


def test_manual_rotation_override_added():
    line = build_cpl([_placement(angle=0.0)], manual_rotation={"R1": 90.0})[0]
    assert line.rotation == 90.0


# ---- include filter / ordering ---------------------------------------

def test_include_filters_to_bom_refs():
    lines = build_cpl(
        [_placement("R1"), _placement("R2"), _placement("R3")],
        include={"R1", "R3"},
    )
    assert sorted(ln.designator for ln in lines) == ["R1", "R3"]


def test_lines_natural_sorted():
    lines = build_cpl([_placement("R10"), _placement("R2"), _placement("R1")])
    assert [ln.designator for ln in lines] == ["R1", "R2", "R10"]


# ---- rotation DB -----------------------------------------------------

def test_rotation_db_strips_library_prefix_and_defaults_zero():
    db = default_rotation_db()
    assert db.offset("Package_SO:SOIC-8_x") == 270.0
    assert db.offset("Resistor_SMD:R_0603_1608Metric") == 0.0  # not in table
    assert db.offset("") == 0.0


def test_custom_rotation_db():
    db = RotationDb([(r"^WIDGET", 42.0)])
    assert db.offset("Lib:WIDGET-3") == 42.0
    assert db.offset("Lib:OTHER") == 0.0


# ---- parsing a .kicad_pcb --------------------------------------------

_MINI_PCB = """
(kicad_pcb
  (setup (aux_axis_origin 10 20))
  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (property "Reference" "R8")
    (at 100 50 90)
    (attr smd)
    (pad "1" smd roundrect (at -0.825 0 90) (size 0.8 0.95) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.825 0 90) (size 0.8 0.95) (layers "F.Cu"))
  )
  (footprint "Package_TO_SOT_SMD:SOT-23"
    (layer "B.Cu")
    (property "Reference" "Q1")
    (at 30 40 45)
    (attr smd)
    (pad "1" smd rect (at -1 -0.95) (size 0.6 0.7) (layers "B.Cu"))
    (pad "3" smd rect (at 1 0) (size 0.6 0.7) (layers "B.Cu"))
  )
)
"""


def test_parse_footprint_placements():
    plc = parse_footprint_placements(_MINI_PCB)
    assert set(plc) == {"R8", "Q1"}
    r8 = plc["R8"]
    assert (r8.x, r8.y, r8.angle) == (100.0, 50.0, 90.0)
    assert r8.side == "top"
    assert r8.footprint == "Resistor_SMD:R_0603_1608Metric"
    assert len(r8.pads) == 2
    assert plc["Q1"].side == "bottom"
    # angle defaults to 0 is exercised elsewhere; here Q1 has an explicit 45.
    assert plc["Q1"].angle == 45.0


def test_read_aux_origin():
    assert read_aux_origin(_MINI_PCB) == (10.0, 20.0)
    assert read_aux_origin("(kicad_pcb)") == (0.0, 0.0)  # none set -> board origin


def test_angle_absent_defaults_to_zero():
    pcb = (
        '(kicad_pcb (footprint "L:F" (layer "F.Cu") (property "Reference" "U1") '
        '(at 5 6) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))))'
    )
    assert parse_footprint_placements(pcb)["U1"].angle == 0.0


# ---- CSV output ------------------------------------------------------

def test_csv_header_and_bare_mm_and_layer():
    buf = io.StringIO()
    write_cpl_csv([CplLine("R1", 12.3456, -7.0, "Top", 270.0)], buf)
    rows = buf.getvalue().splitlines()
    assert rows[0] == ",".join(CPL_FIELDS)
    # bare mm number (no "mm" suffix), 4 decimals; integer rotation stays integer.
    assert rows[1] == "R1,12.3456,-7.0000,Top,270"


def test_fractional_rotation_formatting():
    buf = io.StringIO()
    write_cpl_csv([CplLine("R1", 0.0, 0.0, "Top", 22.5)], buf)
    assert buf.getvalue().splitlines()[1].endswith("22.5")


# ---- CLI -------------------------------------------------------------

def _sibling_pcb_for_ecg(tmp_path):
    """A schematic copy of the (flat) ECG board with a synthetic sibling PCB
    that places two of its real BOM refs (R8, C1)."""
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "board.kicad_pcb").write_text(
        '(kicad_pcb'
        ' (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu")'
        '   (property "Reference" "R8") (at 130 100 0) (attr smd)'
        '   (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu"))'
        '   (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu")))'
        ' (footprint "Capacitor_SMD:C_0603_1608Metric" (layer "F.Cu")'
        '   (property "Reference" "C1") (at 140 40 90) (attr smd)'
        '   (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu"))'
        '   (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu"))))',
        encoding="utf-8",
    )
    return sch


def test_cli_writes_cpl_file(tmp_path, capsys):
    sch = _sibling_pcb_for_ecg(tmp_path)
    out = tmp_path / "cpl.csv"
    rc = cli.main(["cpl", str(sch), "-o", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(CPL_FIELDS)
    assert any(row.startswith("R8,") for row in text.splitlines())
    assert any(row.startswith("C1,") for row in text.splitlines())
    # BOM parts without a placed footprint are reported, not silently dropped.
    assert "no footprint on the board" in capsys.readouterr().err


def test_cli_no_pcb_warns(tmp_path, capsys):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")  # no sibling PCB
    rc = cli.main(["cpl", str(sch)])
    assert rc == 1
    assert "no board.kicad_pcb" in capsys.readouterr().err
