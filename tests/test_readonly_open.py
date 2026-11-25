"""Opening a database read-only (``open_parts_db(..., read_only=True)``):
no schema statements, every write refused. ``copy_inventory_to_test.py``
reads the real database this way, so a develop checkout with new tables
does not add them to the database a release uses (``README_TEST.md``)."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from tests.dbbackends import mariadb_params, new_db, use_backend, MARIADB
from kicadtools.dbconfig import DbConfig
from kicadtools.dbconnect import open_parts_db
from kicadtools.inventorydb import InventoryDb
from kicadtools.partsdb import PartsDb

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "copy_inventory_to_test.py"


def _never_ask(reason):  # pragma: no cover - a test that asks has failed
    raise AssertionError(f"asked for a password: {reason}")


def _sqlite_tables(path: Path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


# ----- SQLite -----------------------------------------------------------------

def test_sqlite_read_only_runs_no_schema_and_refuses_writes(tmp_path):
    path = tmp_path / "parts.sqlite"
    PartsDb(path).close()                    # parts tables, no inventory tables
    before = _sqlite_tables(path)
    assert "bin" not in before

    session = open_parts_db(DbConfig(sqlite_path=path), _never_ask, read_only=True)
    try:
        assert session.db.read_only and session.read_only
        InventoryDb(session.db)              # would create location, item, stock …
        with pytest.raises(sqlite3.OperationalError):
            session.db._execute("INSERT INTO settings VALUES ('k', 'v')")
        again = session.connect_again()
        assert again.read_only
        again.close()
    finally:
        session.close()
    assert _sqlite_tables(path) == before


def test_sqlite_default_still_writes(tmp_path):
    path = tmp_path / "parts.sqlite"
    session = open_parts_db(DbConfig(sqlite_path=path), _never_ask)
    try:
        assert not session.db.read_only
        InventoryDb(session.db)
    finally:
        session.close()
    assert {"location", "item", "item_tag", "stock"} <= _sqlite_tables(path)


# ----- MariaDB ----------------------------------------------------------------

@pytest.fixture
def mariadb():
    if not mariadb_params():
        pytest.skip("no MariaDB test database (KICADTOOLS_TEST_DB_NAME)")
    yield from use_backend(MARIADB)


def _maria_tables(db: PartsDb) -> set:
    rows = db._execute("SELECT table_name FROM information_schema.tables"
                       " WHERE table_schema = DATABASE()").fetchall()
    return {r[0] for r in rows}


def test_mariadb_read_only_runs_no_schema_and_refuses_writes(mariadb):
    import pymysql

    new_db().close()                         # parts tables, no inventory tables
    ro = PartsDb.connect_mariadb(**mariadb_params(), read_only=True)
    try:
        before = _maria_tables(ro)
        assert "bin" not in before
        InventoryDb(ro)
        assert _maria_tables(ro) == before
        with pytest.raises(pymysql.err.MySQLError):
            ro._execute("INSERT INTO settings (\"key\", value) VALUES ('k', 'v')")
        ro.conn.rollback()

        # After a reconnect (the idle ping) the session is read-only again.
        ro.conn.close()
        ro._last_used -= 2 * PartsDb._IDLE_S
        with pytest.raises(pymysql.err.MySQLError):
            ro._execute("INSERT INTO settings (\"key\", value) VALUES ('k', 'v')")
        ro.conn.rollback()
    finally:
        ro.close()


# ----- the copy script ----------------------------------------------------------

def _load_script():
    spec = importlib.util.spec_from_file_location("copy_inventory_to_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(path: Path, db: Path) -> Path:
    path.write_text(f'[parts_db]\nbackend = "sqlite"\npath = "{db}"\n')
    return path


def _run_copy(tmp_path, monkeypatch, source: Path) -> Path:
    monkeypatch.delenv("KICADTOOLS_PARTS_DB", raising=False)
    monkeypatch.setenv("KICADTOOLS_CONFIG", str(_config(tmp_path / "real.toml", source)))
    target = tmp_path / "parts_test.sqlite"
    _load_script().main(str(_config(tmp_path / "test.toml", target)))
    return target


def _stock(target: Path) -> list:
    copy = PartsDb(target)
    try:
        return sorted((i.name, i.locations, i.on_hand) for i in InventoryDb(copy).items())
    finally:
        copy.close()


def test_copy_script_converts_a_first_layout_source_and_leaves_it_unchanged(
        tmp_path, monkeypatch, capsys):
    # The real database before the cutover: bins, release kt_v0.1a.
    from kicadtools.inventorymigrate import create_v1
    source = tmp_path / "parts.sqlite"
    db = PartsDb(source)
    create_v1(db, pictures=False)
    with db._tx():
        db._execute("INSERT INTO bin (id, name, path, site, updated_at)"
                    " VALUES (1, '57', '57', 'school', 'x')")
        db._execute("INSERT INTO item (name, quantity, bin_id, created_at, updated_at)"
                    " VALUES ('0.1uF 0603', 50, 1, 'x', 'x')")
        db._execute("INSERT INTO item (name, quantity, created_at, updated_at)"
                    " VALUES ('Nano Every', 3, 'x', 'x')")
    db.close()
    snapshot = source.read_bytes()

    target = _run_copy(tmp_path, monkeypatch, source)
    assert source.read_bytes() == snapshot
    out = capsys.readouterr().out
    assert "copied (first layout)" in out and "3 locations, 2 items, 2 stock rows" in out
    assert _stock(target) == [("0.1uF 0603", ("school/57",), 50),
                              ("Nano Every", ("unknown",), 3)]
    # Run again: the test database is emptied and made again.
    _run_copy(tmp_path, monkeypatch, source)
    assert len(_stock(target)) == 2


def test_copy_script_copies_a_converted_source(tmp_path, monkeypatch, capsys):
    source = tmp_path / "parts.sqlite"
    db = PartsDb(source)
    inv = InventoryDb(db)
    nano = inv.add_item("Nano Every", location="school/C12/5", quantity=3, tags=["arduino"])
    inv.add_stock(nano.id, "home", 1)
    inv.add_item("0.1uF 0603", location="school/57", quantity=50)
    db.close()
    snapshot = source.read_bytes()

    target = _run_copy(tmp_path, monkeypatch, source)
    assert source.read_bytes() == snapshot
    assert "copied (converted source)" in capsys.readouterr().out
    assert _stock(target) == [("0.1uF 0603", ("school/57",), 50),
                              ("Nano Every", ("home", "school/C12/5"), 4)]
