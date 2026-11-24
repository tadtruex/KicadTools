"""Open the configured parts database (``DB_PLAN.md``): the SQLite file, or
the MariaDB server through the SSH tunnel with the stored password.

:func:`open_parts_db` is used by ``kicadtools db`` and the Cockpit. For the
server it opens the tunnel (:mod:`kicadtools.dbtunnel`), gets a password the
server accepts (:func:`kicadtools.dbconfig.obtain_password`, asking through
*ask* when needed), and connects. The returned :class:`DbSession` holds the
database and the tunnel; :meth:`DbSession.close` closes both.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from kicadtools.dbconfig import MARIADB, DbConfig, obtain_password
from kicadtools.dbtunnel import Tunnel
from kicadtools.partsdb import PartsDb


@dataclass
class DbSession:
    """An open parts database and, for the server, its tunnel and the local
    port it uses (worker threads open their own connections through it)."""

    db: PartsDb
    config: DbConfig
    tunnel: Optional[Tunnel] = None
    password: Optional[str] = None
    read_only: bool = False

    @property
    def port(self) -> Optional[int]:
        return self.tunnel.port if self.tunnel is not None else None

    def connect_again(self) -> PartsDb:
        """Another connection to the same database (a worker thread's)."""
        if self.config.backend != MARIADB:
            if self.read_only:
                return PartsDb.open_read_only(self.config.sqlite_path)
            return PartsDb(self.config.sqlite_path)
        return PartsDb.connect_mariadb(host="127.0.0.1", port=self.port,
                                       user=self.config.user, password=self.password,
                                       database=self.config.database,
                                       read_only=self.read_only)

    def close(self) -> None:
        try:
            self.db.close()
        finally:
            if self.tunnel is not None:
                self.tunnel.close()


def open_parts_db(config: DbConfig, ask: Callable[[str], Optional[str]], *,
                  askpass: Optional[Path] = None,
                  read_only: bool = False) -> DbSession:
    """Open the database *config* names. For the server: tunnel, password
    (asked through *ask* when there is none or it is refused), connection.
    Raises :class:`~kicadtools.dbtunnel.TunnelError` when the tunnel does not
    start, :class:`~kicadtools.dbconfig.PasswordCancelled` when the user
    cancels the prompt, and PyMySQL's errors when the server cannot be
    reached; the tunnel is closed on any of them.

    With *read_only* nothing is written: the schema statements are not run
    (only the version is checked) and a write fails (SQLite opens the file
    read-only; the server refuses writes in the session). For reading a
    database with code of another version, such as ``develop`` beside a
    release."""
    if config.backend != MARIADB:
        db = (PartsDb.open_read_only(config.sqlite_path) if read_only
              else PartsDb(config.sqlite_path))
        return DbSession(db, config, read_only=read_only)
    tunnel = Tunnel(config.ssh_host, config.ssh_user, config.remote_port,
                    local_port=config.local_port, askpass=askpass)
    port = tunnel.open()
    try:
        params = dict(host="127.0.0.1", port=port, user=config.user,
                      database=config.database)
        password = obtain_password(
            lambda pw: PartsDb.check_mariadb_login(password=pw, **params), ask)
        db = PartsDb.connect_mariadb(password=password, read_only=read_only, **params)
    except BaseException:
        tunnel.close()
        raise
    return DbSession(db, config, tunnel, password, read_only)
