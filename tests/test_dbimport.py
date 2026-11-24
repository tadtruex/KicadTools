"""Tests for the one-time import into the server (dbimport.py, DB_PLAN.md
step 5), on SQLite and, when configured, MariaDB (tests/dbbackends.py)."""
from __future__ import annotations

import pytest

from kicadtools.dbimport import DbImportError, import_parts, imported_name
from kicadtools.partsdb import IcChoice, PartChoice, PartParams, PartsDb
from tests.dbbackends import BACKENDS, MARIADB, current, new_db, use_backend

STAMP = "2026-11-23T12:00:00+00:00"


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


def _source(path, *, reference="C1"):
    src = PartsDb(path, now=lambda: STAMP)
    cap = src.accept_part(PartChoice(
        kind="capacitor", mpn="CL10B104KB8NNNC", manufacturer="Samsung", package="0603",
        params=PartParams(100e-9, 50.0, "X7R", 10.0), digikey_pn="1276-1000-1-ND",
        lcsc="C14663", jlc_library="basic", description="100nF µ ± Würth"))
    src.reject_part(PartChoice(
        kind="resistor", mpn="RC0603FR-0710KL", manufacturer="Yageo", package="0603",
        params=PartParams(10e3, 0.1, "", 1.0)))
    ic = src.accept_part(IcChoice(mpn="L7805CV", manufacturer="STMicroelectronics",
                                  package="TO-220", package_case="TO-220-3", pins=3,
                                  digikey_pn="497-1443-5-ND", lcsc="C111887"))
    src.record_usage(cap, "LabBoard/LabBoard.kicad_sch", [reference, "C2"])
    src.record_usage(ic, "LabBoard/LabBoard.kicad_sch", ["U1"])
    src.set_setting("cap.voltage_min", "16V")
    src.close()
    return cap, ic


def test_import_keeps_ids_rows_and_rotations(tmp_path):
    path = tmp_path / "parts.sqlite"
    cap, ic = _source(path)
    dest = new_db(now=lambda: STAMP)
    report = import_parts(path, dest, {"C14663": 90, "C0": 360})
    assert report.source == report.dest
    assert report.dest["part"] == 3 and report.dest["usage"] == 3
    assert report.dest["part_rotation"] == 1            # 360° is no override
    hits = dest.find_parts(kind="capacitor", nominal=100e-9, package="0603", rating_min=None,
                           dielectrics=(), tolerance_max_pct=None)
    assert [(h.part_id, h.usage_count, h.description) for h in hits] == [(cap, 2, "100nF µ ± Würth")]
    assert dest.find_ics("l7805cv")[0].part_id == ic
    assert dest.is_rejected("Yageo", "RC0603FR-0710KL")
    assert dest.get_setting("cap.voltage_min") == "16V"
    assert dest.rotations() == {"C14663": 90.0}
    # A part accepted after the import gets an id above the imported ones.
    new = dest.accept_part(PartChoice(
        kind="capacitor", mpn="NEW", manufacturer="Murata", package="0402",
        params=PartParams(1e-6, 10.0, "X5R", 10.0)))
    assert new > max(cap, ic)


def test_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "parts.sqlite"
    _source(path)
    dest = new_db(now=lambda: STAMP)
    report = import_parts(path, dest, {"C1": 180}, dry_run=True)
    assert report.dest["part"] == 3 and report.dry_run
    assert dest.settings() == {} and dest.rotations() == {}
    assert dest._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0


def test_refuses_a_database_that_holds_data(tmp_path):
    path = tmp_path / "parts.sqlite"
    _source(path)
    dest = new_db(now=lambda: STAMP)
    dest.set_setting("res.tolerance", "1%")
    with pytest.raises(DbImportError, match="already holds data .*settings 1"):
        import_parts(path, dest)


def test_refuses_another_schema_version(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    conn.commit(); conn.close()
    with pytest.raises(DbImportError, match="schema version 1"):
        import_parts(path, new_db())


def test_a_value_the_server_refuses_rolls_everything_back(tmp_path):
    if current() != MARIADB:
        pytest.skip("strict column lengths are MariaDB's")
    path = tmp_path / "parts.sqlite"
    _source(path, reference="R" * 40)                  # usage.reference is VARCHAR(32)
    dest = new_db(now=lambda: STAMP)
    with pytest.raises(Exception):
        import_parts(path, dest)
    assert dest._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0


def test_imported_name(tmp_path):
    src = tmp_path / "parts.sqlite"
    assert imported_name(src).name == "parts.sqlite.imported"
    (tmp_path / "parts.sqlite.imported").touch()
    assert imported_name(src).name == "parts.sqlite.imported-2"


def test_rotations_in_the_source_file_are_imported_and_win(tmp_path):
    # A file written by `kicadtools db pull` carries its own part_rotation
    # table; those overrides reach the destination, and win over the cache's.
    path = tmp_path / "parts-export.sqlite"
    _source(path)
    src = PartsDb(path)
    src.set_rotation("C14663", 90)
    src.set_rotation("C111887", 270)
    src.close()
    dest = new_db(now=lambda: STAMP)
    report = import_parts(path, dest, {"C14663": 180, "C2040": 45})
    assert dest.rotations() == {"C14663": 90.0, "C111887": 270.0, "C2040": 45.0}
    assert report.source["part_rotation"] == report.dest["part_rotation"] == 3


def test_pull_then_import_round_trip(tmp_path):
    from kicadtools.dbbackup import pull
    first = tmp_path / "first.sqlite"
    _source(first)
    db = PartsDb(first)
    db.set_rotation("C14663", 90)
    export = tmp_path / "export.sqlite"
    counts = pull(db, export)
    db.close()
    dest = new_db(now=lambda: STAMP)
    report = import_parts(export, dest)
    assert dest.rotations() == {"C14663": 90.0}
    assert {t: report.dest[t] for t in counts} == counts


def _run_cli_import(tmp_path, monkeypatch, argv):
    from kicadtools import cli, dbconfig, dbconnect
    from kicadtools.dbconnect import DbSession
    dest = PartsDb(tmp_path / "server.sqlite")
    config = dbconfig.DbConfig(backend=dbconfig.MARIADB, source=str(tmp_path / "config.toml"))
    monkeypatch.setattr(dbconfig, "load_config", lambda: config)
    monkeypatch.setattr(dbconnect, "open_parts_db", lambda c, ask: DbSession(dest, c))
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "parts.sqlite"))
    assert cli.main(["db", "import", "--cache", str(tmp_path / "none.sqlite")] + argv) == 0


def test_cli_renames_only_the_default_file(tmp_path, monkeypatch):
    if current() == MARIADB:
        pytest.skip("CLI wiring only; the destination is a stand-in")
    export = tmp_path / "export.sqlite"
    _source(export)
    _run_cli_import(tmp_path, monkeypatch, ["--from", str(export)])
    assert export.exists()


def test_cli_renames_the_default_file(tmp_path, monkeypatch):
    if current() == MARIADB:
        pytest.skip("CLI wiring only; the destination is a stand-in")
    default = tmp_path / "parts.sqlite"
    _source(default)
    _run_cli_import(tmp_path, monkeypatch, [])
    assert not default.exists() and (tmp_path / "parts.sqlite.imported").exists()
