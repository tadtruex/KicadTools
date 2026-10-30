"""Tests for the local parts database (kicadtools.partsdb)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kicadtools.partsdb import (
    SCHEMA_VERSION,
    CapacitorChoice,
    CapacitorParams,
    PartsDb,
    default_path,
)

STAMP = "2026-10-29T12:00:00+00:00"


def _db() -> PartsDb:
    return PartsDb(":memory:", now=lambda: STAMP)


def _cap(mpn: str = "CL10B104KB8NNNC", *, manufacturer: str = "Samsung",
         package: str = "0603", c: float = 100e-9, v: float = 50.0,
         diel: str = "X7R", tol=10.0, dk="1276-1000-1-ND",
         lcsc="C14663", lib="basic") -> CapacitorChoice:
    return CapacitorChoice(
        mpn=mpn, manufacturer=manufacturer, package=package,
        params=CapacitorParams(c, v, diel, tol),
        digikey_pn=dk, digikey_packaging="cut tape" if dk else None,
        lcsc=lcsc, jlc_library=lib,
        description="100nF 50V X7R 0603", datasheet_url="https://x/ds.pdf",
    )


def _find(db: PartsDb, **kw):
    args = dict(capacitance_f=100e-9, package="0603", voltage_min_v=16.0,
                dielectrics=["X7R", "X5R"], tolerance_max_pct=10.0)
    args.update(kw)
    return db.find_capacitors(**args)


def test_accept_find_round_trip():
    db = _db()
    pid = db.accept_capacitor(_cap())
    hits = _find(db)
    assert len(hits) == 1
    h = hits[0]
    assert h.part_id == pid
    assert (h.mpn, h.manufacturer, h.package) == ("CL10B104KB8NNNC", "Samsung", "0603")
    assert h.params == CapacitorParams(100e-9, 50.0, "X7R", 10.0)
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-1000-1-ND", "C14663", "basic")
    assert h.usage_count == 0
    assert h.description == "100nF 50V X7R 0603"
    assert h.datasheet_url == "https://x/ds.pdf"


def test_timestamps_from_injected_now():
    db = _db()
    pid = db.accept_capacitor(_cap())
    assert db.conn.execute(
        "SELECT decided_at FROM part WHERE id = ?", (pid,)).fetchone()[0] == STAMP
    stamps = {r[0] for r in db.conn.execute("SELECT verified_at FROM supplier_pn")}
    assert stamps == {STAMP}


def test_capacitance_band():
    db = _db()
    db.accept_capacitor(_cap())
    assert _find(db, capacitance_f=100.05e-9)      # 0.05 % off: equal
    assert _find(db, capacitance_f=99.95e-9)
    assert not _find(db, capacitance_f=100.2e-9)   # 0.2 % off: different
    assert not _find(db, capacitance_f=99.8e-9)
    assert not _find(db, capacitance_f=1e-6)


def test_package_filter():
    db = _db()
    db.accept_capacitor(_cap())
    assert not _find(db, package="0402")
    assert not _find(db, package="0805")


def test_voltage_floor():
    db = _db()
    db.accept_capacitor(_cap(v=25.0))
    assert _find(db, voltage_min_v=25.0)           # equal rating meets the floor
    assert _find(db, voltage_min_v=10.0)
    assert not _find(db, voltage_min_v=35.0)


def test_dielectric_set_normalized():
    db = _db()
    db.accept_capacitor(_cap("C0G-PART", diel="cog", tol=5.0))
    # stored normalized
    assert db.conn.execute("SELECT dielectric FROM capacitor").fetchone()[0] == "C0G"
    assert _find(db, dielectrics=["c0g"])
    assert _find(db, dielectrics=["COG", "NP0"])
    assert _find(db, dielectrics=[" C0G "])
    assert not _find(db, dielectrics=["X7R", "X5R"])
    assert _find(db, dielectrics=[])          # empty: any dielectric


def test_tolerance_max():
    db = _db()
    db.accept_capacitor(_cap(tol=10.0))
    assert _find(db, tolerance_max_pct=10.0)
    assert _find(db, tolerance_max_pct=20.0)
    assert not _find(db, tolerance_max_pct=5.0)
    assert _find(db, tolerance_max_pct=None)


def test_null_tolerance_fails_a_stated_max():
    db = _db()
    db.accept_capacitor(_cap(tol=None))
    assert not _find(db, tolerance_max_pct=20.0)
    hits = _find(db, tolerance_max_pct=None)
    assert len(hits) == 1 and hits[0].params.tolerance_pct is None


def test_rejected_never_found_and_reaccept_flips_back():
    db = _db()
    choice = _cap()
    pid = db.reject_capacitor(choice)
    assert db.is_rejected("Samsung", "CL10B104KB8NNNC")
    assert not _find(db)
    assert not _find(db, tolerance_max_pct=None, dielectrics=["X7R"])
    assert db.accept_capacitor(choice) == pid
    assert not db.is_rejected("Samsung", "CL10B104KB8NNNC")
    assert [h.part_id for h in _find(db)] == [pid]
    assert db.reject_capacitor(choice) == pid
    assert not _find(db)


def test_is_rejected_unknown_part():
    db = _db()
    assert not db.is_rejected("Samsung", "NOPE")
    db.accept_capacitor(_cap())
    assert not db.is_rejected("Samsung", "CL10B104KB8NNNC")


def test_upsert_updates_without_duplicating():
    db = _db()
    pid = db.accept_capacitor(_cap())
    pid2 = db.accept_capacitor(_cap(v=100.0, tol=5.0, dk="1276-9999-1-ND",
                                    lcsc="C99999", lib="extended"))
    assert pid2 == pid
    assert db.conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM capacitor").fetchone()[0] == 1
    rows = sorted(db.conn.execute(
        "SELECT supplier, pn, jlc_library FROM supplier_pn").fetchall())
    assert rows == [("digikey", "1276-9999-1-ND", None),
                    ("lcsc", "C99999", "extended")]
    h = _find(db, tolerance_max_pct=5.0)[0]
    assert h.params == CapacitorParams(100e-9, 100.0, "X7R", 5.0)
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-9999-1-ND", "C99999", "extended")


def test_same_mpn_other_manufacturer_is_separate():
    db = _db()
    a = db.accept_capacitor(_cap(manufacturer="A", dk=None, lcsc=None))
    b = db.accept_capacitor(_cap(manufacturer="B", dk=None, lcsc=None))
    assert a != b
    assert len(_find(db)) == 2


def test_usage_ordering_then_basic_first_then_mpn():
    db = _db()
    ext = db.accept_capacitor(_cap("AAA", dk="d1", lcsc="C1", lib="extended"))
    basic = db.accept_capacitor(_cap("ZZZ", dk="d2", lcsc="C2", lib="basic"))
    basic2 = db.accept_capacitor(_cap("MMM", dk="d3", lcsc="C3", lib="basic"))
    # No usage: Basic first (MMM, ZZZ by MPN), then Extended.
    assert [h.part_id for h in _find(db)] == [basic2, basic, ext]
    db.record_usage(ext, "ECG/ECG.kicad_sch", ["C1", "C2"])
    db.record_usage(basic, "ECG/ECG.kicad_sch", ["C3"])
    hits = _find(db)
    assert [h.part_id for h in hits] == [ext, basic, basic2]
    assert [h.usage_count for h in hits] == [2, 1, 0]


def test_record_usage_replaces_existing_pair():
    db = _db()
    pid = db.accept_capacitor(_cap())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C1", "C2"])
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C2", "C3"])
    assert _find(db)[0].usage_count == 3


def test_settings_round_trip():
    db = _db()
    assert db.get_setting("cap.package") is None
    assert db.settings() == {}
    db.set_setting("cap.package", "0603")
    db.set_setting("cap.voltage_min", "16V")
    db.set_setting("cap.package", "0402")
    assert db.get_setting("cap.package") == "0402"
    assert db.settings() == {"cap.package": "0402", "cap.voltage_min": "16V"}


def test_schema_version_stamped():
    db = _db()
    row = db.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row[0] == str(SCHEMA_VERSION)


def test_foreign_keys_enabled():
    db = _db()
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_version_mismatch_raises(tmp_path: Path):
    path = tmp_path / "parts.sqlite"
    PartsDb(path, now=lambda: STAMP).close()
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError):
        PartsDb(path)


def test_file_db_persists_across_reopen(tmp_path: Path):
    path = tmp_path / "sub" / "dir" / "parts.sqlite"   # parent created
    db = PartsDb(path, now=lambda: STAMP)
    pid = db.accept_capacitor(_cap())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C1"])
    db.set_setting("cap.package", "0805")
    db.close()
    db = PartsDb(path, now=lambda: STAMP)
    hits = _find(db)
    assert [h.part_id for h in hits] == [pid]
    assert hits[0].usage_count == 1
    assert db.get_setting("cap.package") == "0805"
    db.close()


def test_default_now_is_iso_utc():
    db = PartsDb()
    pid = db.accept_capacitor(_cap())
    stamp = db.conn.execute(
        "SELECT decided_at FROM part WHERE id = ?", (pid,)).fetchone()[0]
    assert stamp.endswith("+00:00") and "T" in stamp and "." not in stamp


def test_default_path_honours_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "x.sqlite"))
    assert default_path() == tmp_path / "x.sqlite"
    monkeypatch.delenv("KICADTOOLS_PARTS_DB")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_path() == tmp_path / "Code" / "Boards" / "parts.sqlite"


def test_manufacturer_and_mpn_case_and_whitespace_insensitive():
    db = _db()
    pid = db.accept_capacitor(_cap())
    assert db.accept_capacitor(_cap(" cl10b104kb8nnnc ", manufacturer="SAMSUNG ")) == pid
    assert db.conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1
    db.reject_capacitor(_cap())
    assert db.is_rejected(" samsung", "CL10B104KB8NNNC ")


def test_empty_mpn_raises():
    db = _db()
    with pytest.raises(ValueError):
        db.accept_capacitor(_cap("  "))
    assert db.conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0


def test_none_or_empty_supplier_pn_keeps_existing_row():
    db = _db()
    pid = db.accept_capacitor(_cap())
    db.accept_capacitor(_cap(dk=None, lcsc=""))
    h = _find(db)[0]
    assert h.part_id == pid
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-1000-1-ND", "C14663", "basic")


def test_supplier_pn_and_library_normalized():
    db = _db()
    db.accept_capacitor(_cap(dk=" 1276-1000-1-ND ", lcsc=" C14663", lib=" Basic "))
    h = _find(db)[0]
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-1000-1-ND", "C14663", "basic")


def test_multi_code_dielectric_stored_and_matched():
    db = _db()
    db.accept_capacitor(_cap("NP0-PART", c=100e-12, diel="C0G/NP0", tol=5.0))
    assert db.conn.execute("SELECT dielectric FROM capacitor").fetchone()[0] == "C0G, NP0"
    kw = dict(capacitance_f=100e-12, tolerance_max_pct=5.0)
    assert _find(db, dielectrics=["NP0"], **kw)
    assert _find(db, dielectrics=["C0G"], **kw)
    assert _find(db, dielectrics=["C0G, NP0"], **kw)
    assert not _find(db, dielectrics=["X7R"], **kw)
    assert _find(db, dielectrics=["NP0"], **kw)[0].params.dielectric == "C0G, NP0"


def test_package_whitespace_ignored():
    db = _db()
    db.accept_capacitor(_cap(package=" 0603"))
    assert _find(db, package="0603 ")


def test_record_usage_unknown_part_violates_foreign_key():
    db = _db()
    with pytest.raises(sqlite3.IntegrityError):
        db.record_usage(999, "ECG/ECG.kicad_sch", ["C1"])


def test_record_usage_duplicate_references_counted_once():
    # A hierarchical subsheet instantiated twice yields two references for one
    # symbol; the same reference listed twice is one usage row.
    db = _db()
    pid = db.accept_capacitor(_cap())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C1", "C101", "C1"])
    assert _find(db)[0].usage_count == 2


def test_delete_part_cascades():
    db = _db()
    pid = db.accept_capacitor(_cap())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C1"])
    with db.conn:
        db.conn.execute("DELETE FROM part WHERE id = ?", (pid,))
    for table in ("capacitor", "supplier_pn", "usage"):
        assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_accept_is_one_transaction(monkeypatch):
    db = _db()

    def boom(*a, **k):
        raise RuntimeError("supplier write failed")

    monkeypatch.setattr(db, "_replace_pn", boom)
    with pytest.raises(RuntimeError):
        db.accept_capacitor(_cap())
    for table in ("part", "capacitor", "supplier_pn"):
        assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_schema_version_mismatch_leaves_file_untouched(tmp_path: Path):
    path = tmp_path / "parts.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema version 2"):
        PartsDb(path)
    conn = sqlite3.connect(str(path))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert tables == {"meta"}


def test_usable_from_another_thread():
    import threading

    db = _db()
    db.accept_capacitor(_cap())
    out = []
    t = threading.Thread(target=lambda: out.append(len(_find(db))))
    t.start()
    t.join()
    assert out == [1]


def test_no_voltage_floor_finds_any_rating():
    db = _db()
    db.accept_capacitor(_cap(v=6.3))
    assert not _find(db)                       # 16 V floor
    assert _find(db, voltage_min_v=None)
