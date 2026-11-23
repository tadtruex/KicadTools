"""Tests for the parts-database configuration and password file
(dbconfig.py, DB_PLAN.md step 3)."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from kicadtools import dbconfig
from kicadtools.dbconfig import (
    MARIADB,
    SQLITE,
    ConfigError,
    PasswordCancelled,
    load_config,
    obtain_password,
    read_password,
    write_password,
)

SERVER = """
[parts_db]
backend = "mariadb"
database = "tadtruex_inventory"
user = "tadtruex_inventory"

[parts_db.ssh]
host = "tadtruex.host.dartmouth.edu"
user = "tadtruex"
"""


def _env(tmp_path: Path, text: str = None, **extra) -> dict:
    cfg = tmp_path / "config.toml"
    if text is not None:
        cfg.write_text(text)
    return {"KICADTOOLS_CONFIG": str(cfg), **extra}


def test_server_config_is_read(tmp_path):
    cfg = load_config(_env(tmp_path, SERVER))
    assert cfg.backend == MARIADB
    assert (cfg.database, cfg.user) == ("tadtruex_inventory", "tadtruex_inventory")
    assert (cfg.ssh_host, cfg.ssh_user) == ("tadtruex.host.dartmouth.edu", "tadtruex")
    assert (cfg.remote_port, cfg.local_port) == (3306, None)
    assert cfg.label == "tadtruex_inventory (server)"


def test_no_file_is_the_default_sqlite_file(tmp_path):
    cfg = load_config(_env(tmp_path))
    assert cfg.backend == SQLITE and cfg.sqlite_path.name == "parts.sqlite"


def test_parts_db_variable_wins_over_the_file(tmp_path):
    cfg = load_config(_env(tmp_path, SERVER, KICADTOOLS_PARTS_DB=str(tmp_path / "x.sqlite")))
    assert cfg.backend == SQLITE and cfg.sqlite_path == tmp_path / "x.sqlite"


def test_local_port_for_a_user_tunnel(tmp_path):
    cfg = load_config(_env(tmp_path, SERVER + "local_port = 3307\n"))
    assert cfg.local_port == 3307


@pytest.mark.parametrize("text, message", [
    (SERVER.replace('database = "tadtruex_inventory"\n', ""), "needs database"),
    (SERVER.replace('host = "tadtruex.host.dartmouth.edu"\n', ""), "[parts_db.ssh] needs host"),
    (SERVER.replace('"mariadb"', '"postgres"'), "backend 'postgres'"),
    (SERVER + "remote_port = 99999\n", "port number"),
    ("[parts_db\n", "config.toml"),
])
def test_bad_config_names_the_problem(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_config(_env(tmp_path, text))


# --- password -------------------------------------------------------------------


def test_password_file_is_owner_only(tmp_path):
    path = write_password("s3cr$t(", tmp_path / "db_password")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_password({}, path) == "s3cr$t("


def test_environment_overrides_the_file(tmp_path):
    path = write_password("file", tmp_path / "db_password")
    assert read_password({"KICADTOOLS_DB_PASS": "env"}, path) == "env"


class _Refused(Exception):
    def __init__(self):
        super().__init__(1045, "Access denied")


class _Server:
    def __init__(self, good):
        self.good = good
        self.tried = []

    def check(self, password):
        self.tried.append(password)
        if password != self.good:
            raise _Refused()


def test_stored_password_is_used_without_asking(tmp_path):
    path = write_password("good", tmp_path / "db_password")
    server = _Server("good")
    asked = []
    assert obtain_password(server.check, asked.append, env={}, path=path) == "good"
    assert asked == []


def test_missing_file_asks_and_saves(tmp_path):
    path = tmp_path / "db_password"
    server = _Server("good")
    answers = iter(["wrong", "good"])
    reasons = []

    def ask(reason):
        reasons.append(reason)
        return next(answers)

    assert obtain_password(server.check, ask, env={}, path=path) == "good"
    assert server.tried == ["wrong", "good"]
    assert "saved to" in reasons[0] and "refused" in reasons[1]
    assert read_password({}, path) == "good"


def test_refused_stored_password_asks_and_rewrites(tmp_path):
    path = write_password("old", tmp_path / "db_password")
    server = _Server("new")
    assert obtain_password(server.check, lambda reason: "new", env={}, path=path) == "new"
    assert read_password({}, path) == "new"


def test_cancel_raises(tmp_path):
    with pytest.raises(PasswordCancelled):
        obtain_password(_Server("good").check, lambda reason: None, env={},
                        path=tmp_path / "db_password")


def test_unreachable_server_is_not_a_password_problem(tmp_path):
    path = write_password("good", tmp_path / "db_password")

    def down(password):
        raise OSError("tunnel down")

    asked = []
    with pytest.raises(OSError):
        obtain_password(down, asked.append, env={}, path=path)
    assert asked == []


def test_refused_environment_password_is_not_replaced(tmp_path):
    path = tmp_path / "db_password"
    with pytest.raises(_Refused):
        obtain_password(_Server("good").check, lambda reason: "good",
                        env={"KICADTOOLS_DB_PASS": "bad"}, path=path)
    assert not path.exists()


def test_auth_error_codes():
    assert dbconfig.is_auth_error(_Refused())
    assert not dbconfig.is_auth_error(OSError(61, "refused"))
