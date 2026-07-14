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
