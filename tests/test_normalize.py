"""End-to-end tests for part-number key normalization (kicadtools.normalize)."""
import re
import shutil
from pathlib import Path

import sexpdata

from kicadtools.cli import main
from kicadtools.normalize import (
    compute_file_changes,
    plan_normalization,
    write_file_changes,
)

DATA = Path(__file__).parent / "data"
ECG = DATA / "ECG.kicad_sch"

# Every part-number key spelling (canonical or variant) that ECG uses, so we can
# count non-empty values before/after and prove nothing was dropped.
_PN_VALUE_RE = re.compile(
    r'"(?:DigikeyPN|DigiKeyPN|DigiKey PN|Manufacturer_PN|MPN|MANUFACTURER'
    r'|Manufacturer|LCSC)" "([^"]+)"'
)


def _pn_values(text: str) -> list[str]:
    return _PN_VALUE_RE.findall(text)


def test_plan_finds_variant_keys_in_ecg():
    plan = plan_normalization(ECG)
    assert plan  # ECG is full of DigikeyPN / Manufacturer_PN variants
    ops = [e.op for sym in plan for e in sym.edits]
    assert "rename" in ops


def test_write_normalizes_keys_and_is_lossless(tmp_path):
    work = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, work)
    before = work.read_text(encoding="utf-8")

    plan = plan_normalization(work)
    changes = compute_file_changes(plan)
    write_file_changes(changes)

    after = work.read_text(encoding="utf-8")
    # Still a valid s-expression KiCad can open.
    assert sexpdata.loads(after)[0] == sexpdata.Symbol("kicad_sch")
    # No non-empty part-number value was lost (order-independent).
    assert sorted(_pn_values(after)) == sorted(_pn_values(before))
    # The lowercase-k variant that carried a value is gone; the canonical wins.
    assert '(property "DigikeyPN" "SM5817PL-TPMSCT-ND"' in before
    assert '(property "DigikeyPN" "SM5817PL-TPMSCT-ND"' not in after
    assert '(property "DigiKeyPN" "SM5817PL-TPMSCT-ND"' in after


def test_normalization_is_idempotent(tmp_path):
    work = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, work)
    write_file_changes(compute_file_changes(plan_normalization(work)))
    # Second pass finds nothing left to do.
    assert plan_normalization(work) == []


def test_only_removes_change_the_property_count(tmp_path):
    # Renames/sets never add or drop a node; the node count only falls by the
    # two empty Manufacturer_PN duplicates that sit beside a populated MPN.
    work = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, work)
    before = work.read_text(encoding="utf-8")
    write_file_changes(compute_file_changes(plan_normalization(work)))
    after = work.read_text(encoding="utf-8")
    assert before.count("(property ") - after.count("(property ") == 2


def test_lib_symbols_definitions_are_not_touched(tmp_path):
    # Only *placed* symbols are normalized; the embedded lib_symbols template
    # (three-tab indented) keeps its original spelling.
    work = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, work)
    write_file_changes(compute_file_changes(plan_normalization(work)))
    after = work.read_text(encoding="utf-8")
    assert '\t\t\t(property "DigikeyPN" "505-AD8232ACPZ-R7CT-ND"' in after


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    work = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, work)
    before = work.read_text(encoding="utf-8")

    rc = main(["normalize", "-n", str(work)])
    assert rc == 0
    assert work.read_text(encoding="utf-8") == before  # untouched

    out = capsys.readouterr().out
    assert "Dry run" in out
    assert 'rename "DigikeyPN" → "DigiKeyPN"' in out


def test_cli_apply_then_clean(tmp_path, capsys):
    work = tmp_path / "ECG.kicad_sch"
    shutil.copy(ECG, work)

    assert main(["normalize", str(work)]) == 0
    assert "Wrote 1 file" in capsys.readouterr().out

    # Re-running reports a clean board.
    assert main(["normalize", str(work)]) == 0
    assert "already canonical" in capsys.readouterr().out
