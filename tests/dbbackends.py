"""Parts-database engines for the tests (``DB_PLAN.md`` step 2).

SQLite (an in-memory database) always. MariaDB too when
``KICADTOOLS_TEST_DB_NAME`` names a database whose name ends ``_test``; the
tables in it are dropped before each database the tests open, so it must
never be the real one. The connection reads:

- ``KICADTOOLS_TEST_DB_HOST`` (default ``127.0.0.1``, the SSH tunnel's end)
  and ``KICADTOOLS_TEST_DB_PORT`` (default ``3307``);
- ``KICADTOOLS_TEST_DB_USER``;
- the password from ``KICADTOOLS_DB_PASS``, else ``~/.kicadtools/db_password``.

A test module uses it as::

    @pytest.fixture(autouse=True, params=BACKENDS)
    def backend(request):
        yield from use_backend(request.param)

and opens each database with :func:`new_db`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

import pytest

from kicadtools.partsdb import PartsDb

SQLITE = "sqlite"
MARIADB = "mariadb"


def _password() -> str:
    env = os.environ.get("KICADTOOLS_DB_PASS")
    if env:
        return env
    path = Path.home() / ".kicadtools" / "db_password"
    return path.read_text().strip() if path.exists() else ""


def mariadb_params() -> Optional[Dict[str, object]]:
    """The test server's connection arguments, ``None`` when not configured.
    Raises ``RuntimeError`` for a database name that does not end ``_test``."""
    name = os.environ.get("KICADTOOLS_TEST_DB_NAME", "").strip()
    if not name:
        return None
    if not name.endswith("_test"):
        raise RuntimeError(
            f"KICADTOOLS_TEST_DB_NAME={name!r}: the tests drop every table in it; "
            "use a database whose name ends _test")
    try:
        import pymysql  # noqa: F401
    except ImportError:
        return None
    return dict(host=os.environ.get("KICADTOOLS_TEST_DB_HOST", "127.0.0.1"),
                port=int(os.environ.get("KICADTOOLS_TEST_DB_PORT", "3307")),
                user=os.environ.get("KICADTOOLS_TEST_DB_USER", ""),
                password=_password(), database=name)


BACKENDS: List[str] = [SQLITE] + ([MARIADB] if mariadb_params() else [])

_current = {"backend": SQLITE}
_opened: List[PartsDb] = []


def _close_opened() -> None:
    while _opened:
        try:
            _opened.pop().close()
        except Exception:  # noqa: BLE001 - already closed
            pass


def wipe() -> None:
    """Drop every table in the test database."""
    import pymysql

    params = mariadb_params()
    conn = pymysql.connect(**params, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = DATABASE()")
            for (table,) in cur.fetchall():
                cur.execute(f"DROP TABLE `{table}`")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
    finally:
        conn.close()


def use_backend(name: str) -> Iterator[str]:
    """Select *name* for :func:`new_db` for one test; close what it opened."""
    if name == MARIADB:
        _close_opened()
        wipe()
    _current["backend"] = name
    try:
        yield name
    finally:
        _close_opened()
        _current["backend"] = SQLITE


def current() -> str:
    return _current["backend"]


def new_db(now: Optional[Callable[[], str]] = None) -> PartsDb:
    """An empty parts database on the current engine. On MariaDB the tables
    are dropped first, so every call starts empty, as ``":memory:"`` does."""
    if current() == SQLITE:
        return PartsDb(":memory:", now=now)
    _close_opened()
    wipe()
    db = PartsDb.connect_mariadb(**mariadb_params(), now=now)
    _opened.append(db)
    return db


def sqlite_only() -> None:
    """Skip the running test on MariaDB (SQLite files, PRAGMAs)."""
    if current() != SQLITE:
        pytest.skip("SQLite only")


def integrity_errors() -> tuple:
    """The exception types a foreign-key or unique-key violation raises on
    either engine."""
    import sqlite3

    errors = [sqlite3.IntegrityError]
    try:
        import pymysql

        errors.append(pymysql.err.IntegrityError)
    except ImportError:
        pass
    return tuple(errors)
