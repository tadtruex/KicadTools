"""Where the parts database is, and its password (``DB_PLAN.md`` step 3).

The configuration is ``~/.kicadtools/config.toml`` (``KICADTOOLS_CONFIG``
names another file)::

    [parts_db]
    backend = "mariadb"            # or "sqlite", today's behaviour
    database = "tadtruex_inventory"
    user = "tadtruex_inventory"

    [parts_db.ssh]
    host = "tadtruex.host.dartmouth.edu"
    user = "tadtruex"
    remote_port = 3306             # MariaDB's port on the server (default)
    # local_port = 3307            # a tunnel the user runs; omit to let the
    #                              # Cockpit open one on a free port

:func:`load_config` reads it. ``KICADTOOLS_PARTS_DB`` (a SQLite path) wins
over the file, so tests and one-off runs need no configuration; with no
file, the backend is the SQLite file of :func:`kicadtools.partsdb.default_path`.

The password is ``~/.kicadtools/db_password``: one line, mode ``0600``, in
no repository (``KICADTOOLS_DB_PASS`` overrides it). :func:`obtain_password`
reads it, checks it with a login, and asks for it (and writes the file)
when the file is missing or the server rejects what it holds. Nothing here
prints or logs a password.

No network I/O of its own: the login check is passed in.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from kicadtools import partsdb

SQLITE = "sqlite"
MARIADB = "mariadb"
BACKENDS = (SQLITE, MARIADB)

CONFIG_ENV = "KICADTOOLS_CONFIG"
PASSWORD_ENV = "KICADTOOLS_DB_PASS"
DEFAULT_REMOTE_PORT = 3306

# MariaDB's "access denied" error codes: a bad password (1045) or no
# privileges on the database (1044). Any other error (the tunnel is down,
# the host is unreachable) is not a reason to ask for the password again.
AUTH_ERRORS = frozenset({1044, 1045})


def kicadtools_dir() -> Path:
    return Path.home() / ".kicadtools"


def config_path(env: Mapping[str, str] = os.environ) -> Path:
    """``$KICADTOOLS_CONFIG``, else ``~/.kicadtools/config.toml``."""
    override = env.get(CONFIG_ENV)
    return Path(override).expanduser() if override else kicadtools_dir() / "config.toml"


def password_path() -> Path:
    return kicadtools_dir() / "db_password"


class ConfigError(ValueError):
    """The configuration file is unreadable or incomplete; the message names
    the file and the problem."""


@dataclass(frozen=True)
class DbConfig:
    """Where the parts database is. For :data:`SQLITE` only ``sqlite_path``
    is used; for :data:`MARIADB` the database, its user and the SSH host the
    tunnel runs through."""

    backend: str = SQLITE
    sqlite_path: Optional[Path] = None
    database: str = ""
    user: str = ""
    ssh_host: str = ""
    ssh_user: str = ""
    remote_port: int = DEFAULT_REMOTE_PORT
    local_port: Optional[int] = None    # a tunnel the user runs
    source: str = ""                    # where this came from, for messages

    @property
    def label(self) -> str:
        """``"tadtruex_inventory (server)"`` or the SQLite path."""
        if self.backend == MARIADB:
            return f"{self.database} (server)"
        return str(self.sqlite_path)


def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise ConfigError(
                f"{path}: reading it needs Python 3.11 or the tomli package") from None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _text(table: Mapping[str, object], key: str, path: Path, section: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: [{section}] needs {key} = \"…\"")
    return value.strip()


def _port(table: Mapping[str, object], key: str, path: Path, section: str,
          default: Optional[int]) -> Optional[int]:
    value = table.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise ConfigError(f"{path}: [{section}] {key} must be a port number")
    return value


def load_config(env: Mapping[str, str] = os.environ) -> DbConfig:
    """The database configuration: ``KICADTOOLS_PARTS_DB`` when set (a SQLite
    file); else the configuration file when it exists; else the default
    SQLite file. Raises :class:`ConfigError` for a file that does not parse,
    names an unknown backend, or lacks a field its backend needs."""
    if env.get(partsdb.ENV_VAR):
        return DbConfig(sqlite_path=Path(env[partsdb.ENV_VAR]).expanduser(),
                        source=partsdb.ENV_VAR)
    path = config_path(env)
    if not path.exists():
        return DbConfig(sqlite_path=partsdb.default_path(), source="default")
    data = _load_toml(path)
    db = data.get("parts_db", {})
    if not isinstance(db, dict):
        raise ConfigError(f"{path}: [parts_db] must be a table")
    backend = str(db.get("backend", SQLITE)).strip().lower()
    if backend not in BACKENDS:
        raise ConfigError(f"{path}: [parts_db] backend {backend!r}; "
                          f"use one of {', '.join(BACKENDS)}")
    if backend == SQLITE:
        raw = db.get("path")
        sqlite_path = Path(str(raw)).expanduser() if raw else partsdb.default_path()
        return DbConfig(sqlite_path=sqlite_path, source=str(path))
    ssh = db.get("ssh", {})
    if not isinstance(ssh, dict):
        raise ConfigError(f"{path}: [parts_db.ssh] must be a table")
    return DbConfig(
        backend=MARIADB,
        database=_text(db, "database", path, "parts_db"),
        user=_text(db, "user", path, "parts_db"),
        ssh_host=_text(ssh, "host", path, "parts_db.ssh"),
        ssh_user=_text(ssh, "user", path, "parts_db.ssh"),
        remote_port=_port(ssh, "remote_port", path, "parts_db.ssh", DEFAULT_REMOTE_PORT),
        local_port=_port(ssh, "local_port", path, "parts_db.ssh", None),
        source=str(path),
    )


def backups_dir() -> Path:
    """Where the backups of the server's data are written (``DB_PLAN.md``
    step 7); the newest is the offline copy."""
    return kicadtools_dir() / "backups"


def newest_backup(folder: Optional[Path] = None) -> Optional[Path]:
    """The newest ``parts-*.sqlite`` backup (by modification time), else
    ``None``."""
    folder = folder or backups_dir()
    try:
        files = [f for f in folder.glob("parts-*.sqlite") if f.is_file()]
    except OSError:
        return None
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


# --- password -------------------------------------------------------------------


def read_password(env: Mapping[str, str] = os.environ,
                  path: Optional[Path] = None) -> Optional[str]:
    """``$KICADTOOLS_DB_PASS``, else the password file's first line, else
    ``None``."""
    if env.get(PASSWORD_ENV):
        return env[PASSWORD_ENV]
    path = path or password_path()
    try:
        line = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    return line[0] if line and line[0] else None


def write_password(password: str, path: Optional[Path] = None) -> Path:
    """Write *password* to the password file, readable by its owner only
    (``0600``, set before the password is written). Returns the path."""
    path = path or password_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (password + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return path


def is_auth_error(exc: BaseException) -> bool:
    """Whether *exc* is the server refusing the login (a MariaDB error whose
    code is in :data:`AUTH_ERRORS`), as opposed to failing to reach it."""
    args = getattr(exc, "args", ())
    return bool(args) and isinstance(args[0], int) and args[0] in AUTH_ERRORS


class PasswordCancelled(Exception):
    """The user closed the password prompt."""


def obtain_password(check: Callable[[str], None],
                    ask: Callable[[str], Optional[str]], *,
                    env: Mapping[str, str] = os.environ,
                    path: Optional[Path] = None) -> str:
    """A password the server accepts.

    The stored one (:func:`read_password`) is tried first. When there is
    none, or *check* raises an error :func:`is_auth_error` reads as a
    refused login, *ask* is called with a one-line reason and its answer is
    checked; an accepted answer is written to the password file (unless it
    came from ``KICADTOOLS_DB_PASS``, which the file does not replace) and
    returned. *check(password)* logs in and returns, or raises; an error
    that is not a refused login (the server cannot be reached) is raised to
    the caller, without asking. *ask* returns ``None`` when the user
    cancels, which raises :class:`PasswordCancelled`."""
    path = path or password_path()
    stored = read_password(env, path)
    from_env = bool(env.get(PASSWORD_ENV))
    if stored is not None:
        try:
            check(stored)
            return stored
        except Exception as exc:  # noqa: BLE001 - sorted by is_auth_error
            if not is_auth_error(exc) or from_env:
                raise
            reason = "The server refused the stored password. Enter the database password:"
    else:
        reason = "Enter the parts database password (saved to ~/.kicadtools/db_password):"
    while True:
        answer = ask(reason)
        if answer is None:
            raise PasswordCancelled()
        try:
            check(answer)
        except Exception as exc:  # noqa: BLE001
            if not is_auth_error(exc):
                raise
            reason = "The server refused that password. Try again:"
            continue
        write_password(answer, path)
        return answer


def terminal_ask(reason: str) -> Optional[str]:
    """An *ask* for :func:`obtain_password` in a terminal: prints *reason*
    and reads the password without echoing it; ``None`` on an empty answer
    or Ctrl-D."""
    import getpass

    try:
        answer = getpass.getpass(reason + " ")
    except EOFError:
        return None
    return answer or None
