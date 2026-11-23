"""Tests for the local parts database (kicadtools.partsdb)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kicadtools.partsdb import (
    IC,
    SCHEMA_VERSION,
    DbIc,
    IcChoice,
    PartChoice,
    PartParams,
    PartsDb,
    default_path,
    norm_mpn,
)
from tests.dbbackends import BACKENDS, integrity_errors, new_db, sqlite_only, use_backend

STAMP = "2026-10-29T12:00:00+00:00"


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    """Every test runs on SQLite, and on MariaDB when one is configured
    (tests/dbbackends.py)."""
    yield from use_backend(request.param)


def _db() -> PartsDb:
    return new_db(now=lambda: STAMP)


def _cap(mpn: str = "CL10B104KB8NNNC", *, manufacturer: str = "Samsung",
         package: str = "0603", c: float = 100e-9, v: float = 50.0,
         diel: str = "X7R", tol=10.0, dk="1276-1000-1-ND",
         lcsc="C14663", lib="basic") -> PartChoice:
    return PartChoice(kind="capacitor",
        mpn=mpn, manufacturer=manufacturer, package=package,
        params=PartParams(c, v, diel, tol),
        digikey_pn=dk, digikey_packaging="cut tape" if dk else None,
        lcsc=lcsc, jlc_library=lib,
        description="100nF 50V X7R 0603", datasheet_url="https://x/ds.pdf",
    )


def _find(db: PartsDb, **kw):
    args = dict(nominal=100e-9, package="0603", rating_min=16.0,
                dielectrics=["X7R", "X5R"], tolerance_max_pct=10.0)
    args.update(kw)
    return db.find_parts(kind="capacitor", **args)


def test_accept_find_round_trip():
    db = _db()
    pid = db.accept_part(_cap())
    hits = _find(db)
    assert len(hits) == 1
    h = hits[0]
    assert h.part_id == pid
    assert (h.mpn, h.manufacturer, h.package) == ("CL10B104KB8NNNC", "Samsung", "0603")
    assert h.params == PartParams(100e-9, 50.0, "X7R", 10.0)
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-1000-1-ND", "C14663", "basic")
    assert h.usage_count == 0
    assert h.description == "100nF 50V X7R 0603"
    assert h.datasheet_url == "https://x/ds.pdf"


def test_timestamps_from_injected_now():
    db = _db()
    pid = db.accept_part(_cap())
    assert db._execute(
        "SELECT decided_at FROM part WHERE id = ?", (pid,)).fetchone()[0] == STAMP
    stamps = {r[0] for r in db._execute("SELECT verified_at FROM supplier_pn")}
    assert stamps == {STAMP}


def test_capacitance_band():
    db = _db()
    db.accept_part(_cap())
    assert _find(db, nominal=100.05e-9)      # 0.05 % off: equal
    assert _find(db, nominal=99.95e-9)
    assert not _find(db, nominal=100.2e-9)   # 0.2 % off: different
    assert not _find(db, nominal=99.8e-9)
    assert not _find(db, nominal=1e-6)


def test_package_filter():
    db = _db()
    db.accept_part(_cap())
    assert not _find(db, package="0402")
    assert not _find(db, package="0805")


def test_voltage_floor():
    db = _db()
    db.accept_part(_cap(v=25.0))
    assert _find(db, rating_min=25.0)           # equal rating meets the floor
    assert _find(db, rating_min=10.0)
    assert not _find(db, rating_min=35.0)


def test_dielectric_set_normalized():
    db = _db()
    db.accept_part(_cap("C0G-PART", diel="cog", tol=5.0))
    # stored normalized
    assert db._execute("SELECT dielectric FROM capacitor").fetchone()[0] == "C0G"
    assert _find(db, dielectrics=["c0g"])
    assert _find(db, dielectrics=["COG", "NP0"])
    assert _find(db, dielectrics=[" C0G "])
    assert not _find(db, dielectrics=["X7R", "X5R"])
    assert _find(db, dielectrics=[])          # empty: any dielectric


def test_tolerance_max():
    db = _db()
    db.accept_part(_cap(tol=10.0))
    assert _find(db, tolerance_max_pct=10.0)
    assert _find(db, tolerance_max_pct=20.0)
    assert not _find(db, tolerance_max_pct=5.0)
    assert _find(db, tolerance_max_pct=None)


def test_null_tolerance_fails_a_stated_max():
    db = _db()
    db.accept_part(_cap(tol=None))
    assert not _find(db, tolerance_max_pct=20.0)
    hits = _find(db, tolerance_max_pct=None)
    assert len(hits) == 1 and hits[0].params.tolerance_pct is None


def test_rejected_never_found_and_reaccept_flips_back():
    db = _db()
    choice = _cap()
    pid = db.reject_part(choice)
    assert db.is_rejected("Samsung", "CL10B104KB8NNNC")
    assert not _find(db)
    assert not _find(db, tolerance_max_pct=None, dielectrics=["X7R"])
    assert db.accept_part(choice) == pid
    assert not db.is_rejected("Samsung", "CL10B104KB8NNNC")
    assert [h.part_id for h in _find(db)] == [pid]
    assert db.reject_part(choice) == pid
    assert not _find(db)


def test_is_rejected_unknown_part():
    db = _db()
    assert not db.is_rejected("Samsung", "NOPE")
    db.accept_part(_cap())
    assert not db.is_rejected("Samsung", "CL10B104KB8NNNC")


def test_upsert_updates_without_duplicating():
    db = _db()
    pid = db.accept_part(_cap())
    pid2 = db.accept_part(_cap(v=100.0, tol=5.0, dk="1276-9999-1-ND",
                                    lcsc="C99999", lib="extended"))
    assert pid2 == pid
    assert db._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1
    assert db._execute("SELECT COUNT(*) FROM capacitor").fetchone()[0] == 1
    rows = sorted(db._execute(
        "SELECT supplier, pn, jlc_library FROM supplier_pn").fetchall())
    assert rows == [("digikey", "1276-9999-1-ND", None),
                    ("lcsc", "C99999", "extended")]
    h = _find(db, tolerance_max_pct=5.0)[0]
    assert h.params == PartParams(100e-9, 100.0, "X7R", 5.0)
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-9999-1-ND", "C99999", "extended")


def test_same_mpn_other_manufacturer_is_separate():
    db = _db()
    a = db.accept_part(_cap(manufacturer="A", dk=None, lcsc=None))
    b = db.accept_part(_cap(manufacturer="B", dk=None, lcsc=None))
    assert a != b
    assert len(_find(db)) == 2


def test_usage_ordering_then_basic_first_then_mpn():
    db = _db()
    ext = db.accept_part(_cap("AAA", dk="d1", lcsc="C1", lib="extended"))
    basic = db.accept_part(_cap("ZZZ", dk="d2", lcsc="C2", lib="basic"))
    basic2 = db.accept_part(_cap("MMM", dk="d3", lcsc="C3", lib="basic"))
    # No usage: Basic first (MMM, ZZZ by MPN), then Extended.
    assert [h.part_id for h in _find(db)] == [basic2, basic, ext]
    db.record_usage(ext, "ECG/ECG.kicad_sch", ["C1", "C2"])
    db.record_usage(basic, "ECG/ECG.kicad_sch", ["C3"])
    hits = _find(db)
    assert [h.part_id for h in hits] == [ext, basic, basic2]
    assert [h.usage_count for h in hits] == [2, 1, 0]


def test_record_usage_replaces_existing_pair():
    db = _db()
    pid = db.accept_part(_cap())
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
    row = db._execute(
        'SELECT value FROM meta WHERE "key" = \'schema_version\'').fetchone()
    assert row[0] == str(SCHEMA_VERSION)


def test_foreign_keys_enabled():
    sqlite_only()
    db = _db()
    assert db._execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_version_mismatch_raises(tmp_path: Path):
    sqlite_only()
    path = tmp_path / "parts.sqlite"
    PartsDb(path, now=lambda: STAMP).close()
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError):
        PartsDb(path)


def test_file_db_persists_across_reopen(tmp_path: Path):
    sqlite_only()
    path = tmp_path / "sub" / "dir" / "parts.sqlite"   # parent created
    db = PartsDb(path, now=lambda: STAMP)
    pid = db.accept_part(_cap())
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
    pid = db.accept_part(_cap())
    stamp = db._execute(
        "SELECT decided_at FROM part WHERE id = ?", (pid,)).fetchone()[0]
    assert stamp.endswith("+00:00") and "T" in stamp and "." not in stamp


def test_default_path_honours_env(monkeypatch, tmp_path: Path):
    sqlite_only()
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "x.sqlite"))
    assert default_path() == tmp_path / "x.sqlite"
    monkeypatch.delenv("KICADTOOLS_PARTS_DB")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_path() == tmp_path / "Code" / "Boards" / "parts.sqlite"


def test_manufacturer_and_mpn_case_and_whitespace_insensitive():
    db = _db()
    pid = db.accept_part(_cap())
    assert db.accept_part(_cap(" cl10b104kb8nnnc ", manufacturer="SAMSUNG ")) == pid
    assert db._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1
    db.reject_part(_cap())
    assert db.is_rejected(" samsung", "CL10B104KB8NNNC ")


def test_empty_mpn_raises():
    db = _db()
    with pytest.raises(ValueError):
        db.accept_part(_cap("  "))
    assert db._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0


def test_none_or_empty_supplier_pn_keeps_existing_row():
    db = _db()
    pid = db.accept_part(_cap())
    db.accept_part(_cap(dk=None, lcsc=""))
    h = _find(db)[0]
    assert h.part_id == pid
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-1000-1-ND", "C14663", "basic")


def test_supplier_pn_and_library_normalized():
    db = _db()
    db.accept_part(_cap(dk=" 1276-1000-1-ND ", lcsc=" C14663", lib=" Basic "))
    h = _find(db)[0]
    assert (h.digikey_pn, h.lcsc, h.jlc_library) == ("1276-1000-1-ND", "C14663", "basic")


def test_multi_code_dielectric_stored_and_matched():
    db = _db()
    db.accept_part(_cap("NP0-PART", c=100e-12, diel="C0G/NP0", tol=5.0))
    assert db._execute("SELECT dielectric FROM capacitor").fetchone()[0] == "C0G, NP0"
    kw = dict(nominal=100e-12, tolerance_max_pct=5.0)
    assert _find(db, dielectrics=["NP0"], **kw)
    assert _find(db, dielectrics=["C0G"], **kw)
    assert _find(db, dielectrics=["C0G, NP0"], **kw)
    assert not _find(db, dielectrics=["X7R"], **kw)
    assert _find(db, dielectrics=["NP0"], **kw)[0].params.dielectric == "C0G, NP0"


def test_package_whitespace_ignored():
    db = _db()
    db.accept_part(_cap(package=" 0603"))
    assert _find(db, package="0603 ")


def test_record_usage_unknown_part_violates_foreign_key():
    db = _db()
    with pytest.raises(integrity_errors()):
        db.record_usage(999, "ECG/ECG.kicad_sch", ["C1"])


def test_record_usage_duplicate_references_counted_once():
    # A hierarchical subsheet instantiated twice yields two references for one
    # symbol; the same reference listed twice is one usage row.
    db = _db()
    pid = db.accept_part(_cap())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C1", "C101", "C1"])
    assert _find(db)[0].usage_count == 2


def test_delete_part_cascades():
    db = _db()
    pid = db.accept_part(_cap())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C1"])
    with db._tx():
        db._execute("DELETE FROM part WHERE id = ?", (pid,))
    for table in ("capacitor", "supplier_pn", "usage"):
        assert db._execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0


def test_accept_is_one_transaction(monkeypatch):
    db = _db()

    def boom(*a, **k):
        raise RuntimeError("supplier write failed")

    monkeypatch.setattr(db, "_replace_pn", boom)
    with pytest.raises(RuntimeError):
        db.accept_part(_cap())
    for table in ("part", "capacitor", "supplier_pn"):
        assert db._execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0


def test_schema_version_mismatch_leaves_file_untouched(tmp_path: Path):
    sqlite_only()
    path = tmp_path / "parts.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema version 3"):
        PartsDb(path)
    conn = sqlite3.connect(str(path))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert tables == {"meta"}


def test_usable_from_another_thread():
    import threading

    db = _db()
    db.accept_part(_cap())
    out = []
    t = threading.Thread(target=lambda: out.append(len(_find(db))))
    t.start()
    t.join()
    assert out == [1]


def test_no_voltage_floor_finds_any_rating():
    db = _db()
    db.accept_part(_cap(v=6.3))
    assert not _find(db)                       # 16 V floor
    assert _find(db, rating_min=None)


# ----- schema version 2: ICs ----------------------------------------------

def _ic(mpn: str = "LDL212PV33R", *, manufacturer: str = "STMicroelectronics",
        package: str = "6-DFN (2x2)", case: str = "6-WDFN Exposed Pad",
        pins=6, dk="497-19548-1-ND", lcsc=None, lib=None) -> IcChoice:
    return IcChoice(
        mpn=mpn, manufacturer=manufacturer, package=package, package_case=case,
        pins=pins, digikey_pn=dk, digikey_packaging="cut tape" if dk else None,
        lcsc=lcsc, jlc_library=lib,
        description="IC REG LINEAR 3.3V 200MA 6DFN", datasheet_url="https://x/ldl.pdf",
    )


def test_schema_version_is_2():
    assert SCHEMA_VERSION == 2
    assert IC == "ic" and IcChoice.kind == "ic" and DbIc.kind == "ic"


def test_ic_accept_find_round_trip_without_lcsc():
    db = _db()
    pid = db.accept_part(_ic())
    hits = db.find_ics("LDL212PV33R")
    assert hits == [DbIc(
        part_id=pid, mpn="LDL212PV33R", manufacturer="STMicroelectronics",
        package="6-DFN (2x2)", package_case="6-WDFN Exposed Pad", pins=6,
        digikey_pn="497-19548-1-ND", lcsc=None, jlc_library=None, usage_count=0,
        description="IC REG LINEAR 3.3V 200MA 6DFN",
        datasheet_url="https://x/ldl.pdf",
    )]
    assert db._execute("SELECT type FROM part").fetchone()[0] == "ic"
    assert db._execute("SELECT package, pins FROM ic").fetchone() == (
        "6-WDFN Exposed Pad", 6)
    # No LCSC row for a part JLCPCB does not stock.
    assert [r[0] for r in db._execute("SELECT supplier FROM supplier_pn")] == [
        "digikey"]


def test_ic_with_lcsc():
    db = _db()
    db.accept_part(_ic("DRV8313RHHR", manufacturer="Texas Instruments",
                       package="36-QFN (5x6)", case="36-VFQFN Exposed Pad",
                       pins=36, dk="296-37496-1-ND", lcsc="C92482", lib=" Extended"))
    h = db.find_ics("DRV8313RHHR")[0]
    assert (h.lcsc, h.jlc_library, h.pins) == ("C92482", "extended", 36)


def test_find_ics_by_normalized_mpn():
    db = _db()
    pid = db.accept_part(_ic())
    for query in ("ldl212pv33r", " LDL212PV33R ", "LDL212 PV33R", "ldl212pv33r\t"):
        assert [h.part_id for h in db.find_ics(query)] == [pid], query
    # Exact after normalization: a prefix or a longer MPN is another part.
    assert db.find_ics("LDL212") == []
    assert db.find_ics("LDL212PV33R-TR") == []
    assert db.find_ics("") == [] and db.find_ics("  ") == []



def test_find_ics_normalizes_the_stored_mpn():
    # A stored MPN with inner whitespace (space, newline, NBSP) or lower-case
    # letters is found by the same key as the argument.
    for stored in ("LDL212 PV33R", "ldl212\npv33r", "LDL212\xa0PV33R"):
        db = _db()
        pid = db.accept_part(_ic(stored))
        for query in ("ldl212pv33r", stored):
            assert [h.part_id for h in db.find_ics(query)] == [pid], (stored, query)
    # Non-ASCII letters are upper-cased as norm_mpn does (MICRO SIGN ->
    # GREEK CAPITAL MU); SQLite's UPPER changes ASCII letters only.
    db = _db()
    pid = db.accept_part(_ic("\u00b5a741"))
    assert [h.part_id for h in db.find_ics("\u00b5A741")] == [pid]
    assert [h.part_id for h in db.find_ics("\u039cA741")] == [pid]


def test_norm_mpn():
    assert norm_mpn(" ldl212 pv33r ") == "LDL212PV33R"
    assert norm_mpn("LTC6992CS6-2#TRPBF") == "LTC6992CS6-2#TRPBF"
    assert norm_mpn(None) == ""


def test_find_ics_any_manufacturer_ordered():
    db = _db()
    a = db.accept_part(_ic(manufacturer="A", dk="d1"))
    b = db.accept_part(_ic(manufacturer="B", dk="d2", lcsc="C2", lib="extended"))
    c = db.accept_part(_ic(manufacturer="C", dk="d3", lcsc="C3", lib="basic"))
    # No usage: Basic, then Extended, then not at JLCPCB.
    assert [h.part_id for h in db.find_ics("LDL212PV33R")] == [c, b, a]
    db.record_usage(a, "BarrelJackAdapter/BarrelJackAdapter.kicad_sch", ["U2"])
    hits = db.find_ics("LDL212PV33R")
    assert [h.part_id for h in hits] == [a, c, b]
    assert hits[0].usage_count == 1


def test_ic_rejected_never_found_and_reaccept():
    db = _db()
    pid = db.reject_part(_ic())
    assert db.is_rejected("stmicroelectronics", "ldl212pv33r")
    assert db.find_ics("LDL212PV33R") == []
    assert db.accept_part(_ic()) == pid
    assert [h.part_id for h in db.find_ics("LDL212PV33R")] == [pid]


def test_ic_upsert_updates_and_keeps_unnamed_supplier_rows():
    db = _db()
    pid = db.accept_part(_ic(lcsc="C1", lib="basic"))
    assert db.accept_part(_ic(" ldl212pv33r ", pins=None, case="", lcsc=None)) == pid
    assert db._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1
    assert db._execute("SELECT COUNT(*) FROM ic").fetchone()[0] == 1
    h = db.find_ics("LDL212PV33R")[0]
    assert (h.pins, h.package_case) == (None, "")
    assert (h.digikey_pn, h.lcsc) == ("497-19548-1-ND", "C1")


def test_ics_and_capacitors_do_not_mix():
    db = _db()
    db.accept_part(_cap())
    db.accept_part(_ic())
    assert db.find_ics("CL10B104KB8NNNC") == []
    assert len(_find(db)) == 1
    with pytest.raises(ValueError):
        db.find_parts(kind=IC, nominal=1.0, package="", rating_min=None,
                      dielectrics=[], tolerance_max_pct=None)


def test_type_change_moves_parameter_row():
    db = _db()
    pid = db.accept_part(_cap("SAME", manufacturer="X", dk=None, lcsc=None))
    assert db.accept_part(_ic("SAME", manufacturer="X", dk=None)) == pid
    assert db._execute("SELECT COUNT(*) FROM capacitor").fetchone()[0] == 0
    assert db.find_ics("SAME")[0].part_id == pid
    assert db.accept_part(_cap("SAME", manufacturer="X", dk=None, lcsc=None)) == pid
    assert db._execute("SELECT COUNT(*) FROM ic").fetchone()[0] == 0
    assert db.find_ics("SAME") == []
    assert [h.part_id for h in _find(db)] == [pid]


def test_ic_empty_mpn_raises():
    db = _db()
    with pytest.raises(ValueError):
        db.accept_part(_ic(" "))
    assert db._execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0


def test_delete_ic_cascades():
    db = _db()
    pid = db.accept_part(_ic())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["U2"])
    with db._tx():
        db._execute("DELETE FROM part WHERE id = ?", (pid,))
    for table in ("ic", "supplier_pn", "usage"):
        assert db._execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0


# The version-1 schema exactly as shipped (no ic table).
_V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE part (
  id            INTEGER PRIMARY KEY,
  mpn           TEXT NOT NULL COLLATE NOCASE,
  manufacturer  TEXT NOT NULL COLLATE NOCASE,
  type          TEXT NOT NULL,
  package       TEXT NOT NULL,
  description   TEXT,
  datasheet_url TEXT,
  status        TEXT NOT NULL,
  decided_at    TEXT NOT NULL,
  notes         TEXT,
  UNIQUE (manufacturer, mpn)
);
CREATE TABLE capacitor (
  part_id       INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  capacitance_f REAL NOT NULL,
  voltage_v     REAL NOT NULL,
  dielectric    TEXT NOT NULL,
  tolerance_pct REAL
);
CREATE TABLE resistor (
  part_id        INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  resistance_ohm REAL NOT NULL,
  power_w        REAL,
  tolerance_pct  REAL
);
CREATE TABLE supplier_pn (
  part_id       INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  supplier      TEXT NOT NULL,
  pn            TEXT NOT NULL,
  packaging     TEXT,
  jlc_library   TEXT,
  verified_at   TEXT NOT NULL,
  PRIMARY KEY (supplier, pn)
);
CREATE TABLE usage (
  part_id       INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  board         TEXT NOT NULL,
  reference     TEXT NOT NULL,
  used_at       TEXT NOT NULL,
  PRIMARY KEY (part_id, board, reference)
);
INSERT INTO meta VALUES ('schema_version', '1');
INSERT INTO settings VALUES ('cap.package', '0603');
INSERT INTO part VALUES (1, 'CL10B104KB8NNNC', 'Samsung', 'capacitor', '0603',
  '100nF', 'https://x/ds.pdf', 'accepted', '2026-10-01T00:00:00+00:00', NULL);
INSERT INTO part VALUES (2, 'RC0603FR-0710KL', 'Yageo', 'resistor', '0603',
  '10k', '', 'accepted', '2026-10-01T00:00:00+00:00', NULL);
INSERT INTO part VALUES (3, 'BAD-CAP', 'Samsung', 'capacitor', '0603',
  '', '', 'rejected', '2026-10-01T00:00:00+00:00', 'cracked');
INSERT INTO capacitor VALUES (1, 1e-7, 50.0, 'X7R', 10.0);
INSERT INTO capacitor VALUES (3, 1e-7, 50.0, 'X7R', 10.0);
INSERT INTO resistor VALUES (2, 10000.0, 0.1, 1.0);
INSERT INTO supplier_pn VALUES (1, 'digikey', '1276-1000-1-ND', 'cut tape', NULL,
  '2026-10-01T00:00:00+00:00');
INSERT INTO supplier_pn VALUES (1, 'lcsc', 'C14663', NULL, 'basic',
  '2026-10-01T00:00:00+00:00');
INSERT INTO usage VALUES (1, 'ECG/ECG.kicad_sch', 'C1', '2026-10-01T00:00:00+00:00');
"""

_V1_TABLES = ("settings", "part", "capacitor", "resistor", "supplier_pn", "usage")


def _v1_file(path: Path) -> dict:
    """Write a version-1 database at *path*; return its rows by table."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_SCHEMA)
    conn.commit()
    rows = {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall())
            for t in _V1_TABLES}
    conn.close()
    return rows


def test_migrates_version_1_file_keeping_rows(tmp_path: Path):
    sqlite_only()
    path = tmp_path / "parts.sqlite"
    before = _v1_file(path)
    db = PartsDb(path, now=lambda: STAMP)
    assert db._execute(
        'SELECT value FROM meta WHERE "key" = \'schema_version\'').fetchone()[0] == "2"
    for table, rows in before.items():
        assert sorted(db._execute(f"SELECT * FROM {table}").fetchall()) == rows
    # Version-1 parts still found as before; the rejected one still excluded.
    hits = _find(db)
    assert [h.part_id for h in hits] == [1]
    assert hits[0].usage_count == 1
    assert db.is_rejected("Samsung", "BAD-CAP")
    assert db.get_setting("cap.package") == "0603"
    # The new table is there and usable.
    pid = db.accept_part(_ic())
    assert db.find_ics("LDL212PV33R")[0].part_id == pid
    db.close()
    # Reopening a migrated file is a plain version-2 open.
    db = PartsDb(path)
    assert db.find_ics("LDL212PV33R")[0].part_id == pid
    assert [h.part_id for h in _find(db)] == [1]
    db.close()


def test_migration_failure_leaves_version_1_file(tmp_path: Path, monkeypatch):
    sqlite_only()
    import kicadtools.partsdb as partsdb

    path = tmp_path / "parts.sqlite"
    before = _v1_file(path)
    # A statement that fails after the ic table has been created.
    monkeypatch.setattr(partsdb, "_SCHEMA",
                        partsdb._SCHEMA + "\nCREATE TABLE ic (x);")
    with pytest.raises(sqlite3.OperationalError):
        PartsDb(path)
    conn = sqlite3.connect(str(path))
    assert conn.execute(
        'SELECT value FROM meta WHERE "key" = \'schema_version\'').fetchone()[0] == "1"
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "ic" not in tables
    for table, rows in before.items():
        assert sorted(conn.execute(f"SELECT * FROM {table}").fetchall()) == rows
    conn.close()
