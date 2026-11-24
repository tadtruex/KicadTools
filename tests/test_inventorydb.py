"""Tests for the lab inventory tables (inventorydb.py, INVENTORY_PLAN.md step
1), on SQLite and, when configured, MariaDB (tests/dbbackends.py)."""
from __future__ import annotations

import pytest

from kicadtools.dbbackup import pull
from kicadtools.inventorydb import (INVENTORY_TABLES, InventoryDb, InventoryError,
                                    natural_key, norm_path)
from kicadtools.partsdb import PartChoice, PartParams, PartsDb
from tests.dbbackends import (BACKENDS, MARIADB, current, mariadb_params, new_db,
                              use_backend)

STAMP = "2026-11-23T12:00:00+00:00"


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


@pytest.fixture
def inv():
    return InventoryDb(new_db(now=lambda: STAMP))


def _cabinet(inv):
    """C12 (school) with drawers 3 and 5, and a top-level box 17."""
    inv.add_bin("C12", site="school", location="north wall")
    inv.add_bin("3", "C12")
    inv.add_bin("5", "C12")
    inv.add_bin("17", site="school", notes="static-safe box")
    inv.add_item("1k 1/2W carbon film", bin="C12/3", quantity=200,
                 category="Resistor", tags=["THT", "carbon film"])
    inv.add_item("SPDT slide switch", bin="C12/5", quantity=12, category="Switch")
    inv.add_item("4.7k 0603", bin="17", quantity=100, category="Resistor",
                 mpn="RMCF0603FT4K70", digikey_pn="RMCF0603FT4K70CT-ND")


# ----- bins ----------------------------------------------------------------

def test_nested_bins_have_paths_and_inherit_the_site(inv):
    _cabinet(inv)
    drawer = inv.bin("C12/5")
    assert drawer.name == "5" and drawer.path == "C12/5" and drawer.depth == 1
    assert drawer.parent_id == inv.bin("C12").id
    assert drawer.site is None and inv.site_of("C12/5") == "school"
    assert [b.path for b in inv.children()] == ["17", "C12"]
    assert [b.path for b in inv.children("C12")] == ["C12/3", "C12/5"]
    assert [b.path for b in inv.subtree("C12")] == ["C12", "C12/3", "C12/5"]


def test_paths_ignore_case_and_spacing(inv):
    inv.add_bin("C12")
    inv.add_bin("5", " c12 ")
    assert inv.bin(" c12 / 5 ").path == "C12/5"
    assert norm_path(" C12 /  5 ") == "C12/5"
    with pytest.raises(InventoryError, match="already exists"):
        inv.add_bin("c12")


def test_bad_bin_names_are_refused(inv):
    for name in ("", "   ", "a/b"):
        with pytest.raises(InventoryError):
            inv.add_bin(name)
    with pytest.raises(InventoryError, match="no bin"):
        inv.add_bin("3", "nowhere")


def test_ensure_bin_makes_the_missing_levels(inv):
    drawer = inv.ensure_bin("Barn shelf/Box 2/Bag 1", site="barn")
    assert drawer.path == "Barn shelf/Box 2/Bag 1"
    assert inv.bin("Barn shelf").site == "barn" and inv.bin("Barn shelf/Box 2").site is None
    assert inv.ensure_bin("barn shelf/box 2/bag 1").id == drawer.id


def test_rename_and_move_rewrite_the_paths_below(inv):
    _cabinet(inv)
    inv.rename_bin("C12", "C13")
    assert inv.bin("C12") is None
    assert [b.path for b in inv.subtree("C13")] == ["C13", "C13/3", "C13/5"]
    assert [i.bin_path for i in inv.items("C13")] == ["C13/3", "C13/5"]
    inv.add_bin("Room 2")
    inv.move_bin("C13", "Room 2")
    assert [b.path for b in inv.subtree("Room 2")] == [
        "Room 2", "Room 2/C13", "Room 2/C13/3", "Room 2/C13/5"]
    inv.move_bin("Room 2/C13", None)
    assert inv.bin("C13/5") is not None
    inv.rename_bin("C13", "c13")                         # a change of case only
    assert inv.bin("C13").name == "c13"


def test_a_bin_cannot_move_inside_itself_or_onto_another(inv):
    _cabinet(inv)
    with pytest.raises(InventoryError, match="inside itself"):
        inv.move_bin("C12", "C12/3")
    with pytest.raises(InventoryError, match="inside itself"):
        inv.move_bin("C12", "C12")
    inv.add_bin("3")
    with pytest.raises(InventoryError, match="already exists"):
        inv.move_bin("C12/3", None)
    assert inv.bin("C12/3") is not None                  # nothing changed


def test_only_an_empty_bin_is_deleted(inv):
    _cabinet(inv)
    with pytest.raises(InventoryError, match="holds other bins"):
        inv.delete_bin("C12")
    with pytest.raises(InventoryError, match="holds items"):
        inv.delete_bin("C12/5")
    inv.add_bin("empty", "C12")
    inv.delete_bin("C12/empty")
    assert inv.bin("C12/empty") is None


def test_update_bin_sets_and_clears_fields(inv):
    inv.add_bin("C12", site="school", location="north wall")
    inv.update_bin("C12", location="by the door")
    assert (inv.bin("C12").site, inv.bin("C12").location) == ("school", "by the door")
    inv.update_bin("C12", site="")
    assert inv.bin("C12").site is None and inv.site_of("C12") is None


def test_bins_sort_naturally(inv):
    for name in ("10", "2", "10001", "1", "C10", "C2"):
        inv.add_bin(name)
    assert [b.path for b in inv.bins()] == ["1", "2", "10", "10001", "C2", "C10"]
    assert sorted(["b10", "B2", "a"], key=natural_key) == ["a", "B2", "b10"]


def test_like_wildcards_in_a_path_match_only_themselves(inv):
    inv.add_bin("A_1")
    inv.add_bin("x", "A_1")
    inv.add_bin("AB1")
    inv.add_bin("y", "AB1")
    inv.add_item("in A_1", bin="A_1/x")
    inv.add_item("in AB1", bin="AB1/y")
    assert [b.path for b in inv.subtree("A_1")] == ["A_1", "A_1/x"]
    assert [i.name for i in inv.items("A_1")] == ["in A_1"]


# ----- items ---------------------------------------------------------------

def test_items_in_a_bin_include_the_bins_inside_it(inv):
    _cabinet(inv)
    assert [i.name for i in inv.items("C12")] == ["1k 1/2W carbon film", "SPDT slide switch"]
    assert [i.name for i in inv.items("C12/5")] == ["SPDT slide switch"]
    assert inv.items("C12", recursive=False) == []
    assert len(inv.items()) == 3


def test_add_item_needs_only_a_name(inv):
    item = inv.add_item("  STLink   V3 ")
    assert item.name == "STLink V3" and item.bin_path is None and item.quantity is None
    assert item.tags == () and item.created_at == STAMP
    assert [i.name for i in inv.unbinned()] == ["STLink V3"]
    with pytest.raises(InventoryError, match="needs a name"):
        inv.add_item("  ")
    with pytest.raises(TypeError, match="colour"):
        inv.add_item("x", colour="red")


def test_add_item_makes_its_bin(inv):
    item = inv.add_item("M6 x 5mm pan head screw", bin="Screw drawer/M6", quantity=200)
    assert item.bin_path == "Screw drawer/M6" and inv.bin("Screw drawer") is not None


def test_a_failed_add_leaves_no_new_bin(inv):
    # A tag is checked after the bin is made, inside the same transaction:
    # the failure rolls the new bins back with it.
    with pytest.raises(InventoryError, match="longer than"):
        inv.add_item("x", bin="New/Drawer", tags=["t" * 65])
    assert inv.bin("New") is None and inv.bins() == [] and inv.items() == []
    with pytest.raises(InventoryError, match="negative"):
        inv.add_item("x", bin="Other", quantity=-1)
    assert inv.bin("Other") is None


def test_update_item_changes_only_what_is_given(inv):
    _cabinet(inv)
    switch = inv.items("C12/5")[0]
    changed = inv.update_item(switch.id, quantity=10, manufacturer="C&K",
                              tags=["switch", "Switch", " slide "])
    assert (changed.name, changed.quantity, changed.manufacturer) == (
        "SPDT slide switch", 10, "C&K")
    assert changed.tags == ("slide", "switch")
    moved = inv.update_item(switch.id, bin="C12/3", manufacturer="")
    assert moved.bin_path == "C12/3" and moved.manufacturer is None
    assert inv.update_item(switch.id, bin=None).bin_path is None
    with pytest.raises(InventoryError, match="needs a name"):
        inv.update_item(switch.id, name="")


def test_adjust_quantity_stops_at_zero(inv):
    item = inv.add_item("Nano Every", quantity=6)
    assert inv.adjust_quantity(item.id, -2).quantity == 4
    assert inv.adjust_quantity(item.id, -10).quantity == 0
    unknown = inv.add_item("Wire, red 22 AWG")
    assert inv.adjust_quantity(unknown.id, 1).quantity == 1


def test_delete_item_removes_its_tags(inv):
    item = inv.add_item("Nano Every", tags=["arduino"])
    inv.delete_item(item.id)
    assert inv.item(item.id) is None and inv.tags() == []
    with pytest.raises(InventoryError, match="no item"):
        inv.delete_item(item.id)


# ----- values used so far --------------------------------------------------

def test_values_and_tags_used_so_far(inv):
    _cabinet(inv)
    inv.add_item("Nano Every", category="Dev board", tags=["arduino", "AVR"])
    inv.add_item("Uno R3", category="dev board", tags=["Arduino"])
    inv.add_bin("Shelf", site="barn")
    assert inv.values("category") == ["Dev board", "Resistor", "Switch"]
    assert inv.values("site") == ["barn", "school"]
    assert [t.casefold() for t in inv.tags()] == ["arduino", "avr", "carbon film", "tht"]
    with pytest.raises(ValueError):
        inv.values("name")


def test_unicode_text_round_trips(inv):
    item = inv.add_item("100 µF ± 20 %", description="Würth, 25 V", bin="Bin µ")
    assert inv.item(item.id).name == "100 µF ± 20 %"
    assert inv.item(item.id).description == "Würth, 25 V"
    assert inv.bin("Bin µ") is not None


# ----- with the parts tables -----------------------------------------------

def _second_connection(tmp_path):
    """(an InventoryDb, a function opening another connection to the same
    database) on the current engine."""
    if current() == MARIADB:
        first = new_db(now=lambda: STAMP)
        return InventoryDb(first), lambda: PartsDb.connect_mariadb(**mariadb_params())
    path = tmp_path / "parts.sqlite"
    return InventoryDb(PartsDb(path, now=lambda: STAMP)), lambda: PartsDb(path)


def test_every_change_is_committed(tmp_path):
    # Another connection (another machine, the web app) sees each change at
    # once, and a failed change leaves it nothing to see.
    inv, other = _second_connection(tmp_path)

    def seen():
        db = other()
        try:
            view = InventoryDb(db)
            return [b.path for b in view.bins()], [(i.name, i.quantity, i.tags)
                                                   for i in view.items()]
        finally:
            db.close()

    item = inv.add_item("Nano Every", bin="C12/5", quantity=6, tags=["arduino"])
    assert seen() == (["C12", "C12/5"], [("Nano Every", 6, ("arduino",))])
    inv.adjust_quantity(item.id, -1)
    inv.rename_bin("C12", "C13")
    assert seen() == (["C13", "C13/5"], [("Nano Every", 5, ("arduino",))])
    with pytest.raises(InventoryError):
        inv.add_item("x", bin="New/Drawer", tags=["t" * 65])
    assert seen() == (["C13", "C13/5"], [("Nano Every", 5, ("arduino",))])
    inv.delete_item(item.id)
    assert seen() == (["C13", "C13/5"], [])


def test_opening_twice_keeps_the_rows(inv):
    _cabinet(inv)
    again = InventoryDb(inv.db)
    assert len(again.items()) == 3


def test_parts_and_inventory_share_the_database(inv):
    inv.db.accept_part(PartChoice(
        kind="resistor", mpn="RMCF0603FT4K70", manufacturer="Stackpole", package="0603",
        params=PartParams(4.7e3, 0.1, "", 1.0)))
    _cabinet(inv)
    assert inv.db._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1
    assert len(inv.items()) == 3


def test_pull_copies_the_inventory(inv, tmp_path):
    _cabinet(inv)
    # A bin moved under a newer one has a smaller id than its parent; the
    # copy must still insert the parent first.
    inv.add_bin("Room 2")
    inv.move_bin("C12", "Room 2")
    counts = pull(inv.db, tmp_path / "copy.sqlite")
    assert (counts["bin"], counts["item"], counts["item_tag"]) == (5, 3, 2)
    copy = InventoryDb(PartsDb.open_read_only(tmp_path / "copy.sqlite"))
    assert [b.path for b in copy.bins()] == [
        "17", "Room 2", "Room 2/C12", "Room 2/C12/3", "Room 2/C12/5"]
    assert [i.name for i in copy.items("Room 2")] == ["1k 1/2W carbon film",
                                                      "SPDT slide switch"]
    assert copy.items("Room 2/C12/3")[0].tags == ("carbon film", "THT")


def test_pull_from_a_database_without_the_inventory(tmp_path):
    db = new_db(now=lambda: STAMP)
    counts = pull(db, tmp_path / "copy.sqlite")
    assert all(counts[t] == 0 for t in INVENTORY_TABLES)
