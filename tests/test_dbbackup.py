"""Tests for the backups and the offline copy (dbbackup.py, DB_PLAN.md
step 7), from SQLite and, when configured, MariaDB (tests/dbbackends.py)."""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from kicadtools.dbbackup import backup_name, backup_now, prune, pull
from kicadtools.dbconfig import newest_backup
from kicadtools.partsdb import IcChoice, PartChoice, PartParams, PartsDb
from tests.dbbackends import BACKENDS, new_db, use_backend

STAMP = "2026-11-23T12:00:00+00:00"


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


def _filled():
    db = new_db(now=lambda: STAMP)
    cap = db.accept_part(PartChoice(
        kind="capacitor", mpn="CL10B104KB8NNNC", manufacturer="Samsung", package="0603",
        params=PartParams(100e-9, 50.0, "X7R", 10.0), digikey_pn="1276-1000-1-ND",
        lcsc="C14663", jlc_library="basic", description="100nF µ ± Würth"))
    db.accept_part(IcChoice(mpn="L7805CV", manufacturer="ST", package="TO-220",
                            package_case="TO-220-3", pins=3, digikey_pn="497-1443-5-ND"))
    db.record_usage(cap, "LabBoard/LabBoard.kicad_sch", ["C1", "C2"])
    db.set_setting("cap.voltage_min", "16V")
    db.set_rotation("C14663", 90)
    return db, cap


def test_pull_copies_every_table_into_a_readable_file(tmp_path):
    db, cap = _filled()
    path = tmp_path / "copy.sqlite"
    counts = pull(db, path)
    assert counts["part"] == 2 and counts["usage"] == 2 and counts["part_rotation"] == 1
    assert not (tmp_path / "copy.sqlite.partial").exists()
    copy = PartsDb.open_read_only(path)
    hits = copy.find_parts(kind="capacitor", nominal=100e-9, package="0603", rating_min=None,
                           dielectrics=(), tolerance_max_pct=None)
    assert [(h.part_id, h.usage_count, h.description) for h in hits] == [(cap, 2, "100nF µ ± Würth")]
    assert copy.find_ics("L7805CV")[0].pins == 3
    assert copy.get_setting("cap.voltage_min") == "16V"
    assert copy.rotations() == {"C14663": 90.0}
    copy.close()


def test_pull_replaces_an_older_file(tmp_path):
    db, _ = _filled()
    path = tmp_path / "copy.sqlite"
    pull(db, path)
    db.set_setting("cap.voltage_min", "25V")
    pull(db, path)
    copy = PartsDb.open_read_only(path)
    assert copy.get_setting("cap.voltage_min") == "25V"
    copy.close()


def test_backup_now_names_keeps_ten_and_is_the_offline_copy(tmp_path):
    db, _ = _filled()
    for i in range(12):
        path = backup_now(db, tmp_path, when=datetime(2026, 11, 23, 14, 0, i))
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
        prune(tmp_path)
    files = sorted(f.name for f in tmp_path.glob("parts-*.sqlite"))
    assert len(files) == 10
    assert files[0] == "parts-2026-11-23-140002.sqlite"
    assert newest_backup(tmp_path).name == "parts-2026-11-23-140011.sqlite"


def test_backup_name():
    assert backup_name(datetime(2026, 11, 23, 14, 2, 15)) == "parts-2026-11-23-140215.sqlite"
