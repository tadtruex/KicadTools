"""Tests for the lab inventory tables (inventorydb.py: locations, items,
stock rows), on SQLite and, when configured, MariaDB (tests/dbbackends.py)."""
from __future__ import annotations

import pytest

from kicadtools.dbbackup import pull
from kicadtools.inventorydb import (INVENTORY_TABLES, InventoryDb, InventoryError,
                                    InventoryLayoutError, add_quantities, in_location,
                                    layout, natural_key, norm_path)
from kicadtools.inventorymigrate import create_v1
from kicadtools.partsdb import PartChoice, PartParams, PartsDb
from tests.dbbackends import (BACKENDS, MARIADB, current, mariadb_params, new_db,
                              use_backend)

STAMP = "2026-11-25T12:00:00+00:00"


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


@pytest.fixture
def inv():
    return InventoryDb(new_db(now=lambda: STAMP))


def _cabinet(inv):
    """school/C12 with drawers 3 and 5, and a box school/17."""
    inv.add_location("school")
    inv.add_location("C12", "school", description="north wall")
    inv.add_location("3", "school/C12")
    inv.add_location("5", "school/C12")
    inv.add_location("17", "school", notes="static-safe box")
    inv.add_item("1k 1/2W carbon film", location="school/C12/3", quantity=200,
                 category="Resistor", tags=["THT", "carbon film"])
    inv.add_item("SPDT slide switch", location="school/C12/5", quantity=12, category="Switch")
    inv.add_item("4.7k 0603", location="school/17", quantity=100, category="Resistor",
                 mpn="RMCF0603FT4K70", digikey_pn="RMCF0603FT4K70CT-ND")


# ----- layout ----------------------------------------------------------------

def test_a_new_database_gets_this_layout(inv):
    assert layout(inv.db) == "2"
    assert InventoryDb(inv.db) is not None          # a second open is fine


def test_the_first_layout_is_refused_until_migrated():
    db = new_db(now=lambda: STAMP)
    create_v1(db)
    assert layout(db) == "1"
    with pytest.raises(InventoryLayoutError, match="kicadtools inventory migrate"):
        InventoryDb(db)
    assert layout(db) == "1"                        # and nothing was created


# ----- locations ---------------------------------------------------------------

def test_nested_locations_have_paths(inv):
    _cabinet(inv)
    drawer = inv.location("school/C12/5")
    assert drawer.name == "5" and drawer.path == "school/C12/5" and drawer.depth == 2
    assert drawer.top == "school"
    assert drawer.parent_id == inv.location("school/C12").id
    assert [loc.path for loc in inv.children()] == ["school"]
    assert [loc.path for loc in inv.children("school")] == ["school/17", "school/C12"]
    assert [loc.path for loc in inv.subtree("school/C12")] == [
        "school/C12", "school/C12/3", "school/C12/5"]


def test_paths_ignore_case_and_spacing(inv):
    inv.add_location("C12")
    inv.add_location("5", " c12 ")
    assert inv.location(" c12 / 5 ").path == "C12/5"
    assert norm_path(" C12 /  5 ") == "C12/5"
    with pytest.raises(InventoryError, match="already exists"):
        inv.add_location("c12")


def test_bad_location_names_are_refused(inv):
    for name in ("", "   ", "a/b"):
        with pytest.raises(InventoryError):
            inv.add_location(name)
    with pytest.raises(InventoryError, match="no location"):
        inv.add_location("3", "nowhere")


def test_ensure_location_makes_the_missing_levels(inv):
    bag = inv.ensure_location("barn/Shelf/Box 2/Bag 1")
    assert bag.path == "barn/Shelf/Box 2/Bag 1"
    assert [loc.path for loc in inv.locations()] == [
        "barn", "barn/Shelf", "barn/Shelf/Box 2", "barn/Shelf/Box 2/Bag 1"]
    assert inv.ensure_location("BARN/shelf/box 2/bag 1").id == bag.id


def test_rename_and_move_rewrite_the_paths_below(inv):
    _cabinet(inv)
    inv.rename_location("school/C12", "C13")
    assert inv.location("school/C12") is None
    assert [loc.path for loc in inv.subtree("school/C13")] == [
        "school/C13", "school/C13/3", "school/C13/5"]
    assert [i.locations for i in inv.items("school/C13")] == [
        ("school/C13/3",), ("school/C13/5",)]
    inv.add_location("home")
    inv.move_location("school/C13", "home")
    assert [loc.path for loc in inv.subtree("home")] == [
        "home", "home/C13", "home/C13/3", "home/C13/5"]
    inv.move_location("home/C13", None)
    assert inv.location("C13/5") is not None
    inv.rename_location("C13", "c13")                     # a change of case only
    assert inv.location("C13").name == "c13"


def test_a_location_cannot_move_inside_itself_or_onto_another(inv):
    _cabinet(inv)
    with pytest.raises(InventoryError, match="inside itself"):
        inv.move_location("school/C12", "school/C12/3")
    with pytest.raises(InventoryError, match="inside itself"):
        inv.move_location("school/C12", "school/C12")
    inv.add_location("3", "school")
    with pytest.raises(InventoryError, match="already exists"):
        inv.move_location("school/C12/3", "school")
    assert inv.location("school/C12/3") is not None      # nothing changed


def test_only_an_empty_location_is_deleted(inv):
    _cabinet(inv)
    with pytest.raises(InventoryError, match="holds other locations"):
        inv.delete_location("school/C12")
    with pytest.raises(InventoryError, match="holds items"):
        inv.delete_location("school/C12/5")
    inv.add_location("empty", "school/C12")
    inv.delete_location("school/C12/empty")
    assert inv.location("school/C12/empty") is None


def test_a_location_holding_a_row_at_zero_is_not_empty(inv):
    item = inv.add_item("Nano Every", location="home/box", quantity=1)
    inv.adjust_stock(item.stock[0].id, -5)
    with pytest.raises(InventoryError, match="holds items"):
        inv.delete_location("home/box")


def test_update_location_sets_and_clears_fields(inv):
    inv.add_location("C12", description="Cabinet of small drawers")
    inv.update_location("C12", description="  Drawers 1-20: passives\nnorth wall  ")
    assert inv.location("C12").description == "Drawers 1-20: passives\nnorth wall"
    inv.update_location("C12", notes="keys in the top drawer")   # description kept
    assert inv.location("C12").description.startswith("Drawers 1-20")
    inv.update_location("C12", description="")
    assert inv.location("C12").description is None
    assert inv.location("C12").notes == "keys in the top drawer"


def test_locations_sort_naturally(inv):
    for name in ("10", "2", "10001", "1", "C10", "C2"):
        inv.add_location(name)
    assert [loc.path for loc in inv.locations()] == ["1", "2", "10", "10001", "C2", "C10"]
    assert sorted(["b10", "B2", "a"], key=natural_key) == ["a", "B2", "b10"]


def test_like_wildcards_in_a_path_match_only_themselves(inv):
    inv.ensure_location("A_1/x")
    inv.ensure_location("AB1/y")
    inv.add_item("in A_1", location="A_1/x")
    inv.add_item("in AB1", location="AB1/y")
    assert [loc.path for loc in inv.subtree("A_1")] == ["A_1", "A_1/x"]
    assert [i.name for i in inv.items("A_1")] == ["in A_1"]
    assert [s.location_path for s in inv.stock_in("A_1")] == ["A_1/x"]


def test_in_location():
    assert in_location("school/C12/5", "school/c12")
    assert in_location("school", "school")
    assert not in_location("school2", "school")
    assert not in_location(None, "school")


# ----- items -------------------------------------------------------------------

def test_items_in_a_location_include_the_locations_inside_it(inv):
    _cabinet(inv)
    assert [i.name for i in inv.items("school/C12")] == [
        "1k 1/2W carbon film", "SPDT slide switch"]
    assert [i.name for i in inv.items("school/C12/5")] == ["SPDT slide switch"]
    assert inv.items("school/C12", recursive=False) == []
    assert len(inv.items()) == 3 and len(inv.items("school")) == 3


def test_add_item_needs_only_a_name(inv):
    item = inv.add_item("  STLink   V3 ")
    assert item.name == "STLink V3" and item.stock == () and item.on_hand is None
    assert item.tags == () and item.created_at == STAMP
    assert [i.name for i in inv.unplaced()] == ["STLink V3"]
    with pytest.raises(InventoryError, match="needs a name"):
        inv.add_item("  ")
    with pytest.raises(TypeError, match="colour"):
        inv.add_item("x", colour="red")
    with pytest.raises(InventoryError, match="needs a location"):
        inv.add_item("x", quantity=3)


def test_add_item_with_a_location_makes_it_and_the_first_stock_row(inv):
    item = inv.add_item("M6 x 5mm pan head screw", location="barn/Screws/M6", quantity=200)
    assert inv.location("barn/Screws") is not None
    (row,) = item.stock
    assert (row.location_path, row.quantity, row.item_id) == ("barn/Screws/M6", 200, item.id)
    assert item.on_hand == 200 and item.locations == ("barn/Screws/M6",)
    unknown = inv.add_item("Wire, red 22 AWG", location="barn")
    assert unknown.stock[0].quantity is None and unknown.on_hand is None


def test_a_failed_add_leaves_no_new_location(inv):
    # A tag is checked after the item is written, inside the same
    # transaction: the failure rolls back the item and its locations.
    with pytest.raises(InventoryError, match="longer than"):
        inv.add_item("x", location="New/Drawer", tags=["t" * 65])
    assert inv.locations() == [] and inv.items() == []
    with pytest.raises(InventoryError, match="negative"):
        inv.add_item("x", location="Other", quantity=-1)
    assert inv.location("Other") is None and inv.items() == []


def test_update_item_changes_only_what_is_given(inv):
    _cabinet(inv)
    switch = inv.items("school/C12/5")[0]
    changed = inv.update_item(switch.id, manufacturer="C&K",
                              tags=["switch", "Switch", " slide "])
    assert (changed.name, changed.manufacturer, changed.on_hand) == (
        "SPDT slide switch", "C&K", 12)
    assert changed.tags == ("slide", "switch")
    assert inv.update_item(switch.id, manufacturer="").manufacturer is None
    with pytest.raises(InventoryError, match="needs a name"):
        inv.update_item(switch.id, name="")
    with pytest.raises(TypeError):
        inv.update_item(switch.id, quantity=3)


def test_delete_item_removes_its_stock_and_tags(inv):
    item = inv.add_item("Nano Every", location="home/box", quantity=3, tags=["arduino"])
    inv.add_stock(item.id, "school/17", 2)
    inv.delete_item(item.id)
    assert inv.item(item.id) is None and inv.tags() == []
    assert inv.stock_in("home") == [] and inv.stock_in("school") == []
    with pytest.raises(InventoryError, match="no item"):
        inv.delete_item(item.id)


def test_matching_finds_the_same_thing_by_name_or_part_number(inv):
    _cabinet(inv)
    assert [i.name for i in inv.matching(name=" 4.7K   0603 ")] == ["4.7k 0603"]
    assert [i.name for i in inv.matching(mpn="rmcf0603ft4k70")] == ["4.7k 0603"]
    assert [i.name for i in inv.matching(digikey_pn="RMCF0603FT4K70CT-ND")] == ["4.7k 0603"]
    assert inv.matching(name="4.7k") == [] and inv.matching() == []


# ----- stock -------------------------------------------------------------------

def test_an_item_in_several_locations(inv):
    item = inv.add_item("Nano Every", location="school/17", quantity=50)
    inv.add_stock(item.id, "home/C3/2", 10, notes="for the clock project")
    inv.add_stock(item.id, "barn", None)
    item = inv.item(item.id)
    assert item.locations == ("barn", "home/C3/2", "school/17")
    assert item.on_hand == 60                       # the unknown one adds nothing
    assert [s.location_path for s in item.stock_in("home")] == ["home/C3/2"]
    assert [i.name for i in inv.items("home")] == ["Nano Every"]
    assert [s.quantity for s in inv.stock_in("school")] == [50]


def test_adding_stock_where_the_item_already_is_adds_to_that_row(inv):
    item = inv.add_item("Nano Every", location="school/17", quantity=5)
    row = inv.add_stock(item.id, "School/17", 3, notes="second box")
    assert (row.id, row.quantity, row.notes) == (item.stock[0].id, 8, "second box")
    assert len(inv.item(item.id).stock) == 1


def test_adjust_stock_stops_at_zero_and_keeps_the_row(inv):
    item = inv.add_item("Nano Every", location="school/17", quantity=6)
    row = item.stock[0]
    assert inv.adjust_stock(row.id, -2).quantity == 4
    assert inv.adjust_stock(row.id, -10).quantity == 0
    assert inv.item(item.id).stock[0].quantity == 0          # still there
    unknown = inv.add_item("Wire, red 22 AWG", location="barn").stock[0]
    assert inv.adjust_stock(unknown.id, 1).quantity == 1


def test_update_stock(inv):
    row = inv.add_item("Nano Every", location="school/17", quantity=6).stock[0]
    assert inv.update_stock(row.id, quantity=None).quantity is None
    assert inv.update_stock(row.id, notes=" top shelf ").notes == "top shelf"
    assert inv.update_stock(row.id, quantity=9).notes == "top shelf"
    with pytest.raises(InventoryError, match="negative"):
        inv.update_stock(row.id, quantity=-1)


def test_move_stock_and_merge_into_a_row_already_there(inv):
    item = inv.add_item("Nano Every", location="school/17", quantity=6)
    a = item.stock[0]
    moved = inv.move_stock(a.id, "school/C12/5")
    assert moved.id == a.id and moved.location_path == "school/C12/5"
    b = inv.add_stock(item.id, "home", 4, notes="spares")
    merged = inv.move_stock(b.id, "school/c12/5")
    assert (merged.id, merged.quantity, merged.notes) == (a.id, 10, "spares")
    assert inv.stock_row(b.id) is None
    assert inv.item(item.id).locations == ("school/C12/5",)
    assert inv.move_stock(a.id, "school/C12/5") == inv.stock_row(a.id)   # same place


def test_delete_stock_leaves_the_item(inv):
    item = inv.add_item("Nano Every", location="school/17", quantity=6)
    inv.delete_stock(item.stock[0].id)
    assert inv.item(item.id).stock == ()
    assert [i.name for i in inv.unplaced()] == ["Nano Every"]
    with pytest.raises(InventoryError, match="no stock row"):
        inv.delete_stock(item.stock[0].id)


def test_add_quantities():
    assert add_quantities(None, None) is None
    assert add_quantities(None, 3) == 3 and add_quantities(4, 3) == 7


# ----- values used so far ------------------------------------------------------

def test_values_and_tags_used_so_far(inv):
    _cabinet(inv)
    inv.add_item("Nano Every", category="Dev board", tags=["arduino", "AVR"])
    inv.add_item("Uno R3", category="dev board", tags=["Arduino"])
    assert inv.values("category") == ["Dev board", "Resistor", "Switch"]
    assert [t.casefold() for t in inv.tags()] == ["arduino", "avr", "carbon film", "tht"]
    with pytest.raises(ValueError):
        inv.values("name")
    with pytest.raises(ValueError):
        inv.values("site")


def test_free_text_keeps_line_breaks_and_names_do_not(inv):
    item = inv.add_item("  Nano   Every ", description=" Line one\n  line two ",
                        notes="a\n\nb", category="  Dev   board ")
    assert (item.name, item.category) == ("Nano Every", "Dev board")
    assert (item.description, item.notes) == ("Line one\n  line two", "a\n\nb")


def test_unicode_text_round_trips(inv):
    item = inv.add_item("100 µF ± 20 %", description="Würth, 25 V", location="Bin µ")
    assert inv.item(item.id).name == "100 µF ± 20 %"
    assert inv.item(item.id).description == "Würth, 25 V"
    assert inv.location("Bin µ") is not None


# ----- with the parts tables ---------------------------------------------------

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
            return ([loc.path for loc in view.locations()],
                    [(i.name, i.on_hand, i.locations, i.tags) for i in view.items()])
        finally:
            db.close()

    item = inv.add_item("Nano Every", location="C12/5", quantity=6, tags=["arduino"])
    assert seen() == (["C12", "C12/5"], [("Nano Every", 6, ("C12/5",), ("arduino",))])
    inv.adjust_stock(item.stock[0].id, -1)
    inv.rename_location("C12", "C13")
    assert seen() == (["C13", "C13/5"], [("Nano Every", 5, ("C13/5",), ("arduino",))])
    with pytest.raises(InventoryError):
        inv.add_item("x", location="New/Drawer", tags=["t" * 65])
    assert seen() == (["C13", "C13/5"], [("Nano Every", 5, ("C13/5",), ("arduino",))])
    inv.delete_item(item.id)
    assert seen() == (["C13", "C13/5"], [])


def test_a_long_lived_reader_sees_what_others_write(tmp_path):
    # The web app keeps one connection open: each read must see the writes
    # other connections committed since its last one.
    inv, other = _second_connection(tmp_path)
    assert inv.items() == []
    db = other()
    try:
        InventoryDb(db).add_item("Nano Every", location="school/17", quantity=3)
    finally:
        db.close()
    assert [i.name for i in inv.items()] == ["Nano Every"]
    assert [loc.path for loc in inv.locations()] == ["school", "school/17"]


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
    # A location moved under a newer one has a smaller id than its parent;
    # the copy must still insert the parent first.
    inv.add_location("Room 2", "school")
    inv.move_location("school/C12", "school/Room 2")
    counts = pull(inv.db, tmp_path / "copy.sqlite")
    assert (counts["location"], counts["item"], counts["item_tag"], counts["stock"]) == (
        6, 3, 2, 3)
    copy = InventoryDb(PartsDb.open_read_only(tmp_path / "copy.sqlite"))
    assert [loc.path for loc in copy.locations()] == [
        "school", "school/17", "school/Room 2", "school/Room 2/C12",
        "school/Room 2/C12/3", "school/Room 2/C12/5"]
    assert [i.name for i in copy.items("school/Room 2")] == ["1k 1/2W carbon film",
                                                             "SPDT slide switch"]
    assert copy.items("school/Room 2/C12/3")[0].tags == ("carbon film", "THT")
    assert copy.items("school/Room 2/C12/3")[0].on_hand == 200


def test_pull_from_a_database_without_the_inventory(tmp_path):
    db = new_db(now=lambda: STAMP)
    counts = pull(db, tmp_path / "copy.sqlite")
    assert all(counts[t] == 0 for t in INVENTORY_TABLES)
