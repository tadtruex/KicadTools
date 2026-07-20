"""Tests for the `confirm` CLI orchestration core (cli.run_confirm)."""
import shutil
from pathlib import Path

from kicadtools.cli import run_confirm
from kicadtools.confirm import confirm_resistor
from kicadtools.intent import CapacitorIntent, ResistorIntent
from kicadtools.resolve import PartRecord, iter_bom_part_records
from kicadtools.suppliers import DigiKeyProduct

BOARD = Path(__file__).parent / "data" / "MotorDriver.kicad_sch"


class FakeResolver:
    """Supplies canned confirm results keyed by reference; records the calls."""

    def __init__(self, results):
        self.results = results        # ref -> ConfirmResult | None
        self.calls = []

    def confirm(self, record):
        self.calls.append(record.reference)
        return self.results.get(record.reference)


def _resistor(ref="R1", **kw):
    kw.setdefault("dnp", False)
    kw.setdefault("in_bom", True)
    kw.setdefault("value", "10k")
    return ResistorIntent(reference=ref, **kw)


def _record(intent, source_file=BOARD, uuid="uuid-1", **props):
    props.setdefault("Reference", intent.reference)
    return PartRecord(intent.reference, props, source_file, uuid, intent)


def _product(mpn="RC0402FR-0710KL", **params):
    return DigiKeyProduct(mpn=mpn, parameters=params)


def _fill_result(intent):
    # A blank-tolerance resistor that DigiKey can fill.
    return confirm_resistor(intent, _product(Resistance="10 kOhms", Tolerance="±1%"))


def test_run_confirm_tallies_fills_and_conflicts():
    r_fill = _resistor("R1")
    r_conflict = _resistor("R2", power_min="0.25W")
    records = [
        _record(r_fill, uuid="u1", MPN="M1"),
        _record(r_conflict, uuid="u2", MPN="M2"),
    ]
    results = {
        "R1": _fill_result(r_fill),
        "R2": confirm_resistor(
            r_conflict, _product(Resistance="10 kOhms", **{"Power (Watts)": "0.063W, 1/16W"})
        ),
    }
    lines = []
    cs, t = run_confirm(records, FakeResolver(results), out=lines.append)

    assert t == dict(confirmed=2, conflicted=1, filled=1, skipped=0, unsupported=0)
    assert any(c.property_key == "Tolerance:Preferred" for c in cs.changes)
    assert [c.label for c in cs.conflicts] == ["power rating"]
    assert len(lines) == 2  # one per confirmed part


def test_run_confirm_skips_non_resistor():
    cap = CapacitorIntent(reference="C1", dnp=False, in_bom=True, value="100nF")
    resolver = FakeResolver({})
    lines = []
    cs, t = run_confirm([_record(cap, MPN="X")], resolver, out=lines.append)

    assert t["unsupported"] == 1
    assert resolver.calls == []      # non-resistors never reach the resolver
    assert cs.is_empty
    assert lines == []


def test_run_confirm_skips_when_resolver_returns_none():
    # A resistor with no part number: the resolver returns None, part is skipped.
    r = _resistor("R9")
    lines = []
    _cs, t = run_confirm(
        [_record(r, uuid="u9")], FakeResolver({"R9": None}), out=lines.append
    )
    assert t["skipped"] == 1
    assert any("skipped (no part number)" in ln for ln in lines)


def test_run_confirm_write_applies_to_schematic(tmp_path):
    copy = tmp_path / "board.kicad_sch"
    shutil.copy(BOARD, copy)
    src = next(
        r for r in iter_bom_part_records(BOARD)
        if isinstance(r.intent, ResistorIntent) and r.symbol_uuid
    )
    intent = _resistor(src.reference)  # blank tolerance
    rec = PartRecord(src.reference, {"Reference": src.reference}, copy, src.symbol_uuid, intent)

    resolver = FakeResolver({src.reference: _fill_result(intent)})
    _cs, _t = run_confirm([rec], resolver, write=True, out=lambda _l: None)

    block_start = copy.read_text().index(f'(uuid "{src.symbol_uuid}")')
    block = copy.read_text()[block_start: block_start + 4000]
    assert '(property "Tolerance:Preferred" "1%"' in block


def test_run_confirm_without_write_leaves_files_untouched(tmp_path):
    copy = tmp_path / "board.kicad_sch"
    shutil.copy(BOARD, copy)
    before = copy.read_text()
    src = next(
        r for r in iter_bom_part_records(BOARD)
        if isinstance(r.intent, ResistorIntent) and r.symbol_uuid
    )
    intent = _resistor(src.reference)
    rec = PartRecord(src.reference, {"Reference": src.reference}, copy, src.symbol_uuid, intent)

    run_confirm([rec], FakeResolver({src.reference: _fill_result(intent)}),
                write=False, out=lambda _l: None)
    assert copy.read_text() == before  # report-only never mutates
