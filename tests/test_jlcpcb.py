"""Tests for the JLCPCB BOM export (kicadtools.jlcpcb + the `jlcpcb` CLI)."""
import io
from pathlib import Path

import pytest

from kicadtools import cli
from kicadtools.jlcpcb import (
    JLC_FIELDS,
    build_jlcpcb_bom,
    jlcpcb_bom_from_schematic,
    missing_lcsc,
    undetermined_references,
    write_jlcpcb_csv,
)
from kicadtools.pcb import SMD, THT, UNKNOWN
from kicadtools.resolve import PartRecord, iter_bom_part_records

DATA = Path(__file__).parent / "data"
BOARD = DATA / "MotorDriver.kicad_sch"
FLAT_BOARD = DATA / "ECG.kicad_sch"  # flat (no subsheets) — safe to copy standalone


def _rec(ref, **props):
    props.setdefault("Reference", ref)
    return PartRecord(ref, props, BOARD, f"uuid-{ref}")


# ---- grouping ---------------------------------------------------------

def test_groups_identical_parts_into_one_line():
    lines = build_jlcpcb_bom([
        _rec("R1", Value="10k", Footprint="R_0603", LCSC="C1"),
        _rec("R2", Value="10k", Footprint="R_0603", LCSC="C1"),
    ])
    assert len(lines) == 1
    assert lines[0].designator == "R1,R2"
    assert lines[0].quantity == 2


def test_designators_natural_sorted():
    lines = build_jlcpcb_bom([
        _rec("C10", Value="1u", Footprint="C_0402", LCSC="C1"),
        _rec("C2", Value="1u", Footprint="C_0402", LCSC="C1"),
        _rec("C1", Value="1u", Footprint="C_0402", LCSC="C1"),
    ])
    assert len(lines) == 1
    assert lines[0].designator == "C1,C2,C10"  # C2 before C10


def test_different_lcsc_splits_lines():
    lines = build_jlcpcb_bom([
        _rec("R1", Value="10k", Footprint="R_0603", LCSC="C1"),
        _rec("R2", Value="10k", Footprint="R_0603", LCSC="C2"),
    ])
    assert len(lines) == 2  # one LCSC per line — must not collapse


def test_blank_and_valued_lcsc_split():
    lines = build_jlcpcb_bom([
        _rec("R1", Value="10k", Footprint="R_0603", LCSC="C1"),
        _rec("R2", Value="10k", Footprint="R_0603"),  # no LCSC
    ])
    assert len(lines) == 2
    assert any(ln.lcsc == "" for ln in lines)


def test_oversized_group_splits_at_200_designators():
    # JLCPCB rejects >200 designators in one cell; a 450-part group -> 200+200+50.
    recs = [_rec(f"R{i}", Value="10k", Footprint="R_0603", LCSC="C1") for i in range(450)]
    lines = build_jlcpcb_bom(recs)
    assert len(lines) == 3
    assert [ln.quantity for ln in sorted(lines, key=lambda l: -l.quantity)] == [200, 200, 50]
    # Every original ref appears exactly once across all lines, no duplicates.
    all_refs = [r for ln in lines for r in ln.references]
    assert len(all_refs) == 450
    assert len(set(all_refs)) == 450
    assert all(ln.lcsc == "C1" for ln in lines)


def test_non_lcsc_partnumber_is_blanked():
    # An MPN mistakenly sitting in the LCSC field must never reach the export.
    lines = build_jlcpcb_bom([_rec("U1", Value="MCU", Footprint="QFN", LCSC="STM32F103")])
    assert lines[0].lcsc == ""


def test_lcsc_code_is_uppercased():
    lines = build_jlcpcb_bom([_rec("R1", Value="10k", Footprint="R_0603", LCSC="c123")])
    assert lines[0].lcsc == "C123"


def test_missing_lcsc_blank_not_dropped():
    lines = build_jlcpcb_bom([_rec("R1", Value="10k", Footprint="R_0603")])
    assert len(lines) == 1
    assert lines[0].lcsc == ""
    assert missing_lcsc(lines) == lines


def test_comment_is_value_and_footprint_passthrough():
    (ln,) = build_jlcpcb_bom([_rec("U1", Value="STM32", Footprint="LQFP-48", LCSC="C1")])
    assert ln.comment == "STM32"
    assert ln.footprint == "LQFP-48"


def test_lcsc_alias_read():
    # extract_part_numbers tolerates variant spellings of the LCSC key: any that
    # normalize to one of its aliases (here "LCSC PN" -> "lcscpn").
    (ln,) = build_jlcpcb_bom([_rec("R1", Value="10k", Footprint="R_0603",
                                   **{"LCSC PN": "C17497"})])
    assert ln.lcsc == "C17497"


def test_lcsc_override_used():
    recs = [
        _rec("R1", Value="10k", Footprint="R_0603"),           # no LCSC on disk
        _rec("R2", Value="10k", Footprint="R_0603"),           # identical
    ]
    lines = build_jlcpcb_bom(recs, {"R1": "C999"})
    # The override gives R1 an LCSC, splitting it from the still-blank R2.
    assert len(lines) == 2
    r1 = next(ln for ln in lines if ln.references == ("R1",))
    assert r1.lcsc == "C999"


def test_line_ordering_deterministic():
    recs = [
        _rec("C10", Value="1u", Footprint="C_0402", LCSC="C1"),
        _rec("R1", Value="10k", Footprint="R_0603", LCSC="C2"),
        _rec("C2", Value="2u", Footprint="C_0402", LCSC="C3"),
    ]
    order1 = [ln.designator for ln in build_jlcpcb_bom(recs)]
    order2 = [ln.designator for ln in build_jlcpcb_bom(list(reversed(recs)))]
    assert order1 == order2  # sorted by natural_key of first designator


# ---- SMD-only filtering ----------------------------------------------

def _mixed_records():
    return [
        _rec("R1", Value="10k", Footprint="R_0402", LCSC="C1"),
        _rec("J1", Value="hdr", Footprint="PinHeader", LCSC="C2"),
        _rec("U1", Value="IC", Footprint="Custom"),
    ]


_MOUNTING = {"R1": SMD, "J1": THT, "U1": UNKNOWN}


def test_smd_only_keeps_only_smd():
    lines = build_jlcpcb_bom(_mixed_records(), mounting=_MOUNTING, smd_only=True)
    assert [ln.references for ln in lines] == [("R1",)]


def test_without_smd_only_keeps_everything():
    lines = build_jlcpcb_bom(_mixed_records(), mounting=_MOUNTING, smd_only=False)
    assert len(lines) == 3


def test_lines_carry_mounting():
    lines = build_jlcpcb_bom(_mixed_records(), mounting=_MOUNTING)
    by_ref = {ln.references[0]: ln.mounting for ln in lines}
    assert by_ref == {"R1": SMD, "J1": THT, "U1": UNKNOWN}


def test_smd_only_with_no_mounting_excludes_all():
    # No PCB → empty mounting map → every part is undetermined → nothing kept.
    assert build_jlcpcb_bom(_mixed_records(), mounting={}, smd_only=True) == []


def test_undetermined_references_lists_unknown_not_tht():
    # Only U1 (unknown) is "undetermined"; J1 (tht) is correctly excluded, not listed.
    assert undetermined_references(_mixed_records(), _MOUNTING) == ["U1"]
    # With no mounting at all, every part is undetermined (sorted naturally).
    assert undetermined_references(_mixed_records(), {}) == ["J1", "R1", "U1"]


# ---- CSV writing ------------------------------------------------------

def test_write_csv_header_and_quoting():
    lines = build_jlcpcb_bom([
        _rec("R1", Value="10k", Footprint="R_0603", LCSC="C1"),
        _rec("R2", Value="10k", Footprint="R_0603", LCSC="C1"),
        _rec("R3", Value="10k", Footprint="R_0603", LCSC="C1"),
    ])
    buf = io.StringIO()
    write_jlcpcb_csv(lines, buf)
    text = buf.getvalue()
    first = text.splitlines()[0]
    assert first == "Comment,Designator,Footprint,LCSC Part #"
    assert JLC_FIELDS == ["Comment", "Designator", "Footprint", "LCSC Part #"]
    assert '"R1,R2,R3"' in text  # comma-bearing cell is quoted


# ---- real board -------------------------------------------------------

def test_build_from_real_board():
    lines = jlcpcb_bom_from_schematic(BOARD)
    assert lines
    # Every placed BOM part is accounted for exactly once.
    assert sum(ln.quantity for ln in lines) == len(iter_bom_part_records(BOARD))
    assert any(ln.lcsc for ln in lines)


# ---- CLI --------------------------------------------------------------

def test_cli_writes_csv_file(tmp_path, capsys):
    out = tmp_path / "bom.csv"
    cli.main(["jlcpcb", str(BOARD), "-o", str(out)])
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "Comment,Designator,Footprint,LCSC Part #"
    assert capsys.readouterr().out == ""  # nothing on stdout when -o is given


def test_cli_stdout(capsys):
    cli.main(["jlcpcb", str(BOARD)])
    out = capsys.readouterr().out
    assert out.startswith("Comment,Designator,Footprint,LCSC Part #")


def test_cli_exit_code_tracks_missing(tmp_path, capsys):
    out = tmp_path / "bom.csv"
    expected = 1 if missing_lcsc(jlcpcb_bom_from_schematic(BOARD)) else 0
    rc = cli.main(["jlcpcb", str(BOARD), "-o", str(out)])
    assert rc == expected
    if expected == 1:
        assert "no LCSC Part #" in capsys.readouterr().err


@pytest.fixture
def no_standard_libs(tmp_path, monkeypatch):
    """Point the standard footprint library fallback at an empty folder, so a
    part not on the synthetic board stays of undetermined mounting instead of
    being read from this repo's Libraries/kicad-footprints."""
    monkeypatch.setenv("KICADTOOLS_REPO_ROOT", str(tmp_path / "no_repo"))


def _write_pcb(path, entries):
    """Write a minimal .kicad_pcb with (ref, attr-first-token) footprint nodes."""
    nodes = "\n".join(
        f'\t(footprint "L:F"\n\t\t(at 0 0)\n\t\t(property "Reference" "{ref}")\n\t\t(attr {attr})\n\t)'
        for ref, attr in entries
    )
    path.write_text(f"(kicad_pcb\n{nodes}\n)\n", encoding="utf-8")


def test_cli_smd_filters_by_sibling_pcb(tmp_path, capsys, no_standard_libs):
    # A schematic copy with a synthetic sibling PCB: two of its refs marked smd,
    # one through_hole, the rest absent (→ undetermined → excluded + warned).
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")
    refs = [r.reference for r in iter_bom_part_records(sch)]
    smd_refs, tht_ref = refs[:2], refs[2]
    _write_pcb(
        tmp_path / "board.kicad_pcb",
        [(smd_refs[0], "smd"), (smd_refs[1], "smd"), (tht_ref, "through_hole")],
    )

    out = tmp_path / "smd.csv"
    cli.main(["jlcpcb", str(sch), "--smd", "-o", str(out)])
    import csv as _csv
    with open(out, encoding="utf-8") as f:
        designators = set()
        for row in _csv.DictReader(f):
            designators.update(row["Designator"].split(","))
    for ref in smd_refs:
        assert ref in designators     # SMD parts kept
    assert tht_ref not in designators  # through-hole excluded
    err = capsys.readouterr().err
    assert "undetermined mounting" in err  # the absent refs are reported


def test_cli_smd_no_pcb_warns_and_empties(tmp_path, capsys, no_standard_libs):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")  # no sibling PCB
    out = tmp_path / "smd.csv"
    cli.main(["jlcpcb", str(sch), "--smd", "-o", str(out)])
    assert out.read_text(encoding="utf-8").splitlines() == [
        "Comment,Designator,Footprint,LCSC Part #"
    ]  # header only — nothing determinable
    assert "no board.kicad_pcb" in capsys.readouterr().err
