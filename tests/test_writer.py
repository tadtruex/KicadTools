import difflib
import shutil
from collections import defaultdict
from pathlib import Path

import pytest
import sexpdata

from kicadtools.components import get_components
from kicadtools.intent import LedIntent
from kicadtools.partnum import PropEdit
from kicadtools.writer import (
    add_intent_types,
    apply_key_edits,
    insert_property,
    remove_property,
    rename_property_key,
    set_property_value,
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


# ----------------------------------------------------------------------
# In-place property edits: rename / set / remove / apply
# ----------------------------------------------------------------------
UUID = "abc-0001"


def _symbol(*prop_lines: str) -> str:
    """A minimal but structurally real placed-symbol schematic for edit tests."""
    props = "\n".join(prop_lines)
    return (
        "(kicad_sch\n"
        "\t(symbol\n"
        '\t\t(lib_id "Device:R")\n'
        f'\t\t(uuid "{UUID}")\n'
        '\t\t(property "Reference" "R1"\n'
        "\t\t\t(at 0 0 0)\n"
        "\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        "\t\t)\n"
        f"{props}\n"
        "\t)\n"
        ")\n"
    )


def _prop(key: str, value: str) -> str:
    return (
        f'\t\t(property "{key}" "{value}"\n'
        "\t\t\t(at 1 2 90)\n"
        "\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        "\t\t)"
    )


def test_rename_property_key_preserves_value_and_position():
    text = _symbol(_prop("DigikeyPN", "296-1-ND"))
    out = rename_property_key(text, UUID, "DigikeyPN", "DigiKeyPN")
    assert '(property "DigikeyPN"' not in out
    assert '(property "DigiKeyPN" "296-1-ND"' in out
    assert "(at 1 2 90)" in out  # position untouched
    assert sexpdata.loads(out)[0] == sexpdata.Symbol("kicad_sch")


def test_rename_onto_existing_key_raises():
    text = _symbol(_prop("DigikeyPN", "296-1-ND"), _prop("DigiKeyPN", ""))
    with pytest.raises(ValueError):
        rename_property_key(text, UUID, "DigikeyPN", "DigiKeyPN")


def test_set_property_value_replaces_only_the_value():
    text = _symbol(_prop("DigiKeyPN", ""))
    out = set_property_value(text, UUID, "DigiKeyPN", "36-232-ND")
    assert '(property "DigiKeyPN" "36-232-ND"' in out
    assert sexpdata.loads(out)[0] == sexpdata.Symbol("kicad_sch")


def test_remove_property_deletes_whole_node():
    text = _symbol(_prop("Manufacturer_PN", ""), _prop("LCSC", "C1"))
    out = remove_property(text, UUID, "Manufacturer_PN")
    assert "Manufacturer_PN" not in out
    assert '(property "LCSC" "C1"' in out  # sibling survives
    # exactly one property node fewer, and still valid.
    assert out.count("(property ") == text.count("(property ") - 1
    assert sexpdata.loads(out)[0] == sexpdata.Symbol("kicad_sch")


def test_missing_property_raises():
    text = _symbol(_prop("LCSC", "C1"))
    for fn in (
        lambda: set_property_value(text, UUID, "MPN", "x"),
        lambda: rename_property_key(text, UUID, "MPN", "MPN2"),
        lambda: remove_property(text, UUID, "MPN"),
    ):
        with pytest.raises(KeyError):
            fn()


def test_apply_key_edits_runs_a_mixed_plan():
    text = _symbol(
        _prop("DigikeyPN", "296-1-ND"),   # rename
        _prop("MPN", "M1"),               # canonical, kept
        _prop("Manufacturer_PN", ""),     # remove (empty dup of MPN)
    )
    edits = [
        PropEdit("rename", "DigiKeyPN", old_key="DigikeyPN", value="296-1-ND"),
        PropEdit("remove", "Manufacturer_PN"),
    ]
    out = apply_key_edits(text, UUID, edits)
    assert '(property "DigiKeyPN" "296-1-ND"' in out
    assert '(property "MPN" "M1"' in out
    assert "Manufacturer_PN" not in out
    assert sexpdata.loads(out)[0] == sexpdata.Symbol("kicad_sch")
