"""Tests for mounting per part (kicadtools.mounting, THT_PLAN.md step 1)."""
import csv
from pathlib import Path
from types import SimpleNamespace

from kicadtools import cli
from kicadtools.mounting import (
    BOARD,
    LIBRARY,
    NOT_FOUND,
    BoardMounting,
    decide,
    mounting_for_records,
    mounting_kinds,
)
from kicadtools.pcb import SMD, THT, UNKNOWN, Pad
from kicadtools.resolve import iter_bom_part_records

DATA = Path(__file__).parent / "data"
FLAT_BOARD = DATA / "ECG.kicad_sch"


def _pads(**kinds):
    """Pads of the given types: ``_pads(smd=2, thru_hole=1)``."""
    out = []
    for kind, n in kinds.items():
        out += [Pad(0, 0, 1, 1, 0, str(len(out) + 1), kind) for _ in range(n)]
    return out


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _mod(name, attr, *pad_kinds):
    attr_node = f"(attr {attr})" if attr else ""
    pads = "\n".join(
        f'  (pad "{i + 1}" {k} circle (at {i} 0) (size 1 1) (layers "*.Cu"))'
        for i, k in enumerate(pad_kinds)
    )
    return f'(footprint "{name}" (layer "F.Cu")\n  {attr_node}\n{pads}\n)\n'


def _board(*footprints):
    """A .kicad_pcb from (ref, lib_id, attr, pad kinds…) tuples."""
    nodes = []
    for ref, lib_id, attr, *kinds in footprints:
        pads = "\n".join(
            f'\t\t(pad "{i + 1}" {k} circle (at {i} 0) (size 1 1) (layers "*.Cu"))'
            for i, k in enumerate(kinds)
        )
        attr_node = f"(attr {attr})" if attr else ""
        nodes.append(
            f'\t(footprint "{lib_id}"\n\t\t(layer "F.Cu")\n\t\t(at 0 0)\n'
            f'\t\t(property "Reference" "{ref}")\n\t\t{attr_node}\n{pads}\n\t)'
        )
    return "(kicad_pcb\n" + "\n".join(nodes) + "\n)\n"


def _record(ref, footprint):
    return SimpleNamespace(reference=ref, props={"Footprint": footprint})


# --- decide -----------------------------------------------------------------


def test_pads_of_one_type_agree_with_attr():
    m = decide(THT, _pads(thru_hole=4), BOARD)
    assert (m.kind, m.source, m.thru_hole_pads, m.smd_pads, m.note) == (THT, BOARD, 4, 0, "")
    assert decide(SMD, _pads(smd=2), LIBRARY).kind == SMD


def test_no_attr_pads_decide():
    # KiCad's standard through-hole library footprints carry no attr.
    m = decide(UNKNOWN, _pads(thru_hole=7), LIBRARY)
    assert (m.kind, m.note) == (THT, "")
    assert decide(UNKNOWN, _pads(smd=3), LIBRARY).kind == SMD


def test_pads_win_over_contrary_attr_with_note():
    # Breakout U6: attr through_hole, nine SMD pads.
    m = decide(THT, _pads(smd=9), BOARD)
    assert m.kind == SMD
    assert m.attr == THT
    assert m.note == "footprint attr is through-hole, pads are all SMD"
    m = decide(SMD, _pads(thru_hole=2), BOARD)
    assert (m.kind, m.note) == (THT, "footprint attr is SMD, pads are all through-hole")


def test_mixed_pads_follow_attr():
    # USB connectors: SMD signal pads, plated shield tabs, attr smd.
    m = decide(SMD, _pads(smd=5, thru_hole=4, np_thru_hole=2), BOARD)
    assert (m.kind, m.note) == (SMD, "")
    m = decide(UNKNOWN, _pads(smd=5, thru_hole=4), LIBRARY)
    assert (m.kind, m.note) == (UNKNOWN, "no attr, 5 SMD and 4 through-hole pads")


def test_no_plated_pads_keep_attr():
    assert decide(UNKNOWN, _pads(np_thru_hole=1), LIBRARY).kind == UNKNOWN
    assert decide(SMD, _pads(connect=2), BOARD).kind == SMD
    assert decide(THT, [], BOARD).kind == THT


# --- BoardMounting ----------------------------------------------------------


def _project(tmp_path):
    """A board folder with an fp-lib-table, one board library and a standard
    library folder found above the board (default_standard_dir)."""
    board = tmp_path / "board"
    _write(
        board / "fp-lib-table",
        '(fp_lib_table (lib (name "Local")(type "KiCad")(uri "${KIPRJMOD}/local.pretty")))\n',
    )
    _write(board / "local.pretty/Rohm.kicad_mod", _mod("Rohm", "through_hole", *["smd"] * 9))
    std = tmp_path / "Libraries" / "kicad-footprints"
    _write(
        std / "Connector_PinHeader_2.54mm.pretty/PinHeader_1x04.kicad_mod",
        _mod("PinHeader_1x04", "", *["thru_hole"] * 4),
    )
    _write(std / "Resistor_SMD.pretty/R_0603.kicad_mod", _mod("R_0603", "smd", "smd", "smd"))
    return board


def test_board_copy_first(tmp_path):
    board = _project(tmp_path)
    _write(
        board / "b.kicad_pcb",
        _board(("J1", "Connector_PinHeader_2.54mm:PinHeader_1x04", "through_hole", *["thru_hole"] * 4)),
    )
    bm = BoardMounting.for_schematic(_write(board / "b.kicad_sch", "(kicad_sch)"))
    m = bm.mounting("J1", "Connector_PinHeader_2.54mm:PinHeader_1x04")
    assert (m.kind, m.source, m.attr, m.note) == (THT, BOARD, THT, "")


def test_board_copy_used_when_name_differs_with_note(tmp_path):
    # Boards updated from KiCad 5 libraries hold the renamed footprint.
    board = _project(tmp_path)
    _write(board / "b.kicad_pcb", _board(("D1", "LED_SMD:LED_0603_1608Metric", "smd", "smd", "smd")))
    bm = BoardMounting.for_schematic(_write(board / "b.kicad_sch", "(kicad_sch)"))
    m = bm.mounting("D1", "LEDs:LED_0603")
    assert (m.kind, m.source) == (SMD, BOARD)
    assert m.note == "board footprint LED_SMD:LED_0603_1608Metric differs from schematic LEDs:LED_0603"


def test_notes_combine(tmp_path):
    board = _project(tmp_path)
    _write(board / "b.kicad_pcb", _board(("U6", "Rohm:BD33", "through_hole", *["smd"] * 9)))
    bm = BoardMounting.for_schematic(_write(board / "b.kicad_sch", "(kicad_sch)"))
    m = bm.mounting("U6", "Local:Rohm")
    assert m.kind == SMD
    assert m.note == (
        "footprint attr is through-hole, pads are all SMD; "
        "board footprint Rohm:BD33 differs from schematic Local:Rohm"
    )


def test_library_when_not_on_board(tmp_path):
    board = _project(tmp_path)  # no .kicad_pcb
    bm = BoardMounting.for_schematic(_write(board / "b.kicad_sch", "(kicad_sch)"))
    assert bm.footprints.pcb_path is None
    m = bm.mounting("J2", "Connector_PinHeader_2.54mm:PinHeader_1x04")
    assert (m.kind, m.source, m.attr, m.thru_hole_pads) == (THT, LIBRARY, UNKNOWN, 4)
    m = bm.mounting("U6", "Local:Rohm")
    assert (m.kind, m.source, m.note) == (SMD, LIBRARY, "footprint attr is through-hole, pads are all SMD")
    assert bm.mounting("R1", "Resistor_SMD:R_0603").kind == SMD


def test_not_found(tmp_path):
    board = _project(tmp_path)
    bm = BoardMounting.for_schematic(_write(board / "b.kicad_sch", "(kicad_sch)"))
    assert bm.mounting("U9", "Local:Missing") == NOT_FOUND
    assert bm.mounting("U9", None) == NOT_FOUND
    assert bm.mounting_for(_record("U9", "")) == NOT_FOUND


def test_mounting_for_records_and_kinds(tmp_path):
    board = _project(tmp_path)
    _write(board / "b.kicad_pcb", _board(("R1", "Resistor_SMD:R_0603", "smd", "smd", "smd")))
    sch = _write(board / "b.kicad_sch", "(kicad_sch)")
    records = [
        _record("R1", "Resistor_SMD:R_0603"),
        _record("J2", "Connector_PinHeader_2.54mm:PinHeader_1x04"),
        _record("U9", "Local:Missing"),
    ]
    mountings, pcb = mounting_for_records(records, sch)
    assert pcb == board / "b.kicad_pcb"
    assert {r: m.source for r, m in mountings.items()} == {"R1": BOARD, "J2": LIBRARY, "U9": ""}
    kinds, _ = mounting_kinds(records, sch)
    assert kinds == {"R1": SMD, "J2": THT, "U9": UNKNOWN}


# --- exports read the library when there is no board -------------------------


def test_cli_smd_bom_without_board_uses_library(tmp_path, capsys):
    """No .kicad_pcb: a part whose library footprint is SMD is kept in the
    --smd BOM; parts whose footprint is not found are left out and listed."""
    sch = tmp_path / "board" / "board.kicad_sch"
    _write(sch, FLAT_BOARD.read_text(encoding="utf-8"))
    records = iter_bom_part_records(sch)
    rec = next(r for r in records if ":" in str(r.props.get("Footprint") or ""))
    lib, name = str(rec.props["Footprint"]).split(":", 1)
    _write(
        tmp_path / "Libraries" / "kicad-footprints" / f"{lib}.pretty" / f"{name}.kicad_mod",
        _mod(name, "smd", "smd", "smd"),
    )
    same = {r.reference for r in records if r.props.get("Footprint") == rec.props["Footprint"]}

    out = tmp_path / "smd.csv"
    cli.main(["jlcpcb", str(sch), "--smd", "-o", str(out)])
    with open(out, encoding="utf-8") as f:
        designators = set()
        for row in csv.DictReader(f):
            designators.update(row["Designator"].split(","))
    assert designators == same
    err = capsys.readouterr().err
    assert "mounting is read from the library footprints instead" in err
    assert "undetermined mounting" in err
