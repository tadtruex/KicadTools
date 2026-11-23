"""Tests for the Cockpit's parts-database states (DB_PLAN.md step 6):
online on the server, offline with a read-only copy (the red bar, [OFFLINE]
in the title, writes refused) and Retry. The server is replaced by a stand-in
for open_parts_db; HOME is a temporary folder."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from kicadtools.dbconfig import DbConfig, MARIADB  # noqa: E402
from kicadtools.dbconnect import DbSession  # noqa: E402
from kicadtools.dbtunnel import TunnelError  # noqa: E402
from kicadtools.partsdb import PartChoice, PartParams, PartsDb  # noqa: E402

SERVER = """
[parts_db]
backend = "mariadb"
database = "tadtruex_inventory"
user = "tadtruex_inventory"

[parts_db.ssh]
host = "db.example.edu"
user = "tadtruex"
"""


class _Tunnel:
    port = 3307

    def close(self):
        pass


@pytest.fixture
def cockpit(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KICADTOOLS_PARTS_DB", raising=False)
    cfg = tmp_path / ".kicadtools" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(SERVER)
    monkeypatch.setenv("KICADTOOLS_CONFIG", str(cfg))
    import kicadtools.gui.main_window as mw

    monkeypatch.setattr(mw, "_LCSC_CACHE", tmp_path / ".kicadtools" / "lcsc_cache.sqlite")
    monkeypatch.setattr(mw, "askpass_script", lambda: tmp_path / "askpass")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    server = {"db": None, "error": TunnelError("ssh: connect to host db.example.edu: "
                                               "Network is unreachable")}

    def fake_open(config, ask, askpass=None):
        if server["error"] is not None:
            raise server["error"]
        return DbSession(server["db"], config, _Tunnel(), "pw")

    monkeypatch.setattr(mw, "open_parts_db", fake_open)
    app = QApplication.instance() or QApplication([])

    def make():
        return mw.MainWindow()

    make.server, make.warnings, make.tmp = server, warnings, tmp_path
    yield make
    app.processEvents()


def _backup(tmp_path):
    folder = tmp_path / ".kicadtools" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "parts-2026-11-23.sqlite"
    db = PartsDb(path)
    db.accept_part(PartChoice(kind="capacitor", mpn="CL10B104KB8NNNC", manufacturer="Samsung",
                              package="0603", params=PartParams(100e-9, 50.0, "X7R", 10.0),
                              lcsc="C14663"))
    db.set_rotation("C14663", 90)
    db.close()
    return path


def test_offline_with_a_copy_is_unmissable_and_read_only(cockpit):
    copy = _backup(cockpit.tmp)
    win = cockpit()
    try:
        assert win._offline.startswith("ssh: connect to host")
        assert not win._offline_bar.isHidden()
        text = win._offline_text.text()
        assert text.startswith("OFFLINE — read-only copy of the parts database from ")
        assert "Accept, Reject and Autopopulate are off." in text and "Network is unreachable" in text
        assert win.windowTitle() == "BOM Cockpit [OFFLINE][*]"
        assert win._db_label.text() == f"parts DB: OFFLINE copy {copy.name}"
        assert not win.autopopulate_btn.isEnabled()
        assert win._parts_db.read_only
        # Stored parts are found in the copy; rotations read from it.
        assert win._parts_db.find_ics("x") == []
        assert win._rotation_store.get("C14663") == 90.0
        # Writes are refused with a message.
        assert win._refuse_offline("accept") and "accept was not recorded" in cockpit.warnings[-1]
        win._rotation_store.set("C14663", 180)
        assert win._rotation_store.get("C14663") == 90.0
        assert "rotation was not saved" in win.statusBar().currentMessage()
        # A worker opens the copy read-only too.
        opener = win._worker_db_kwargs()["open_db"]
        assert opener().read_only
    finally:
        win.close()


def test_offline_with_no_copy_says_so(cockpit):
    win = cockpit()
    try:
        assert win._parts_db is None
        assert win._offline_text.text().startswith(
            "OFFLINE — no local copy of the parts database: stored parts cannot be searched.")
        assert win._db_label.text() == "parts DB: OFFLINE, none"
        assert win._worker_db_kwargs()["open_db"]().label == ":memory:"
    finally:
        win.close()


def test_retry_connects_and_clears_the_offline_state(cockpit):
    _backup(cockpit.tmp)
    win = cockpit()
    try:
        assert win._offline
        cockpit.server.update(db=PartsDb(":memory:"), error=None)
        win._retry_database()
        assert win._offline == "" and win._offline_bar.isHidden()
        assert win.windowTitle() == "BOM Cockpit[*]"
        assert win._db_label.text() == "parts DB: tadtruex_inventory (server)"
        assert win.autopopulate_btn.isEnabled()
        assert not win._refuse_offline("accept")
        # Rotations are the server's now, and writable.
        win._rotation_store.set("C1", 270)
        assert cockpit.server["db"].rotations() == {"C1": 270.0}
        assert win.statusBar().currentMessage() == "Connected to tadtruex_inventory (server)."
    finally:
        win.close()


def test_server_online_at_start(cockpit):
    cockpit.server.update(db=PartsDb(":memory:"), error=None)
    win = cockpit()
    try:
        assert win._offline == "" and win._offline_bar.isHidden()
        assert win._parts_db is cockpit.server["db"]
        assert "open_db" in win._worker_db_kwargs()
        # The start wrote the backup that is the next offline copy.
        backups = list((cockpit.tmp / ".kicadtools" / "backups").glob("parts-*.sqlite"))
        assert len(backups) == 1
        assert win.statusBar().currentMessage().startswith(
            f"Backed up the parts database to {backups[0].name}.")
    finally:
        win.close()


def test_sqlite_configuration_keeps_todays_behaviour(cockpit, monkeypatch, tmp_path):
    monkeypatch.setenv("KICADTOOLS_PARTS_DB", str(tmp_path / "parts.sqlite"))
    win = cockpit()
    try:
        assert win._offline == "" and win._parts_db.backend == "sqlite"
        assert win._worker_db_kwargs() == {}
        assert win._db_label.text() == f"parts DB: {tmp_path / 'parts.sqlite'}"
        assert not win._rotations_on_db()
    finally:
        win.close()
