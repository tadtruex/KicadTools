"""Tests for converting the inventory's first layout (bins) to locations and
stock rows (inventorymigrate.py), on SQLite and, when configured, MariaDB."""
from __future__ import annotations

import io

import pytest

from kicadtools.dbbackup import pull
from kicadtools.inventorydb import InventoryDb, InventoryLayoutError, layout
from kicadtools.inventorymigrate import (UNPLACED, MigrationError, copy_v1_rows, create_v1,
                                         migrate, plan)
from kicadtools.partsdb import PartsDb
from tests.dbbackends import BACKENDS, MARIADB, current, new_db, use_backend

STAMP = "2026-11-25T12:00:00+00:00"
OLD = "2026-11-23T10:00:00+00:00"


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


def _jpeg() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, "JPEG")
    return out.getvalue()


def _bin(db, id, path, parent=None, site=None, description=None, location=None, notes=None):
    db._execute("INSERT INTO bin (id, parent_id, name, path, site, description, location,"
                " notes, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (id, parent, path.rsplit("/", 1)[-1], path, site, description, location,
                 notes, OLD))


def _item(db, id, name, bin_id=None, quantity=None, mpn=None):
    db._execute("INSERT INTO item (id, name, mpn, quantity, bin_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)", (id, name, mpn, quantity, bin_id, OLD, OLD))


def _first_layout(pictures=True):
    """A database as release kt_v0.1a left it, shaped like the real one:
    top-level numbered boxes at school, items one per box, some in no box;
    plus a cabinet with drawers to cover nesting."""
    db = new_db(now=lambda: STAMP)
    create_v1(db, pictures=pictures)
    with db._tx():
        _bin(db, 1, "17", site="school")
        _bin(db, 2, "10001", site="school", notes="partial reel")
        _bin(db, 5, "C12", site="school")
        _bin(db, 3, "C12/5", parent=5)                 # inherits school
        _bin(db, 4, "C12/5/bag", parent=3, site="School")   # same site, other case
        _item(db, 10, "4.7k 0603", bin_id=1, quantity=100, mpn="RMCF0603FT4K70")
        _item(db, 11, "0.1uF 0603 reel", bin_id=2, quantity=3000)
        _item(db, 12, "SPDT slide switch", bin_id=4, quantity=12)
        _item(db, 13, "100uF Radial", quantity=50)     # in no bin
        _item(db, 14, "Gecko BT Module", quantity=None)
        db._execute("INSERT INTO item_tag (item_id, tag) VALUES (10, 'smd'), (10, 'resistor')")
        if pictures:
            db._execute("INSERT INTO item_picture (item_id, position, content_type, width,"
                        " height, data, thumb, caption, created_at)"
                        " VALUES (12, 0, 'image/jpeg', 8, 8, ?, ?, NULL, ?)",
                        (_jpeg(), _jpeg(), OLD))
    return db


def test_the_first_layout_becomes_locations_and_stock_rows():
    db = _first_layout()
    done = migrate(db)
    assert layout(db) == "2"
    assert done.tops == ["school", UNPLACED]
    inv = InventoryDb(db)
    assert [loc.path for loc in inv.locations()] == [
        "school", "school/17", "school/10001", "school/C12", "school/C12/5",
        "school/C12/5/bag", UNPLACED]
    # Bins keep their ids and their fields; a stock row is where the item was.
    assert inv.location("school/17").id == 1
    assert inv.location("school/10001").notes == "partial reel"
    assert inv.location("school/C12/5").parent_id == 5
    by_name = {i.name: i for i in inv.items()}
    assert [(s.location_path, s.quantity) for s in by_name["4.7k 0603"].stock] == [
        ("school/17", 100)]
    assert by_name["4.7k 0603"].tags == ("resistor", "smd")
    assert by_name["4.7k 0603"].updated_at == OLD
    assert by_name["SPDT slide switch"].locations == ("school/C12/5/bag",)
    assert len(by_name["SPDT slide switch"].pictures) == 1
    # In no bin: a row in "unknown" holding the quantity.
    assert [(s.location_path, s.quantity) for s in by_name["100uF Radial"].stock] == [
        (UNPLACED, 50)]
    assert by_name["Gecko BT Module"].stock[0].quantity is None
    assert [i.name for i in inv.items("school")] == [
        "0.1uF 0603 reel", "4.7k 0603", "SPDT slide switch"]
    assert inv.unplaced() == []


def test_the_old_columns_and_table_are_gone_and_the_keys_still_hold():
    db = _first_layout()
    migrate(db)
    inv = InventoryDb(db)
    assert "bin_id" not in inv._columns("item") and "quantity" not in inv._columns("item")
    assert not db._execute(db._dialect.has_table(), ("bin",)).fetchone()
    # Tags, pictures and stock still point at the (rebuilt, on SQLite) item.
    inv.delete_item(10)
    inv.delete_item(12)
    assert inv.tags() == []
    assert db._execute("SELECT COUNT(*) FROM item_picture").fetchone()[0] == 0
    # New rows work as on any new database.
    item = inv.add_item("Nano Every", location="home/C3", quantity=2)
    assert item.id > 14 and inv.location("home").id > 5


def test_a_dry_run_writes_nothing():
    db = _first_layout()
    p = migrate(db, dry_run=True)
    assert (len(p.locations), p.items, len(p.stock)) == (7, 5, 5)
    assert p.unplaced == ["100uF Radial", "Gecko BT Module"]
    assert layout(db) == "1"
    assert not db._execute(db._dialect.has_table(), ("location",)).fetchone()
    assert "2 items in no bin" in p.summary()


def test_a_release_without_pictures_migrates_too():
    db = _first_layout(pictures=False)
    migrate(db)
    inv = InventoryDb(db)          # creates item_picture
    assert inv.pictures(12) == []


def test_free_text_location_goes_into_the_description():
    db = new_db(now=lambda: STAMP)
    create_v1(db)
    with db._tx():
        _bin(db, 1, "C1", site="home", description="Small drawers", location="north wall")
        _bin(db, 2, "C2", site="home", location="by the door")
    p = migrate(db)
    inv = InventoryDb(db)
    assert inv.location("home/C1").description == "Small drawers\n\nnorth wall"
    assert inv.location("home/C2").description == "by the door"
    assert p.merged_text == ["C1", "C2"]


def test_several_sites_and_a_bin_without_one():
    db = new_db(now=lambda: STAMP)
    create_v1(db)
    with db._tx():
        _bin(db, 1, "17", site="school")
        _bin(db, 2, "Shelf", site="barn")
        _bin(db, 3, "Loose")                           # no site: stays at the top
        _item(db, 10, "x", bin_id=3, quantity=1)
    migrate(db)
    inv = InventoryDb(db)
    assert [loc.path for loc in inv.children()] == ["barn", "Loose", "school"]
    assert inv.item(10).locations == ("Loose",)


def test_what_cannot_be_converted_is_refused_and_nothing_written():
    db = new_db(now=lambda: STAMP)
    create_v1(db)
    with db._tx():
        _bin(db, 1, "C12", site="school")
        _bin(db, 2, "C12/5", parent=1, site="home")
    with pytest.raises(MigrationError, match="site 'home' inside a bin at 'school'"):
        migrate(db)
    assert layout(db) == "1"
    db2 = new_db(now=lambda: STAMP)
    create_v1(db2)
    with db2._tx():
        _bin(db2, 1, "17", site="school")
        _bin(db2, 2, "school")                         # would clash with the site
    with pytest.raises(MigrationError, match="school"):
        migrate(db2)
    assert layout(db2) == "1"


def test_migrate_refuses_a_converted_or_empty_database():
    db = new_db(now=lambda: STAMP)
    with pytest.raises(MigrationError, match="no inventory"):
        migrate(db)
    InventoryDb(db)
    with pytest.raises(MigrationError, match="already converted"):
        migrate(db)
    with pytest.raises(MigrationError):
        plan(db)


def test_pull_copies_an_unconverted_database_as_it_is(tmp_path):
    db = _first_layout()
    counts = pull(db, tmp_path / "copy.sqlite")
    assert (counts["bin"], counts["item"], counts["item_tag"], counts["item_picture"]) == (
        5, 5, 2, 1)
    copy = PartsDb(tmp_path / "copy.sqlite", now=lambda: STAMP)
    assert layout(copy) == "1"
    with pytest.raises(InventoryLayoutError):
        InventoryDb(copy)
    migrate(copy)                                      # and the copy converts
    assert len(InventoryDb(copy).locations()) == 7
    copy.close()


def test_copy_v1_rows_then_migrate(tmp_path):
    # The test copy: the real database's first-layout rows, then converted.
    source = _first_layout()
    dest = PartsDb(tmp_path / "test.sqlite", now=lambda: STAMP)
    create_v1(dest)
    counts = copy_v1_rows(source, dest)
    assert counts == {"bin": 5, "item": 5, "item_tag": 2, "item_picture": 1}
    migrate(dest)
    assert {i.name: i.locations for i in InventoryDb(dest).items()}["SPDT slide switch"] == (
        "school/C12/5/bag",)
    dest.close()


@pytest.mark.parametrize("stop", ["fill", "alter"])
def test_a_mariadb_run_that_stopped_is_finished_by_the_next(stop):
    if current() != MARIADB:
        pytest.skip("MariaDB only: SQLite runs in one transaction")
    db = _first_layout()
    migrate(db, _stop_after=stop)
    assert layout(db) == "partial"
    with pytest.raises(InventoryLayoutError, match="stopped part way"):
        InventoryDb(db)
    migrate(db)
    assert layout(db) == "2"
    inv = InventoryDb(db)
    assert len(inv.locations()) == 7 and len(inv.items()) == 5
    assert sum(len(i.stock) for i in inv.items()) == 5
