"""Tests for the import of the master component list (inventoryimport.py,
INVENTORY_PLAN.md step 2), on SQLite and, when configured, MariaDB."""
from __future__ import annotations

from pathlib import Path

import pytest

from kicadtools.inventorydb import InventoryDb
from kicadtools.inventoryimport import (InventoryImportError, columns, convert, convert_row, import_items, read_table)
from tests.dbbackends import BACKENDS, current, MARIADB, new_db, use_backend

STAMP = "2026-11-23T12:00:00+00:00"
MASTER = Path(__file__).resolve().parents[3] / "Libraries" / "Master Component List.numbers"

HEADER = ["Component Type", "Comp Number", "Value", "Tolerance", "Package", "Description",
          "Approx Stock", "Manufacturer PN", "DigiKey PN"]


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


def _row(*cells, line=2):
    row = dict(zip(HEADER, cells))
    row["__line__"] = line
    return row


def _one(*cells):
    row = _row(*cells)
    return convert_row(row, columns([row])[0])


# ----- converting a row ----------------------------------------------------

def test_a_resistor_row():
    item = _one("R", 5.0, "4.7k", 0.01, 603.0, None, 100.0, "RMCF0603FT4K70",
                "RMCF0603FT4K70CT-ND")
    assert (item.name, item.bin, item.quantity, item.category) == (
        "4.7k 0603", "5", 100, "Resistor")
    assert (item.description, item.mpn, item.digikey_pn) == (
        "1%", "RMCF0603FT4K70", "RMCF0603FT4K70CT-ND")


def test_a_resistor_value_stored_as_a_number_gets_ohms():
    assert _one("R", 3.0, 330.0, 0.05, 603.0, None, 100.0, "x", None).name == "330Ω 0603"
    assert _one("R", 1.0, 0.0, None, 603.0, None, 50.0, "x", None).name == "0Ω 0603"


def test_a_capacitor_row_has_dielectric_and_voltage():
    item = _one("C", 57.0, "0.1uF", "X7R", 603.0, 50.0, 100.0, "CL10B104MB8NNNC", None)
    assert (item.name, item.description, item.category) == ("0.1uF 0603", "X7R, 50V",
                                                             "Capacitor")
    assert _one("C", None, "100uF", 0.2, "Radial", 16.0, 50.0, "x", None).description == \
        "20%, 16V"


def test_an_inductor_description_number_is_its_current():
    assert _one("L", 34.0, "2.2uH", None, None, 3.5, 12.0, "x", None).description == "3.5A"


def test_text_cells_are_kept():
    item = _one("U", 107.0, "MCP23017", None, "29QFN", "I/O Expander", 5.0,
                "MCP23017T-E/ML", None)
    assert (item.name, item.description, item.category) == ("MCP23017 29QFN",
                                                             "I/O Expander", "IC")
    assert _one("Z", None, " 1A ", None, None, None, 1.0, None, None).category == "Z"


def test_bin_numbers_and_rows_without_a_bin():
    assert _one("R", 10001.0, "1K", 0.05, 402.0, None, 90.0, "x", None).bin == "10001"
    assert _one("C", None, "47pF", None, 603.0, 25.0, 50.0, "x", None).bin is None


def test_a_part_number_stored_as_a_number_keeps_every_digit():
    assert _one("C", None, "150uF", None, None, 50.0, 30.0, 860160674023.0, None).mpn == \
        "860160674023"


def test_csv_text_converts_as_numbers_do():
    item = _one("R", "3", "330", "0.05", "603", "", "100", "CR0603", "")
    assert (item.name, item.bin, item.description, item.quantity) == (
        "330Ω 0603", "3", "5%", 100)
    assert _one("C", "57", "0.1uF", "X7R", "603", "50", "100", "x", "").description == \
        "X7R, 50V"


def test_a_row_without_a_value_is_skipped():
    rows = [_row("R", 1.0, "1k", None, 603.0, None, 5.0, None, None),
            _row("R", 2.0, None, None, 603.0, None, 5.0, None, None, line=3)]
    items, report = convert(rows)
    assert [i.name for i in items] == ["1k 0603"] and report.skipped == ["row 3: no value"]


def test_a_table_without_a_value_column_is_refused():
    with pytest.raises(InventoryImportError, match="no Value column"):
        convert([{"Part": "x", "__line__": 2}])


def test_unknown_columns_are_reported(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("Value,Comp Number,Colour\n1k,3,red\n,,\n2k,,\n", encoding="utf-8")
    rows = read_table(path)
    assert [r["__line__"] for r in rows] == [2, 4]        # the blank row is dropped
    items, report = convert(rows)
    assert [(i.name, i.bin) for i in items] == [("1k", "3"), ("2k", None)]
    assert report.ignored_columns == ["Colour"]


def test_read_table_refuses_other_files(tmp_path):
    with pytest.raises(InventoryImportError, match="no such file"):
        read_table(tmp_path / "missing.csv")
    (tmp_path / "list.xlsx").write_bytes(b"")
    with pytest.raises(InventoryImportError, match="not a .numbers or .csv"):
        read_table(tmp_path / "list.xlsx")


# ----- importing -----------------------------------------------------------

def _items():
    rows = [
        _row("R", 5.0, "4.7k", 0.01, 603.0, None, 100.0, "RMCF0603FT4K70", "RMCF0603FT4K70CT-ND"),
        _row("C", 57.0, "0.1uF", "X7R", 603.0, 50.0, 100.0, "CL10B104MB8NNNC", None, line=3),
        _row("R", 10001.0, "1K", 0.05, 402.0, None, 90.0, "ERA-2AED102X", None, line=4),
        _row("C", None, "47pF", None, 603.0, 25.0, 50.0, "CC0603JRNPO8BN470", None, line=5),
    ]
    return convert(rows)


def test_import_fills_locations_items_and_stock():
    inv = InventoryDb(new_db(now=lambda: STAMP))
    items, report = _items()
    report = import_items(inv, items, location="school", report=report)
    assert (report.items, report.bins, report.unbinned) == (4, 3, ["47pF 0603"])
    assert [loc.path for loc in inv.locations()] == [
        "school", "school/5", "school/57", "school/10001", "unknown"]
    # A location is made with its name only; the rest is set on it later.
    assert all((loc.description, loc.notes) == (None, None) for loc in inv.locations())
    inv.update_location("school/10001", notes="Partial reel")
    assert inv.location("school/10001").notes == "Partial reel"
    capacitor = inv.items("school/57")[0]
    assert (capacitor.name, capacitor.on_hand, capacitor.description, capacitor.category) == (
        "0.1uF 0603", 100, "X7R, 50V", "Capacitor")
    # No Comp Number: a stock row in "unknown" with the quantity.
    assert [(i.name, i.on_hand) for i in inv.items("unknown")] == [("47pF 0603", 50)]


def test_dry_run_writes_nothing():
    inv = InventoryDb(new_db(now=lambda: STAMP))
    items, _ = _items()
    report = import_items(inv, items, location="school", dry_run=True)
    assert report.items == 4 and report.dry_run
    assert inv.locations() == [] and inv.items() == []


def test_import_refuses_an_inventory_that_holds_something():
    inv = InventoryDb(new_db(now=lambda: STAMP))
    inv.add_location("C12")
    items, _ = _items()
    with pytest.raises(InventoryImportError, match="already holds"):
        import_items(inv, items, location="school")
    with pytest.raises(InventoryImportError, match="location"):
        import_items(InventoryDb(new_db()), items, location=" ")
    with pytest.raises(InventoryImportError, match="location"):
        import_items(InventoryDb(new_db()), items, location="a/b")


def test_a_bad_row_rolls_the_whole_import_back():
    inv = InventoryDb(new_db(now=lambda: STAMP))
    items, _ = _items()
    bad = convert([_row("R", 9.0, "x" * 300, None, None, None, 1.0, None, None, line=9)])[0]
    with pytest.raises(InventoryImportError, match="row 9"):
        import_items(inv, items + bad, location="school")
    assert inv.locations() == [] and inv.items() == []


@pytest.mark.skipif(not MASTER.exists(), reason="the master component list is not checked out")
def test_the_master_component_list_converts():
    items, report = convert(read_table(MASTER))
    assert len(items) == 122 and report.skipped == [] and report.ignored_columns == []
    assert sum(i.bin is None for i in items) == 9
    assert len({i.bin for i in items if i.bin}) == 113
    if current() == MARIADB:
        inv = InventoryDb(new_db(now=lambda: STAMP))
        assert import_items(inv, items, location="school", dry_run=True).items == 122


def test_cli_import(tmp_path, monkeypatch):
    if current() == MARIADB:
        pytest.skip("CLI wiring only; the destination is a SQLite file")
    from kicadtools import cli, dbconfig
    path = tmp_path / "list.csv"
    path.write_text(",".join(HEADER) + "\nR,5,4.7k,0.01,603,,100,RMCF0603FT4K70,\n"
                    "C,,47pF,,603,25,50,CC0603,\n", encoding="utf-8")
    parts = tmp_path / "parts.sqlite"
    config = dbconfig.DbConfig(backend=dbconfig.SQLITE, sqlite_path=parts, source="test")
    monkeypatch.setattr(dbconfig, "load_config", lambda: config)
    assert cli.main(["inventory", "import", str(path), "--location", "school",
                     "--dry-run"]) == 0
    assert cli.main(["inventory", "import", str(path), "--location", "school"]) == 0
    assert cli.main(["inventory", "import", str(path), "--location", "school"]) == 1
    from kicadtools.partsdb import PartsDb
    inv = InventoryDb(PartsDb(parts))
    assert [(i.name, i.locations) for i in inv.items()] == [("4.7k 0603", ("school/5",)),
                                                           ("47pF 0603", ("unknown",))]


def test_cli_migrate(tmp_path, monkeypatch, capsys):
    if current() == MARIADB:
        pytest.skip("CLI wiring only; the database is a SQLite file")
    from kicadtools import cli, dbconfig
    from kicadtools.inventorydb import layout
    from kicadtools.inventorymigrate import create_v1
    from kicadtools.partsdb import PartsDb
    parts = tmp_path / "parts.sqlite"
    db = PartsDb(parts, now=lambda: STAMP)
    create_v1(db)
    with db._tx():
        db._execute("INSERT INTO bin (id, name, path, site, updated_at)"
                    " VALUES (1, '17', '17', 'school', ?)", (STAMP,))
        db._execute("INSERT INTO item (name, quantity, bin_id, created_at, updated_at)"
                    " VALUES ('4.7k 0603', 100, 1, ?, ?)", (STAMP, STAMP))
    db.close()
    config = dbconfig.DbConfig(backend=dbconfig.SQLITE, sqlite_path=parts, source="test")
    monkeypatch.setattr(dbconfig, "load_config", lambda: config)
    monkeypatch.setattr(dbconfig, "backups_dir", lambda: tmp_path / "backups")
    assert cli.main(["inventory", "migrate", "--dry-run"]) == 0
    assert "2 locations" in capsys.readouterr().out
    assert cli.main(["inventory", "migrate"]) == 0
    out = capsys.readouterr().out
    assert "converted" in out and "backup:" in out
    assert len(list((tmp_path / "backups").glob("before-migrate-*.sqlite"))) == 1
    assert cli.main(["inventory", "migrate"]) == 1                # already converted
    db = PartsDb(parts)
    assert layout(db) == "2"
    assert InventoryDb(db).items()[0].locations == ("school/17",)
