"""Tests for the DigiKey BOM export (kicadtools.digikey_bom + the `digikey` CLI)."""
import csv as _csv
import io
from pathlib import Path

import pytest

from kicadtools import cli
from kicadtools.digikey_bom import (
    BOTH,
    DK_FIELDS,
    NON_SMD,
    SMD_ONLY,
    build_digikey_bom,
    digikey_bom_from_schematic,
    unorderable_lines,
    write_digikey_csv,
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
    lines = build_digikey_bom([
        _rec("R1", Value="10k", Footprint="R_0603", DigiKeyPN="311-10.0KLRCT-ND"),
        _rec("R2", Value="10k", Footprint="R_0603", DigiKeyPN="311-10.0KLRCT-ND"),
    ])
    assert len(lines) == 1
    assert lines[0].customer_reference == "R1,R2"
    assert lines[0].quantity == 2


def test_designators_natural_sorted():
    lines = build_digikey_bom([
        _rec("C10", Value="1u", Footprint="C_0402", DigiKeyPN="DK1"),
        _rec("C2", Value="1u", Footprint="C_0402", DigiKeyPN="DK1"),
        _rec("C1", Value="1u", Footprint="C_0402", DigiKeyPN="DK1"),
    ])
    assert len(lines) == 1
    assert lines[0].customer_reference == "C1,C2,C10"  # C2 before C10


def test_different_partnumber_splits_lines():
    lines = build_digikey_bom([
        _rec("R1", Value="10k", Footprint="R_0603", DigiKeyPN="DK1"),
        _rec("R2", Value="10k", Footprint="R_0603", DigiKeyPN="DK2"),
    ])
    assert len(lines) == 2  # one DigiKey number per line — must not collapse


def test_blank_and_valued_partnumber_split():
    lines = build_digikey_bom([
        _rec("R1", Value="10k", Footprint="R_0603", DigiKeyPN="DK1"),
        _rec("R2", Value="10k", Footprint="R_0603"),  # no part number
    ])
    assert len(lines) == 2
    assert any(not ln.orderable for ln in lines)


def test_distinct_blank_partnumber_parts_do_not_merge():
    # Two different parts, both lacking any part number, must stay separate so
    # their quantities are meaningful — Value/Footprint keep them apart.
    lines = build_digikey_bom([
        _rec("R1", Value="10k", Footprint="R_0603"),
        _rec("C1", Value="1u", Footprint="C_0402"),
    ])
    assert len(lines) == 2
    assert all(not ln.orderable for ln in lines)


def test_mpn_carried_and_used_when_no_digikey_pn():
    (ln,) = build_digikey_bom([_rec("U1", Value="MCU", Footprint="QFN", MPN="STM32F103")])
    assert ln.digikey_pn == ""
    assert ln.mpn == "STM32F103"
    assert ln.orderable  # an MPN alone is enough for DigiKey to match


def test_both_partnumbers_emitted():
    (ln,) = build_digikey_bom([
        _rec("U1", Value="MCU", Footprint="QFN", DigiKeyPN="497-6063-ND", MPN="STM32F103"),
    ])
    assert ln.digikey_pn == "497-6063-ND"
    assert ln.mpn == "STM32F103"


def test_partnumber_alias_read():
    # extract_part_numbers tolerates variant spellings: "Digi-Key PN" -> digikeypn.
    (ln,) = build_digikey_bom([_rec("R1", Value="10k", Footprint="R_0603",
                                    **{"Digi-Key PN": "311-10.0KLRCT-ND"})])
    assert ln.digikey_pn == "311-10.0KLRCT-ND"


def test_unorderable_lines_reported_not_dropped():
    lines = build_digikey_bom([_rec("R1", Value="10k", Footprint="R_0603")])
    assert len(lines) == 1
    assert not lines[0].orderable
    assert unorderable_lines(lines) == lines


def test_line_ordering_deterministic():
    recs = [
        _rec("C10", Value="1u", Footprint="C_0402", DigiKeyPN="DK1"),
        _rec("R1", Value="10k", Footprint="R_0603", DigiKeyPN="DK2"),
        _rec("C2", Value="2u", Footprint="C_0402", DigiKeyPN="DK3"),
    ]
    order1 = [ln.customer_reference for ln in build_digikey_bom(recs)]
    order2 = [ln.customer_reference for ln in build_digikey_bom(list(reversed(recs)))]
    assert order1 == order2  # sorted by natural_key of first designator


# ---- mounting filtering ----------------------------------------------

def _mixed_records():
    return [
        _rec("R1", Value="10k", Footprint="R_0402", DigiKeyPN="DK1"),
        _rec("J1", Value="hdr", Footprint="PinHeader", DigiKeyPN="DK2"),
        _rec("U1", Value="IC", Footprint="Custom", DigiKeyPN="DK3"),
    ]


_MOUNTING = {"R1": SMD, "J1": THT, "U1": UNKNOWN}


def test_smd_only_keeps_only_smd():
    lines = build_digikey_bom(_mixed_records(), mounting=_MOUNTING, mounting_filter=SMD_ONLY)
    assert [ln.references for ln in lines] == [("R1",)]


def test_non_smd_keeps_tht_and_unknown():
    # "non-smd" = everything not surface-mount: through-hole AND undetermined.
    lines = build_digikey_bom(_mixed_records(), mounting=_MOUNTING, mounting_filter=NON_SMD)
    assert {ln.references[0] for ln in lines} == {"J1", "U1"}


def test_both_keeps_everything():
    lines = build_digikey_bom(_mixed_records(), mounting=_MOUNTING, mounting_filter=BOTH)
    assert len(lines) == 3


def test_lines_carry_mounting():
    lines = build_digikey_bom(_mixed_records(), mounting=_MOUNTING)
    by_ref = {ln.references[0]: ln.mounting for ln in lines}
    assert by_ref == {"R1": SMD, "J1": THT, "U1": UNKNOWN}


def test_smd_only_with_no_mounting_excludes_all():
    # No PCB → empty mounting map → every part is undetermined → nothing kept.
    assert build_digikey_bom(_mixed_records(), mounting={}, mounting_filter=SMD_ONLY) == []


def test_non_smd_with_no_mounting_keeps_all():
    # No PCB → every part reads "unknown" → not SMD → whole board kept.
    lines = build_digikey_bom(_mixed_records(), mounting={}, mounting_filter=NON_SMD)
    assert len(lines) == 3


# ---- CSV writing ------------------------------------------------------

def test_write_csv_header_and_quoting():
    lines = build_digikey_bom([
        _rec("R1", Value="10k", Footprint="R_0603", DigiKeyPN="DK1"),
        _rec("R2", Value="10k", Footprint="R_0603", DigiKeyPN="DK1"),
        _rec("R3", Value="10k", Footprint="R_0603", DigiKeyPN="DK1"),
    ])
    buf = io.StringIO()
    write_digikey_csv(lines, buf)
    text = buf.getvalue()
    first = text.splitlines()[0]
    assert first == "Quantity,Digi-Key Part Number,Reference Designator,Manufacturer Part Number"
    assert DK_FIELDS == [
        "Quantity", "Digi-Key Part Number", "Reference Designator", "Manufacturer Part Number"
    ]
    assert '"R1,R2,R3"' in text  # comma-bearing cell is quoted
    row = next(_csv.DictReader(io.StringIO(text)))
    assert row["Quantity"] == "3"


# ---- real board -------------------------------------------------------

def test_build_from_real_board():
    lines = digikey_bom_from_schematic(BOARD)
    assert lines
    # Every placed BOM part is accounted for exactly once.
    assert sum(ln.quantity for ln in lines) == len(iter_bom_part_records(BOARD))


# ---- CLI --------------------------------------------------------------

def test_cli_writes_csv_file(tmp_path, capsys):
    out = tmp_path / "bom.csv"
    cli.main(["digikey", str(BOARD), "-o", str(out)])
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == (
        "Quantity,Digi-Key Part Number,Reference Designator,Manufacturer Part Number"
    )
    assert capsys.readouterr().out == ""  # nothing on stdout when -o is given


def test_cli_stdout(capsys):
    cli.main(["digikey", str(BOARD)])
    out = capsys.readouterr().out
    assert out.startswith("Quantity,Digi-Key Part Number,Reference Designator,Manufacturer Part Number")


def test_cli_exit_code_tracks_unorderable(tmp_path, capsys):
    out = tmp_path / "bom.csv"
    expected = 1 if unorderable_lines(digikey_bom_from_schematic(BOARD)) else 0
    rc = cli.main(["digikey", str(BOARD), "-o", str(out)])
    assert rc == expected
    if expected == 1:
        assert "no DigiKey or manufacturer part number" in capsys.readouterr().err


@pytest.fixture
def no_standard_libs(tmp_path, monkeypatch):
    """Point the standard footprint library fallback at an empty folder, so a
    part not on the synthetic board stays of undetermined mounting instead of
    being read from this repo's Libraries/kicad-footprints."""
    import kicadtools.footprints as footprints
    monkeypatch.setattr(footprints, "DEFAULT_REPO_ROOT", tmp_path / "no_repo")


def _write_pcb(path, entries):
    """Write a minimal .kicad_pcb with (ref, attr-first-token) footprint nodes."""
    nodes = "\n".join(
        f'\t(footprint "L:F"\n\t\t(at 0 0)\n\t\t(property "Reference" "{ref}")\n\t\t(attr {attr})\n\t)'
        for ref, attr in entries
    )
    path.write_text(f"(kicad_pcb\n{nodes}\n)\n", encoding="utf-8")


def test_cli_smd_filters_by_sibling_pcb(tmp_path, capsys, no_standard_libs):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")
    refs = [r.reference for r in iter_bom_part_records(sch)]
    smd_refs, tht_ref = refs[:2], refs[2]
    _write_pcb(
        tmp_path / "board.kicad_pcb",
        [(smd_refs[0], "smd"), (smd_refs[1], "smd"), (tht_ref, "through_hole")],
    )

    out = tmp_path / "smd.csv"
    cli.main(["digikey", str(sch), "--mounting", "smd", "-o", str(out)])
    with open(out, encoding="utf-8") as f:
        designators = set()
        for row in _csv.DictReader(f):
            designators.update(row["Reference Designator"].split(","))
    for ref in smd_refs:
        assert ref in designators     # SMD parts kept
    assert tht_ref not in designators  # through-hole excluded
    err = capsys.readouterr().err
    assert "undetermined mounting" in err  # the absent refs are reported


def test_cli_non_smd_filters_by_sibling_pcb(tmp_path, capsys, no_standard_libs):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")
    refs = [r.reference for r in iter_bom_part_records(sch)]
    smd_refs, tht_ref = refs[:2], refs[2]
    _write_pcb(
        tmp_path / "board.kicad_pcb",
        [(smd_refs[0], "smd"), (smd_refs[1], "smd"), (tht_ref, "through_hole")],
    )

    out = tmp_path / "nonsmd.csv"
    cli.main(["digikey", str(sch), "--mounting", "non-smd", "-o", str(out)])
    with open(out, encoding="utf-8") as f:
        designators = set()
        for row in _csv.DictReader(f):
            designators.update(row["Reference Designator"].split(","))
    assert tht_ref in designators                 # through-hole kept
    for ref in smd_refs:
        assert ref not in designators             # surface-mount excluded
    # The rest of the board (absent from the PCB → unknown) is non-SMD, so kept.
    assert len(designators) > 1


def test_cli_smd_no_pcb_warns_and_empties(tmp_path, capsys, no_standard_libs):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")  # no sibling PCB
    out = tmp_path / "smd.csv"
    cli.main(["digikey", str(sch), "--mounting", "smd", "-o", str(out)])
    assert out.read_text(encoding="utf-8").splitlines() == [
        "Quantity,Digi-Key Part Number,Reference Designator,Manufacturer Part Number"
    ]  # header only — nothing determinable
    assert "no board.kicad_pcb" in capsys.readouterr().err


def test_cli_non_smd_no_pcb_keeps_all_no_warning(tmp_path, capsys, no_standard_libs):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(FLAT_BOARD.read_text(encoding="utf-8"), encoding="utf-8")  # no sibling PCB
    out = tmp_path / "nonsmd.csv"
    cli.main(["digikey", str(sch), "--mounting", "non-smd", "-o", str(out)])
    n_parts = len(iter_bom_part_records(sch))
    with open(out, encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    assert sum(int(r["Quantity"]) for r in rows) == n_parts  # whole board kept
    assert "no board.kicad_pcb" not in capsys.readouterr().err  # no mounting warning
