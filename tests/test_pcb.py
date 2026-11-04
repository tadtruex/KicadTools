"""Tests for reading SMD/THT mounting from a .kicad_pcb (kicadtools.pcb)."""
from pathlib import Path

from kicadtools.pcb import (
    SMD,
    THT,
    UNKNOWN,
    parse_footprint_mounting,
    read_mounting_for_schematic,
    sibling_pcb,
)

DATA = Path(__file__).parent / "data"
MINI = DATA / "mini.kicad_pcb"


def test_parse_footprint_mounting_classifies_each_attr():
    m = parse_footprint_mounting(MINI.read_text(encoding="utf-8"))
    assert m == {
        "R1": SMD,          # (attr smd)
        "C1": SMD,          # (attr smd exclude_from_bom dnp) — compound, still SMD
        "J1": THT,          # (attr through_hole)
        "FID1": UNKNOWN,    # (attr board_only …)
        "U1": UNKNOWN,      # no attr node at all
    }


def test_parse_empty_or_junk():
    assert parse_footprint_mounting("(kicad_pcb)") == {}


def test_sibling_pcb_found_and_missing(tmp_path):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    assert sibling_pcb(sch) is None  # no PCB beside it
    (tmp_path / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    assert sibling_pcb(sch) == tmp_path / "board.kicad_pcb"


def test_read_mounting_for_schematic_no_pcb(tmp_path):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    mounting, pcb = read_mounting_for_schematic(sch)
    assert mounting == {} and pcb is None


def test_read_mounting_for_schematic_with_pcb(tmp_path):
    sch = tmp_path / "mini.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    (tmp_path / "mini.kicad_pcb").write_text(MINI.read_text(encoding="utf-8"), encoding="utf-8")
    mounting, pcb = read_mounting_for_schematic(sch)
    assert pcb == tmp_path / "mini.kicad_pcb"
    assert mounting["R1"] == SMD and mounting["J1"] == THT


# --- pad numbers ----------------------------------------------------------

from kicadtools.pcb import Pad, parse_footprint_placements  # noqa: E402

_BOARD = """(kicad_pcb
  (footprint "Package_TO_SOT_SMD:SOT-23-5" (layer "F.Cu") (at 10 20 90)
    (property "Reference" "U1")
    (pad "1" smd roundrect (at -1.1 -0.95) (size 1.3 0.6) (layers "F.Cu"))
    (pad "2" smd roundrect (at -1.1 0) (size 1.3 0.6) (layers "F.Cu"))
    (pad "" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Paste"))
  )
  (footprint "Old:Bare" (at 0 0)
    (property "Reference" "U2")
    (pad 1 smd rect (at 0 0) (size 1 1))
    (pad A2 smd rect (at 1 0) (size 1 1))
  )
)"""


def test_pad_number_parsed_quoted_bare_and_empty():
    placements = parse_footprint_placements(_BOARD)
    assert [p.number for p in placements["U1"].pads] == ["1", "2", ""]
    assert [p.number for p in placements["U2"].pads] == ["1", "A2"]


def test_pad_geometry_unchanged_by_number():
    u1 = parse_footprint_placements(_BOARD)["U1"]
    assert u1.pads[0] == Pad(-1.1, -0.95, 1.3, 0.6, 0.0, "1", "smd")
    assert (u1.x, u1.y, u1.angle, u1.side) == (10.0, 20.0, 90.0, "top")


def test_pad_positional_construction_defaults_number():
    assert Pad(0.0, 0.0, 1.0, 1.0, 0.0).number == ""
