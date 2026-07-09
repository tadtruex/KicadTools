import difflib
import shutil
from collections import defaultdict
from pathlib import Path

import sexpdata

from kicadtools.components import get_components
from kicadtools.intent import LedIntent
from kicadtools.writer import (
    add_intent_types,
    insert_property,
    sibling_dest,
    write_back,
)

DATA = Path(__file__).parent / "data"
BOARD = DATA / "MotorDriver.kicad_sch"


def _guessed_uuid(board: Path, reference: str) -> str:
    c = next(c for c in get_components(board) if c.reference == reference)
    assert c.intent_guessed and c.symbol_uuid
    return c.symbol_uuid


def test_insert_is_additions_only():
    orig = BOARD.read_text(encoding="utf-8")
    uuid = _guessed_uuid(BOARD, "U5")  # the "1515 rgb led", guessed led
    out = insert_property(orig, uuid, "Intent:Type", "led")

    # Every diff hunk must be a pure insertion — no original line removed/changed.
    diff = list(difflib.ndiff(orig.splitlines(), out.splitlines()))
    removed = [ln for ln in diff if ln.startswith("- ")]
    added = [ln for ln in diff if ln.startswith("+ ")]
    assert removed == []
    assert any('(property "Intent:Type" "led"' in ln for ln in added)


def test_output_reparses_and_symbol_gains_the_property():
    orig = BOARD.read_text(encoding="utf-8")
    uuid = _guessed_uuid(BOARD, "U5")
    out = insert_property(orig, uuid, "Intent:Type", "led")

    tree = sexpdata.loads(out)  # still valid s-expression
    assert tree[0] == sexpdata.Symbol("kicad_sch")

    # The U5 symbol block now carries the property.
    block_start = out.index(f'(uuid "{uuid}")')
    block = out[block_start : block_start + 4000]
    assert '(property "Intent:Type" "led"' in block


def test_written_board_has_fewer_guesses(tmp_path):
    # Accept every guessed component across the whole hierarchy, write each file
    # back, and confirm on re-read that the guess count dropped. The board pulls
    # in a subsheet, so the tree is copied whole and edits grouped by file.
    work_dir = tmp_path / "data"
    shutil.copytree(DATA, work_dir)
    work_board = work_dir / BOARD.name

    before = get_components(work_board)
    edits: dict = defaultdict(dict)
    for c in before:
        if c.intent_guessed and c.symbol_uuid:
            edits[c.source_file][c.symbol_uuid] = c.intent_type
    assert edits  # sanity: the board has guesses
    assert len(edits) >= 2  # root + at least one subsheet

    for src, uuid_to_type in edits.items():
        write_back(src, uuid_to_type)  # in place

    after = get_components(work_board)
    assert len([c for c in after if c.intent_guessed]) < len(
        [c for c in before if c.intent_guessed]
    )
    # U5 specifically flips to an explicit LedIntent.
    u5 = next(c for c in after if c.reference == "U5")
    assert isinstance(u5, LedIntent)
    assert u5.intent_guessed is False


def test_sibling_dest_writes_beside_source(tmp_path):
    # write_back is pure text work, so a single-file copy is enough here.
    work = tmp_path / BOARD.name
    shutil.copy(BOARD, work)
    before = work.read_text(encoding="utf-8")
    uuid = _guessed_uuid(BOARD, "U5")  # U5 lives in the root board
    out_path = write_back(work, {uuid: "led"}, dest=sibling_dest())
    assert out_path.name == "MotorDriver.intents.kicad_sch"
    # Source is left byte-identical when writing to a sibling.
    assert work.read_text(encoding="utf-8") == before
    # The sibling has exactly one more Intent:Type than the source (the U5 one).
    marker = '(property "Intent:Type"'
    assert out_path.read_text(encoding="utf-8").count(marker) == before.count(marker) + 1


def test_unknown_uuid_raises():
    try:
        insert_property("(kicad_sch)", "does-not-exist", "Intent:Type", "led")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unknown uuid")


def test_duplicate_key_raises():
    orig = BOARD.read_text(encoding="utf-8")
    uuid = _guessed_uuid(BOARD, "U5")
    once = insert_property(orig, uuid, "Intent:Type", "led")
    try:
        insert_property(once, uuid, "Intent:Type", "led")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on duplicate Intent:Type")


def test_add_intent_types_applies_many():
    orig = BOARD.read_text(encoding="utf-8")
    comps = get_components(BOARD)
    # Only the guesses that actually live in the root board file.
    picks = {
        c.symbol_uuid: c.intent_type
        for c in comps
        if c.intent_guessed and c.symbol_uuid and c.source_file == BOARD
    }
    assert picks
    out = add_intent_types(orig, picks)
    assert out.count('(property "Intent:Type"') == orig.count(
        '(property "Intent:Type"'
    ) + len(picks)
