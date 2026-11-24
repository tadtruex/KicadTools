"""Database of parts accepted (or rejected) for use on the boards: a SQLite
file, or the MariaDB server of ``DB_PLAN.md`` (:meth:`PartsDb.connect_mariadb`;
the SQL that differs between the two is in :class:`_Dialect`).

A part accepted in the Cockpit's assign dialog is recorded here with its
parameters and supplier part numbers, so a later search for the same
requirement finds it before any supplier query. A rejected part is recorded
too, with status ``rejected``, and is excluded from every candidate list.

The default file is ``parts.sqlite`` at the root of the Boards checkout
KicadTools runs from (:func:`kicadtools.repo.repo_root`, wherever it is),
overridable with ``KICADTOOLS_PARTS_DB``. The file is listed in
``Boards/.gitignore`` and is machine-local.

Tables (see ``CAPACITOR_PLAN.md`` and ``IC_PLAN.md`` step 7):

- ``meta`` — ``schema_version``, stamped at creation and checked on open;
- ``settings`` — last-used defaults (``cap.package``, ``cap.voltage_min``,
  ``res.tolerance`` …);
- ``part`` — one row per (manufacturer, MPN), with its type and decision
  status;
- ``capacitor`` — capacitance, voltage, dielectric and tolerance of a part;
- ``resistor`` — resistance, power and tolerance of a part;
- ``inductor`` — inductance, saturation current, shielding and tolerance of
  a part;
- ``ic`` — DigiKey's package/case and pin count of an IC (``part.type`` =
  ``"ic"``; modules are ICs here);
- ``tht`` — a through-hole part (``part.type`` = ``"tht"``; ``THT_PLAN.md``
  step 8): the footprint signature it was accepted for, the Value key when
  the family needs one, the footprint, the family and the positions;
- ``led`` — a one-colour LED (``part.type`` = ``"led"``; ``LED_PLAN.md``
  step 4): its colour, dominant and peak wavelength, intensity, forward
  voltage and lens. Multicolour and smart LEDs are found by part number and
  stored as ICs;
- ``supplier_pn`` — DigiKey and LCSC part numbers of a part. An IC first
  accepted while JLCPCB does not stock it has no LCSC row; an LCSC row
  recorded earlier is kept when the IC is accepted again without one (the
  number still names the part, and each search checks JLCPCB's stock again).
- ``usage`` — which board references place a part.

Schema version 2 added the ``ic`` table. A version-1 file is migrated in place
when it is opened: the table is added and ``meta.schema_version`` is set to 2
in one transaction, and every existing row is kept. A file of any other
version is refused without being changed. The ``inductor`` table was added
within version 2: the schema text is run on every open and holds only
``CREATE TABLE IF NOT EXISTS``, so a version-2 file without the table gains
it and keeps every row. The ``tht`` and ``led`` tables were added the same
way.

Stock and price are not stored; they change by the hour and are fetched from
the suppliers (through the existing JLCPCB cache) when candidates are listed.

Manufacturer and MPN are stored with surrounding whitespace removed and are
compared case-insensitively (``COLLATE NOCASE``), so ``"Samsung"`` and
``"SAMSUNG"`` name the same part. Every thread uses the one connection (the
Cockpit searches on a worker thread); a lock serializes access to it.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Callable, ClassVar, Dict, Iterator, List, Optional, Sequence, Union

from kicadtools import repo
from kicadtools.parttypes import CAPACITOR, INDUCTOR, RESISTOR, part_type

SCHEMA_VERSION = 2

# The one older version :class:`PartsDb` migrates on open.
_MIGRATABLE_VERSION = 1

# ``part.type`` of an IC. ICs are not a :class:`~kicadtools.parttypes.PartType`
# (they are found by part number, not by value), so the name is defined here.
IC = "ic"

# ``part.type`` of a through-hole part found by footprint (headers, sockets,
# terminal blocks, part-named footprints; ``THT_PLAN.md`` step 8).
THT = "tht"

# ``part.type`` of a one-colour LED (``LED_PLAN.md`` step 4).
LED = "led"

# Environment variable naming an alternative database file.
ENV_VAR = "KICADTOOLS_PARTS_DB"

# Relative half-width of the band within which a stored value equals a
# requested one (0.1 %), so float rounding of "100nF" never misses a match.
VALUE_REL_TOL = 1e-3

ACCEPTED = "accepted"
REJECTED = "rejected"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS part (
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
CREATE TABLE IF NOT EXISTS capacitor (
  part_id       INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  capacitance_f REAL NOT NULL,
  voltage_v     REAL NOT NULL,
  dielectric    TEXT NOT NULL,
  tolerance_pct REAL
);
CREATE TABLE IF NOT EXISTS resistor (
  part_id        INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  resistance_ohm REAL NOT NULL,
  power_w        REAL,
  tolerance_pct  REAL
);
-- current_a is the saturation current, or the rated current where the
-- supplier lists none. shielding is "Shielded", "Semi-Shielded" or
-- "Unshielded".
CREATE TABLE IF NOT EXISTS inductor (
  part_id        INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  inductance_h   REAL NOT NULL,
  current_a      REAL,
  shielding      TEXT NOT NULL,
  tolerance_pct  REAL
);
-- ic.package is DigiKey's Package / Case (IcChoice.package_case). The
-- Supplier Device Package is part.package, as for every part type.
CREATE TABLE IF NOT EXISTS ic (
  part_id       INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  package       TEXT,
  pins          INTEGER
);
-- A through-hole part, found again by the footprint signature it was
-- accepted for (thtdb.signature_key) and, for families that need it, the
-- normalized Value (thtdb.value_key, else '').
CREATE TABLE IF NOT EXISTS tht (
  part_id       INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  family        TEXT NOT NULL,
  signature     TEXT NOT NULL,
  value_key     TEXT NOT NULL DEFAULT '',
  footprint     TEXT NOT NULL DEFAULT '',
  positions     INTEGER
);
CREATE INDEX IF NOT EXISTS tht_signature ON tht(signature, value_key);
-- A one-colour LED. color is a kicadtools.ledcolor colour name, lens is
-- "clear", "diffused" or NULL, and the numbers are NULL when not listed.
CREATE TABLE IF NOT EXISTS led (
  part_id       INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  color         TEXT NOT NULL DEFAULT '',
  dominant_nm   REAL,
  peak_nm       REAL,
  intensity_mcd REAL,
  vf_v          REAL,
  lens          TEXT
);
CREATE TABLE IF NOT EXISTS supplier_pn (
  part_id       INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  supplier      TEXT NOT NULL,
  pn            TEXT NOT NULL,
  packaging     TEXT,
  jlc_library   TEXT,
  verified_at   TEXT NOT NULL,
  PRIMARY KEY (supplier, pn)
);
CREATE TABLE IF NOT EXISTS usage (
  part_id       INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  board         TEXT NOT NULL,
  reference     TEXT NOT NULL,
  used_at       TEXT NOT NULL,
  PRIMARY KEY (part_id, board, reference)
);
-- CPL rotation overrides by LCSC number, degrees CCW added to the family
-- offset (kicadtools.partrotation, DB_PLAN.md step 5). Only non-zero
-- offsets are stored.
CREATE TABLE IF NOT EXISTS part_rotation (
  lcsc          TEXT PRIMARY KEY,
  degrees       REAL NOT NULL
);
"""


@dataclass(frozen=True)
class _Table:
    """The parameter table of one part type: its name and the columns that
    hold the value, the rating and the class (a capacitor's dielectric, an
    inductor's shielding; ``None`` for a resistor)."""
    name: str
    nominal: str
    rating: str
    dielectric: Optional[str]


_TABLES: Dict[str, _Table] = {
    CAPACITOR.name: _Table("capacitor", "capacitance_f", "voltage_v", "dielectric"),
    RESISTOR.name: _Table("resistor", "resistance_ohm", "power_w", None),
    INDUCTOR.name: _Table("inductor", "inductance_h", "current_a", "shielding"),
}


# Every per-type parameter table; a part has a row in at most one of them.
_PARAM_TABLES = tuple(t.name for t in _TABLES.values()) + (IC, THT, LED)


def _q(name: str) -> str:
    """*name* as a quoted identifier: ``key`` and ``usage`` are reserved
    words in MariaDB. Both engines read double quotes as an identifier
    (MariaDB with ``ANSI_QUOTES``, set by :meth:`_MariaDialect.setup`)."""
    return f'"{name}"'


def _names(cols: Sequence[str]) -> str:
    return ", ".join(_q(c) for c in cols)


def _insert(table: str, cols: Sequence[str]) -> str:
    """``INSERT INTO "t"("a", "b") VALUES (?, ?)``."""
    return (f"INSERT INTO {_q(table)}({_names(cols)})"
            f" VALUES ({', '.join('?' * len(cols))})")


class _Dialect:
    """The SQL that differs between database engines, for SQLite
    (``DB_PLAN.md`` step 1; MariaDB is step 2). Statements elsewhere are
    written with ``?`` placeholders and standard SQL; :meth:`sql` turns one
    into this engine's text, and the methods below build the statements
    that have no standard form."""

    name = "sqlite"

    def sql(self, text: str) -> str:
        """*text* in this engine's placeholder style (SQLite: ``?``, as
        written)."""
        return text

    def upsert(self, table: str, cols: Sequence[str], keys: Sequence[str]) -> str:
        """Insert a row of *cols*, or update the row whose *keys* match:
        every column of *cols* not in *keys* is set from the new row, and a
        column not in *cols* keeps its value."""
        updates = [c for c in cols if c not in keys]
        tail = (" DO UPDATE SET " + ", ".join(f"{_q(c)} = excluded.{_q(c)}" for c in updates)
                if updates else " DO NOTHING")
        return f"{_insert(table, cols)} ON CONFLICT({_names(keys)}){tail}"

    def insert_ignore(self, table: str, cols: Sequence[str]) -> str:
        """Insert a row of *cols* unless one with the same key exists."""
        return "INSERT OR IGNORE" + _insert(table, cols)[len("INSERT"):]

    def has_table(self) -> str:
        """A query giving a row when the table named by its one parameter
        exists."""
        return "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"

    def schema(self) -> List[str]:
        """The ``CREATE … IF NOT EXISTS`` statements of the schema, in
        order."""
        return [stmt for stmt in _SCHEMA.split(";") if stmt.strip()]

    def setup(self, conn: Any) -> None:
        """Per-connection settings."""
        conn.execute("PRAGMA foreign_keys = ON")


SQLITE = _Dialect()


# The MariaDB schema (DB_PLAN.md step 2): the SQLite schema's tables and
# columns with MariaDB types. Text in a key or index is VARCHAR (an index
# needs a length). Tables compare text as bytes (utf8mb4_bin), as SQLite
# does; mpn and manufacturer ignore case (utf8mb4_unicode_ci), as SQLite's
# COLLATE NOCASE does, and that collation ignores accents too. Strict mode
# makes a value longer than its column an error.
_TABLE_OPTS = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
_CI = "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
_FK = "FOREIGN KEY (part_id) REFERENCES part(id) ON DELETE CASCADE"
_MARIA_SCHEMA = [
    'CREATE TABLE IF NOT EXISTS meta ("key" VARCHAR(64) PRIMARY KEY, value TEXT)' + _TABLE_OPTS,
    'CREATE TABLE IF NOT EXISTS settings ("key" VARCHAR(128) PRIMARY KEY, value TEXT)'
    + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS part (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  mpn           VARCHAR(128) {_CI} NOT NULL,
  manufacturer  VARCHAR(128) {_CI} NOT NULL,
  type          VARCHAR(16) NOT NULL,
  package       VARCHAR(128) NOT NULL,
  description   TEXT,
  datasheet_url TEXT,
  status        VARCHAR(16) NOT NULL,
  decided_at    VARCHAR(32) NOT NULL,
  notes         TEXT,
  UNIQUE KEY part_mfr_mpn (manufacturer, mpn)
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS capacitor (
  part_id       INT PRIMARY KEY,
  capacitance_f DOUBLE NOT NULL,
  voltage_v     DOUBLE NOT NULL,
  dielectric    VARCHAR(64) NOT NULL,
  tolerance_pct DOUBLE,
  {_FK}
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS resistor (
  part_id        INT PRIMARY KEY,
  resistance_ohm DOUBLE NOT NULL,
  power_w        DOUBLE,
  tolerance_pct  DOUBLE,
  {_FK}
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS inductor (
  part_id        INT PRIMARY KEY,
  inductance_h   DOUBLE NOT NULL,
  current_a      DOUBLE,
  shielding      VARCHAR(32) NOT NULL,
  tolerance_pct  DOUBLE,
  {_FK}
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS ic (
  part_id       INT PRIMARY KEY,
  package       VARCHAR(128),
  pins          INT,
  {_FK}
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS tht (
  part_id       INT PRIMARY KEY,
  family        VARCHAR(32) NOT NULL,
  signature     VARCHAR(255) NOT NULL,
  value_key     VARCHAR(128) NOT NULL DEFAULT '',
  footprint     VARCHAR(255) NOT NULL DEFAULT '',
  positions     INT,
  KEY tht_signature (signature, value_key),
  {_FK}
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS led (
  part_id       INT PRIMARY KEY,
  color         VARCHAR(32) NOT NULL DEFAULT '',
  dominant_nm   DOUBLE,
  peak_nm       DOUBLE,
  intensity_mcd DOUBLE,
  vf_v          DOUBLE,
  lens          VARCHAR(16),
  {_FK}
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS supplier_pn (
  part_id       INT NOT NULL,
  supplier      VARCHAR(16) NOT NULL,
  pn            VARCHAR(100) NOT NULL,
  packaging     VARCHAR(32),
  jlc_library   VARCHAR(16),
  verified_at   VARCHAR(32) NOT NULL,
  PRIMARY KEY (supplier, pn),
  {_FK}
)""" + _TABLE_OPTS,
    f"""CREATE TABLE IF NOT EXISTS "usage" (
  part_id       INT NOT NULL,
  board         VARCHAR(255) NOT NULL,
  reference     VARCHAR(32) NOT NULL,
  used_at       VARCHAR(32) NOT NULL,
  PRIMARY KEY (part_id, board, reference),
  {_FK}
)""" + _TABLE_OPTS,
    """CREATE TABLE IF NOT EXISTS part_rotation (
  lcsc          VARCHAR(32) PRIMARY KEY,
  degrees       DOUBLE NOT NULL
)""" + _TABLE_OPTS,
]


class _MariaDialect(_Dialect):
    """The MariaDB forms (``DB_PLAN.md`` step 2), through PyMySQL."""

    name = "mariadb"

    def sql(self, text: str) -> str:
        """``?`` placeholders as PyMySQL's ``%s``; a literal ``%`` doubled."""
        return text.replace("%", "%%").replace("?", "%s")

    def upsert(self, table: str, cols: Sequence[str], keys: Sequence[str]) -> str:
        """As :meth:`_Dialect.upsert`, with ``ON DUPLICATE KEY UPDATE``. Not
        ``REPLACE``: it deletes the old row first, and ``ON DELETE CASCADE``
        would delete the part's rows in the other tables with it. Every table
        upserted here has one unique key, the one named by *keys*."""
        updates = [c for c in cols if c not in keys]
        if not updates:
            return self.insert_ignore(table, cols)
        sets = ", ".join(f"{_q(c)} = VALUES({_q(c)})" for c in updates)
        return f"{_insert(table, cols)} ON DUPLICATE KEY UPDATE {sets}"

    def insert_ignore(self, table: str, cols: Sequence[str]) -> str:
        return "INSERT IGNORE" + _insert(table, cols)[len("INSERT"):]

    def has_table(self) -> str:
        return ("SELECT 1 FROM information_schema.tables"
                " WHERE table_schema = DATABASE() AND table_name = ?")

    def schema(self) -> List[str]:
        return list(_MARIA_SCHEMA)

    def setup(self, conn: Any) -> None:
        """Double quotes name identifiers (``ANSI_QUOTES``), as in SQLite."""
        with conn.cursor() as cur:
            cur.execute("SET SESSION sql_mode = CONCAT(@@sql_mode, ',ANSI_QUOTES')")


MARIADB = _MariaDialect()


def _table(kind: str) -> _Table:
    try:
        return _TABLES[kind]
    except KeyError:
        raise ValueError(f"no parts table for type {kind!r}") from None


def default_path() -> Path:
    """``$KICADTOOLS_PARTS_DB`` when set, else ``parts.sqlite`` at the repo root
    (:func:`kicadtools.repo.repo_root`), else ``~/.kicadtools/parts.sqlite``
    when KicadTools is not run from a checkout."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    root = repo.repo_root()
    if root is not None:
        return root / "parts.sqlite"
    return Path.home() / ".kicadtools" / "parts.sqlite"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_class_field(kind: str, text: object) -> str:
    """A class (dielectric, shielding) as stored: each code normalized by the
    type's tokenizer and joined with ``", "``. A single code is stored as that
    code (``"x7r"`` -> ``"X7R"``); a supplier string naming two codes is
    stored as both (``"C0G/NP0"`` -> ``"C0G, NP0"``) rather than run
    together."""
    return ", ".join(part_type(kind).class_tokens(text))


def _opt_text(text: Optional[str]) -> Optional[str]:
    """*text* stripped, or ``None`` when that leaves it empty."""
    s = (text or "").strip()
    return s or None


def norm_mpn(mpn: object) -> str:
    """An MPN as compared when ICs are looked up: whitespace removed and
    upper-cased (``" ldl212pv33r "`` -> ``"LDL212PV33R"``). Punctuation is
    kept: ``LTC6992CS6-2`` and ``LTC6992CS62`` are different strings, and
    ``#TRPBF``-style suffixes name different orderable parts."""
    return "".join(str(mpn or "").split()).upper()


@dataclass(frozen=True)
class PartParams:
    """The parameters that make two parts of one type interchangeable on a
    footprint: the value (farads, ohms, henries), the rating (volts, watts,
    saturation amps), the class (a capacitor's dielectric, an inductor's
    shielding; ``""`` otherwise) and the tolerance."""

    nominal: float
    rating: Optional[float]
    dielectric: str
    tolerance_pct: Optional[float]


@dataclass(frozen=True)
class PartChoice:
    """A part to record as accepted or rejected, with its supplier part
    numbers. ``kind`` is the part type (``"capacitor"``, ``"resistor"``);
    ``jlc_library`` is ``"basic"`` or ``"extended"``."""

    kind: str
    mpn: str
    manufacturer: str
    package: str
    params: PartParams
    digikey_pn: Optional[str] = None
    digikey_packaging: Optional[str] = None
    lcsc: Optional[str] = None
    jlc_library: Optional[str] = None
    description: str = ""
    datasheet_url: str = ""


@dataclass(frozen=True)
class IcChoice:
    """An IC to record as accepted or rejected. ``package`` is DigiKey's
    ``Supplier Device Package`` (``"24-QFN (4x4)"``, stored in
    ``part.package``), ``package_case`` its ``Package / Case``
    (``"24-VFQFN Exposed Pad"``, stored in ``ic.package``), ``pins`` its
    ``Number of Pins``; each may be empty when DigiKey states none. ``lcsc``
    is ``None`` for a part JLCPCB does not stock."""

    kind: ClassVar[str] = IC
    mpn: str
    manufacturer: str
    package: str = ""
    package_case: str = ""
    pins: Optional[int] = None
    digikey_pn: Optional[str] = None
    digikey_packaging: Optional[str] = None
    lcsc: Optional[str] = None
    jlc_library: Optional[str] = None
    description: str = ""
    datasheet_url: str = ""


@dataclass(frozen=True)
class DbIc:
    """An accepted IC read back from the database; fields as in
    :class:`IcChoice` (empty strings for packages not recorded).
    ``usage_count`` is the number of (board, reference) pairs recorded
    against it."""

    kind: ClassVar[str] = IC
    part_id: int
    mpn: str
    manufacturer: str
    package: str
    package_case: str
    pins: Optional[int]
    digikey_pn: Optional[str]
    lcsc: Optional[str]
    jlc_library: Optional[str]
    usage_count: int
    description: str = ""
    datasheet_url: str = ""


@dataclass(frozen=True)
class ThtChoice:
    """A through-hole part to record as accepted or rejected. ``signature``
    and ``value_key`` are the lookup key (:func:`kicadtools.thtdb.db_key`);
    ``footprint`` the footprint it was accepted for; ``package`` DigiKey's
    ``Package / Case`` when it states one. Through-hole parts are soldered by
    hand, so there is no LCSC number."""

    kind: ClassVar[str] = THT
    mpn: str
    manufacturer: str
    family: str
    signature: str
    value_key: str = ""
    footprint: str = ""
    positions: Optional[int] = None
    package: str = ""
    digikey_pn: Optional[str] = None
    digikey_packaging: Optional[str] = None
    description: str = ""
    datasheet_url: str = ""
    lcsc: ClassVar[Optional[str]] = None
    jlc_library: ClassVar[Optional[str]] = None


@dataclass(frozen=True)
class DbTht:
    """An accepted through-hole part read back from the database; fields as
    in :class:`ThtChoice`. ``usage_count`` is the number of (board,
    reference) pairs recorded against it."""

    kind: ClassVar[str] = THT
    part_id: int
    mpn: str
    manufacturer: str
    family: str
    signature: str
    value_key: str
    footprint: str
    positions: Optional[int]
    package: str
    digikey_pn: Optional[str]
    usage_count: int
    description: str = ""
    datasheet_url: str = ""


def _opt_float(x: object) -> Optional[float]:
    return None if x is None else float(x)


@dataclass(frozen=True)
class LedChoice:
    """A one-colour LED to record as accepted or rejected."""

    kind: ClassVar[str] = LED
    mpn: str
    manufacturer: str
    package: str
    color: str = ""
    dominant_nm: Optional[float] = None
    peak_nm: Optional[float] = None
    intensity_mcd: Optional[float] = None
    vf_v: Optional[float] = None
    lens: Optional[str] = None
    digikey_pn: Optional[str] = None
    digikey_packaging: Optional[str] = None
    lcsc: Optional[str] = None
    jlc_library: Optional[str] = None
    description: str = ""
    datasheet_url: str = ""


@dataclass(frozen=True)
class DbLed:
    """An accepted LED read back from the database; fields as in
    :class:`LedChoice`. ``params`` gives the shape
    :func:`kicadtools.partsearch._from_database` reads (dominant wavelength
    as the value, colour as the class)."""

    kind: ClassVar[str] = LED
    part_id: int
    mpn: str
    manufacturer: str
    package: str
    color: str
    dominant_nm: Optional[float]
    peak_nm: Optional[float]
    intensity_mcd: Optional[float]
    vf_v: Optional[float]
    lens: Optional[str]
    digikey_pn: Optional[str]
    lcsc: Optional[str]
    jlc_library: Optional[str]
    usage_count: int
    description: str = ""
    datasheet_url: str = ""

    @property
    def params(self) -> PartParams:
        return PartParams(self.dominant_nm or 0.0, None, self.color, None)


@dataclass(frozen=True)
class DbPart:
    """An accepted part read back from the database. ``usage_count`` is
    the number of (board, reference) pairs recorded against it."""

    kind: str
    part_id: int
    mpn: str
    manufacturer: str
    package: str
    params: PartParams
    digikey_pn: Optional[str]
    lcsc: Optional[str]
    jlc_library: Optional[str]
    usage_count: int
    description: str = ""
    datasheet_url: str = ""


class PartsDb:
    """The parts database at *path*; ``":memory:"`` gives an ephemeral one
    (tests). *now* returns the ISO-8601 timestamp written to ``decided_at``,
    ``verified_at`` and ``used_at``; the default is UTC now to the second.

    A version-1 file is migrated to version 2 on open (see the module
    docstring). Raises :class:`ValueError` when the file was stamped with any
    other version.
    """

    def __init__(
        self,
        path: Path | str = ":memory:",
        *,
        now: Optional[Callable[[], str]] = None,
        _conn: Any = None,
        _dialect: Optional[_Dialect] = None,
        _read_only: bool = False,
    ) -> None:
        self._now = now or _utc_now
        self._lock = threading.RLock()
        self._dialect = _dialect or SQLITE
        self.label = str(path)
        self.read_only = _read_only
        self._last_used = time.monotonic()
        if _conn is not None:
            self.conn = _conn
        else:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: the GUI opens the database on the main
            # thread and searches it on a worker thread; self._lock serializes use.
            self.conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self._dialect.setup(self.conn)
            if _read_only:
                self._check_version(path)
                return
            # The version is checked before the schema is applied, so a file of
            # another version is refused without tables added to it.
            if (self._dialect is SQLITE
                    and self._stored_version() == str(_MIGRATABLE_VERSION)):
                self._migrate_v1()
            self._check_version(path)
            with self._tx():
                for statement in self._dialect.schema():
                    self._execute(statement)
                self._execute(self._dialect.insert_ignore("meta", ("key", "value")),
                              ("schema_version", str(SCHEMA_VERSION)))
            self._check_version(path)
        except Exception:
            self.conn.close()
            raise

    # ----- statements ------------------------------------------------------

    # A server connection idle longer than this is pinged before use: the
    # server closes idle connections (wait_timeout), and a reconnect needs
    # the session settings again.
    _IDLE_S = 60.0

    def _keepalive(self) -> None:
        if self._dialect is SQLITE:
            return
        now = time.monotonic()
        if now - self._last_used > self._IDLE_S:
            before = self.conn.thread_id()
            self.conn.ping(reconnect=True)
            if self.conn.thread_id() != before:
                self._dialect.setup(self.conn)
        self._last_used = now

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run *sql* (``?`` placeholders) with *params*; the cursor."""
        self._keepalive()
        cur = self.conn.cursor()
        cur.execute(self._dialect.sql(sql), tuple(params))
        return cur

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self._keepalive()
        cur = self.conn.cursor()
        cur.executemany(self._dialect.sql(sql), [tuple(r) for r in rows])

    def _insert_rows(self, table: str, cols: Sequence[str],
                     rows: Sequence[Sequence[Any]]) -> None:
        """Insert *rows* of *cols* into *table* as given (ids included),
        inside the caller's transaction: the import of ``kicadtools.dbimport``."""
        self._executemany(_insert(table, cols), rows)

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """One transaction, holding the lock: committed when the block ends,
        rolled back when it raises."""
        with self._lock:
            try:
                yield
            except BaseException:
                self.conn.rollback()
                raise
            self.conn.commit()

    @classmethod
    def connect_mariadb(cls, *, host: str, port: int, user: str, password: str,
                        database: str, now: Optional[Callable[[], str]] = None,
                        connect_timeout: int = 10) -> "PartsDb":
        """The parts database in MariaDB *database* (``DB_PLAN.md``): the same
        tables and methods as a SQLite file. Needs PyMySQL (``pip install
        kicadtools[server]``). The tables are created when missing and the
        schema version checked, as for a file. Raises
        :class:`pymysql.err.OperationalError` when the login fails."""
        import pymysql

        conn = pymysql.connect(host=host, port=int(port), user=user, password=password,
                               database=database, charset="utf8mb4", autocommit=False,
                               connect_timeout=connect_timeout)
        return cls(f"{user}@{host}:{port}/{database}", now=now, _conn=conn,
                   _dialect=MARIADB)

    @classmethod
    def open_read_only(cls, path: Path | str,
                       now: Optional[Callable[[], str]] = None) -> "PartsDb":
        """The SQLite file at *path*, read-only (the offline copy of
        ``DB_PLAN.md`` decision 1): searches work, and any write raises
        ``sqlite3.OperationalError``. The schema is not applied; the version
        is checked."""
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True,
                               check_same_thread=False)
        return cls(path, now=now, _conn=conn, _read_only=True)

    @staticmethod
    def check_mariadb_login(*, host: str, port: int, user: str, password: str,
                            database: str, connect_timeout: int = 10) -> None:
        """Log in to MariaDB *database* and close the connection, creating
        nothing: the check :func:`kicadtools.dbconfig.obtain_password` runs
        on a password. Raises PyMySQL's ``OperationalError`` when the server
        refuses the login (codes 1044, 1045) or cannot be reached."""
        import pymysql

        pymysql.connect(host=host, port=int(port), user=user, password=password,
                        database=database, connect_timeout=connect_timeout).close()

    @property
    def backend(self) -> str:
        """``"sqlite"`` or ``"mariadb"``."""
        return self._dialect.name

    def _stored_version(self) -> Optional[str]:
        """``meta.schema_version`` of the open database; ``None`` for a new
        one (no ``meta`` table, or no version row in it)."""
        if not self._execute(self._dialect.has_table(), ("meta",)).fetchone():
            return None
        row = self._execute(
            'SELECT value FROM meta WHERE "key" = \'schema_version\'').fetchone()
        return None if row is None else row[0]

    def _check_version(self, path: Path | str) -> None:
        version = self._stored_version()
        if version is not None and version != str(SCHEMA_VERSION):
            raise ValueError(
                f"parts database {path} has schema version {version}; "
                f"this KicadTools reads version {SCHEMA_VERSION}"
            )

    def _migrate_v1(self) -> None:
        """Version 1 -> 2: add the ``ic`` table (and any other table the
        version-1 file lacks) and stamp version 2, in one transaction, so an
        interrupted migration leaves a version-1 file. No row is changed but
        ``meta.schema_version``."""
        # The schema text holds only CREATE ... IF NOT EXISTS, so it adds the
        # ic table and leaves the version-1 tables as they are. It is run
        # statement by statement because executescript commits first. SQLite
        # only: no other engine holds a version-1 database.
        self.conn.execute("BEGIN")
        try:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self.conn.execute(statement)
            self.conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ----- decisions -------------------------------------------------------

    def accept_part(self, choice: Union[PartChoice, IcChoice, ThtChoice, LedChoice]) -> int:
        """Record *choice* (a capacitor, resistor, inductor, IC or
        through-hole part) as accepted and
        return its part id. An existing row for the same (manufacturer, MPN)
        is updated in place, including one previously rejected."""
        return self._upsert(choice, ACCEPTED)

    def reject_part(self, choice: Union[PartChoice, IcChoice, ThtChoice, LedChoice]) -> int:
        """Record *choice* as rejected and return its part id. A rejected part
        is excluded from :meth:`find_parts`, :meth:`find_ics` and
        :meth:`find_tht`."""
        return self._upsert(choice, REJECTED)

    def is_rejected(self, manufacturer: str, mpn: str) -> bool:
        """True when (manufacturer, MPN) is recorded with status rejected."""
        with self._lock:
            row = self._execute(
                "SELECT status FROM part WHERE manufacturer = ? AND mpn = ?",
                ((manufacturer or "").strip(), (mpn or "").strip()),
            ).fetchone()
        return row is not None and row[0] == REJECTED

    def _upsert(self, choice: Union[PartChoice, IcChoice, ThtChoice, LedChoice], status: str) -> int:
        if isinstance(choice, IcChoice):
            table_name, row = IC, self._ic_row
        elif isinstance(choice, ThtChoice):
            table_name, row = THT, self._tht_row
        elif isinstance(choice, LedChoice):
            table_name, row = LED, self._led_row
        else:
            table_name, row = _table(choice.kind).name, self._param_row
        mpn = (choice.mpn or "").strip()
        manufacturer = (choice.manufacturer or "").strip()
        if not mpn:
            raise ValueError(f"a {choice.kind} choice needs an MPN")
        stamp = self._now()
        digikey_pn = _opt_text(choice.digikey_pn)
        lcsc = _opt_text(choice.lcsc)
        jlc_library = _opt_text(choice.jlc_library)
        if jlc_library is not None:
            jlc_library = jlc_library.lower()
        with self._tx():
            self._execute(
                self._dialect.upsert(
                    "part", ("mpn", "manufacturer", "type", "package", "description",
                             "datasheet_url", "status", "decided_at"),
                    ("manufacturer", "mpn")),
                (mpn, manufacturer, choice.kind, (choice.package or "").strip(),
                 choice.description, choice.datasheet_url, status, stamp),
            )
            part_id = self._execute(
                "SELECT id FROM part WHERE manufacturer = ? AND mpn = ?",
                (manufacturer, mpn),
            ).fetchone()[0]
            # A part whose type changed keeps no row in the other tables.
            for other in _PARAM_TABLES:
                if other != table_name:
                    self._execute(
                        f"DELETE FROM {other} WHERE part_id = ?", (part_id,))
            cols, values = row(part_id, choice)
            self._execute(self._dialect.upsert(table_name, cols, ("part_id",)), values)
            # A part has at most one PN per supplier: the rows for a supplier
            # named in the choice are replaced, so a changed PN leaves no stale
            # row behind. A supplier left as None or "" keeps its existing row.
            # A supplier PN identifies one part, so a PN recorded against
            # another part moves to this one (an upsert on the key).
            if digikey_pn:
                self._replace_pn(part_id, "digikey", digikey_pn,
                                 _opt_text(choice.digikey_packaging), None, stamp)
            if lcsc:
                self._replace_pn(part_id, "lcsc", lcsc, None, jlc_library, stamp)
        return int(part_id)

    @staticmethod
    def _param_row(part_id: int, choice: PartChoice) -> tuple:
        """Columns and values of a capacitor, resistor or inductor row."""
        table = _table(choice.kind)
        p = choice.params
        cols = ["part_id", table.nominal, table.rating, "tolerance_pct"]
        values: list = [part_id, float(p.nominal),
                        None if p.rating is None else float(p.rating),
                        None if p.tolerance_pct is None else float(p.tolerance_pct)]
        if table.dielectric:
            cols.append(table.dielectric)
            values.append(_norm_class_field(choice.kind, p.dielectric))
        return cols, values

    @staticmethod
    def _ic_row(part_id: int, choice: IcChoice) -> tuple:
        """Columns and values of an ic row."""
        pins = None if choice.pins is None else int(choice.pins)
        return (["part_id", "package", "pins"],
                [part_id, _opt_text(choice.package_case), pins])

    @staticmethod
    def _tht_row(part_id: int, choice: ThtChoice) -> tuple:
        """Columns and values of a tht row."""
        if not (choice.signature or "").strip():
            raise ValueError("a through-hole choice needs a footprint signature")
        positions = None if choice.positions is None else int(choice.positions)
        return (["part_id", "family", "signature", "value_key", "footprint", "positions"],
                [part_id, choice.family, choice.signature.strip(),
                 (choice.value_key or "").strip(), (choice.footprint or "").strip(),
                 positions])

    @staticmethod
    def _led_row(part_id: int, choice: LedChoice) -> tuple:
        """Columns and values of a led row."""
        return (["part_id", "color", "dominant_nm", "peak_nm", "intensity_mcd",
                 "vf_v", "lens"],
                [part_id, (choice.color or "").strip(),
                 _opt_float(choice.dominant_nm), _opt_float(choice.peak_nm),
                 _opt_float(choice.intensity_mcd), _opt_float(choice.vf_v),
                 _opt_text(choice.lens)])

    def _replace_pn(self, part_id: int, supplier: str, pn: str,
                    packaging: Optional[str], jlc_library: Optional[str],
                    stamp: str) -> None:
        self._execute(
            "DELETE FROM supplier_pn WHERE part_id = ? AND supplier = ?",
            (part_id, supplier),
        )
        self._execute(
            self._dialect.upsert(
                "supplier_pn",
                ("part_id", "supplier", "pn", "packaging", "jlc_library", "verified_at"),
                ("supplier", "pn")),
            (part_id, supplier, pn, packaging, jlc_library, stamp),
        )

    # ----- search ----------------------------------------------------------

    def find_parts(
        self,
        *,
        kind: str,
        nominal: float,
        package: str,
        rating_min: Optional[float],
        dielectrics: Sequence[str],
        tolerance_max_pct: Optional[float],
    ) -> List[DbPart]:
        """Accepted parts of type *kind* meeting a requirement: value within
        0.1 % of *nominal*, the same *package* (a body size within 0.25 mm per
        side, :meth:`~kicadtools.parttypes.PartType.same_package`), a rating of
        at least *rating_min* (any rating when ``None``), a class in
        *dielectrics* (a capacitor's dielectric, an inductor's shielding;
        compared normalized; any class when empty), and,
        when *tolerance_max_pct* is given, a stated tolerance no looser than
        it. Ordered by usage count (highest first), then JLCPCB Basic
        before Extended, then MPN.

        Each entry of *dielectrics* may itself list codes (``"C0G, NP0"``); a
        stored part matches when any of its codes is allowed."""
        table = _table(kind)
        ptype = part_type(kind)
        allowed = {t for d in dielectrics for t in ptype.class_tokens(d)}
        # A body size is compared after the query: suppliers round one part's
        # size differently, so the stored text need not equal the requested.
        by_body = ptype.is_body_size(package)
        band = abs(float(nominal)) * VALUE_REL_TOL
        diel_col = f"c.{table.dielectric}" if table.dielectric else "''"
        sql = (
            "SELECT p.id, p.mpn, p.manufacturer, p.package, p.description,"
            f" p.datasheet_url, c.{table.nominal}, c.{table.rating}, {diel_col},"
            " c.tolerance_pct,"
            " (SELECT pn FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'digikey' ORDER BY pn LIMIT 1),"
            " (SELECT pn FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'lcsc' ORDER BY pn LIMIT 1) AS lcsc,"
            " (SELECT jlc_library FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'lcsc' ORDER BY pn LIMIT 1) AS jlc_library,"
            ' (SELECT COUNT(*) FROM "usage" WHERE part_id = p.id) AS usage_count'
            f" FROM part p JOIN {table.name} c ON c.part_id = p.id"
            " WHERE p.status = ?"
            + ("" if by_body else " AND p.package = ?")
            + f" AND ABS(c.{table.nominal} - ?) <= ?"
        )
        args: list = [ACCEPTED]
        if not by_body:
            args.append((package or "").strip())
        args += [float(nominal), band]
        if rating_min is not None:
            sql += f" AND c.{table.rating} >= ?"
            args.append(float(rating_min))
        if tolerance_max_pct is not None:
            # A part with no stated tolerance cannot be shown to meet a limit.
            sql += " AND c.tolerance_pct IS NOT NULL AND c.tolerance_pct <= ?"
            args.append(float(tolerance_max_pct))
        sql += (
            " ORDER BY usage_count DESC,"
            " CASE WHEN LOWER(jlc_library) = 'basic' THEN 0 ELSE 1 END,"
            " p.mpn"
        )
        with self._lock:
            rows = self._execute(sql, args).fetchall()
        out: List[DbPart] = []
        for row in rows:
            (pid, mpn, mfr, pkg, desc, url, value, rating, diel, tol,
             dk, lcsc, lib, uses) = row
            # The dielectric filter runs here: a stored value can list two
            # codes, which an SQL IN test would compare as one string.
            if allowed and not allowed.intersection(ptype.class_tokens(diel)):
                continue
            if by_body and not ptype.same_package(pkg, package):
                continue
            out.append(DbPart(
                kind=kind, part_id=int(pid), mpn=mpn, manufacturer=mfr, package=pkg,
                params=PartParams(value, rating, diel or "", tol),
                digikey_pn=dk, lcsc=lcsc, jlc_library=lib,
                usage_count=int(uses),
                description=desc or "", datasheet_url=url or "",
            ))
        return out

    def find_ics(self, mpn: str) -> List[DbIc]:
        """Accepted ICs whose MPN equals *mpn* after :func:`norm_mpn` (any
        manufacturer). Ordered as :meth:`find_parts`: usage count (highest
        first), then JLCPCB Basic before Extended (a part with no LCSC row
        last), then manufacturer. An empty *mpn* finds nothing."""
        key = norm_mpn(mpn)
        if not key:
            return []
        sql = (
            "SELECT p.id, p.mpn, p.manufacturer, p.package, p.description,"
            " p.datasheet_url, i.package, i.pins,"
            " (SELECT pn FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'digikey' ORDER BY pn LIMIT 1),"
            " (SELECT pn FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'lcsc' ORDER BY pn LIMIT 1) AS lcsc,"
            " (SELECT jlc_library FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'lcsc' ORDER BY pn LIMIT 1) AS jlc_library,"
            ' (SELECT COUNT(*) FROM "usage" WHERE part_id = p.id) AS usage_count'
            " FROM part p JOIN ic i ON i.part_id = p.id"
            " WHERE p.status = ? AND p.type = ?"
            " ORDER BY usage_count DESC,"
            " CASE WHEN LOWER(jlc_library) = 'basic' THEN 0"
            "      WHEN lcsc IS NOT NULL THEN 1 ELSE 2 END,"
            " p.manufacturer"
        )
        with self._lock:
            rows = self._execute(sql, (ACCEPTED, IC)).fetchall()
        # Stored MPNs are stripped but may hold inner whitespace; the stored
        # side goes through the same norm_mpn as the argument. Compared here,
        # not in SQL, so the query is the same on every engine.
        rows = [r for r in rows if norm_mpn(r[1]) == key]
        return [
            DbIc(part_id=int(pid), mpn=m, manufacturer=mfr, package=pkg or "",
                 package_case=case or "", pins=None if pins is None else int(pins),
                 digikey_pn=dk, lcsc=lcsc, jlc_library=lib, usage_count=int(uses),
                 description=desc or "", datasheet_url=url or "")
            for (pid, m, mfr, pkg, desc, url, case, pins, dk, lcsc, lib, uses)
            in rows
        ]

    def find_tht(self, signature: str, value_key: str = "") -> List[DbTht]:
        """Accepted through-hole parts recorded for footprint *signature* and
        *value_key* (both as :func:`kicadtools.thtdb.db_key` gives them).
        Ordered by usage count (highest first), then the latest decision,
        then MPN. An empty *signature* finds nothing."""
        signature = (signature or "").strip()
        if not signature:
            return []
        sql = (
            "SELECT p.id, p.mpn, p.manufacturer, p.package, p.description,"
            " p.datasheet_url, t.family, t.signature, t.value_key, t.footprint,"
            " t.positions,"
            " (SELECT pn FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'digikey' ORDER BY pn LIMIT 1),"
            ' (SELECT COUNT(*) FROM "usage" WHERE part_id = p.id) AS usage_count'
            " FROM part p JOIN tht t ON t.part_id = p.id"
            " WHERE p.status = ? AND p.type = ? AND t.signature = ? AND t.value_key = ?"
            " ORDER BY usage_count DESC, p.decided_at DESC, p.mpn"
        )
        with self._lock:
            rows = self._execute(
                sql, (ACCEPTED, THT, signature, (value_key or "").strip())).fetchall()
        return [
            DbTht(part_id=int(pid), mpn=m, manufacturer=mfr, family=fam,
                  signature=sig, value_key=vk, footprint=fp or "",
                  positions=None if pos is None else int(pos), package=pkg or "",
                  digikey_pn=dk, usage_count=int(uses),
                  description=desc or "", datasheet_url=url or "")
            for (pid, m, mfr, pkg, desc, url, fam, sig, vk, fp, pos, dk, uses) in rows
        ]

    def find_leds(self, package: str) -> List[DbLed]:
        """Accepted one-colour LEDs in chip *package*, ordered by usage count
        (highest first), then JLCPCB Basic, then the latest decision. The
        caller checks colour and limits
        (:func:`kicadtools.ledsearch.check_led`)."""
        package = (package or "").strip()
        if not package:
            return []
        sql = (
            "SELECT p.id, p.mpn, p.manufacturer, p.package, p.description,"
            " p.datasheet_url, l.color, l.dominant_nm, l.peak_nm, l.intensity_mcd,"
            " l.vf_v, l.lens,"
            " (SELECT pn FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'digikey' ORDER BY pn LIMIT 1),"
            " (SELECT pn FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'lcsc' ORDER BY pn LIMIT 1),"
            " (SELECT jlc_library FROM supplier_pn WHERE part_id = p.id"
            "   AND supplier = 'lcsc' ORDER BY pn LIMIT 1) AS lib,"
            ' (SELECT COUNT(*) FROM "usage" WHERE part_id = p.id) AS usage_count'
            " FROM part p JOIN led l ON l.part_id = p.id"
            " WHERE p.status = ? AND p.type = ? AND p.package = ?"
            " ORDER BY usage_count DESC, (lib = 'basic') DESC, p.decided_at DESC"
        )
        with self._lock:
            rows = self._execute(sql, (ACCEPTED, LED, package)).fetchall()
        return [
            DbLed(part_id=int(pid), mpn=m, manufacturer=mfr, package=pkg or "",
                  color=col or "", dominant_nm=dom, peak_nm=peak,
                  intensity_mcd=mcd, vf_v=vf, lens=lens, digikey_pn=dk,
                  lcsc=lcsc, jlc_library=lib, usage_count=int(uses),
                  description=desc or "", datasheet_url=url or "")
            for (pid, m, mfr, pkg, desc, url, col, dom, peak, mcd, vf, lens, dk,
                 lcsc, lib, uses) in rows
        ]

    # ----- usage -----------------------------------------------------------

    def record_usage(self, part_id: int, board: str,
                     references: Sequence[str]) -> None:
        """Record that *references* on *board* (schematic path relative to
        ``Boards/``) place part *part_id*. A pair already recorded has its
        ``used_at`` refreshed."""
        stamp = self._now()
        with self._tx():
            self._executemany(
                self._dialect.upsert("usage", ("part_id", "board", "reference", "used_at"),
                                     ("part_id", "board", "reference")),
                [(part_id, board, ref, stamp) for ref in references],
            )

    # ----- CPL rotation overrides -------------------------------------------
    # The interface of kicadtools.partrotation.PartRotationStore, so the
    # LCSC tab and the CPL export read the overrides from this database.

    def rotation(self, lcsc: str) -> Optional[float]:
        """The extra rotation stored for *lcsc*, or ``None``."""
        if not lcsc:
            return None
        with self._lock:
            row = self._execute("SELECT degrees FROM part_rotation WHERE lcsc = ?",
                                (lcsc,)).fetchone()
        return float(row[0]) if row else None

    def set_rotation(self, lcsc: str, degrees: float) -> None:
        """Store *degrees* (mod 360) for *lcsc*; zero removes the entry."""
        if not lcsc:
            return
        normalized = float(degrees) % 360.0
        with self._tx():
            if normalized == 0.0:
                self._execute("DELETE FROM part_rotation WHERE lcsc = ?", (lcsc,))
            else:
                self._execute(self._dialect.upsert("part_rotation", ("lcsc", "degrees"),
                                                   ("lcsc",)), (lcsc, normalized))

    def rotations(self) -> Dict[str, float]:
        """Every stored override as ``{lcsc: degrees}``."""
        with self._lock:
            return {lcsc: float(deg) for lcsc, deg in self._execute(
                "SELECT lcsc, degrees FROM part_rotation").fetchall()}

    # ----- settings --------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        """The stored value of setting *key*, or ``None``."""
        with self._lock:
            row = self._execute(
                'SELECT value FROM settings WHERE "key" = ?', (key,)
            ).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Store *value* under setting *key*, replacing any earlier value."""
        with self._tx():
            self._execute(self._dialect.upsert("settings", ("key", "value"), ("key",)),
                          (key, value))

    def settings(self) -> Dict[str, str]:
        """Every stored setting as ``{key: value}``."""
        with self._lock:
            return {k: v for k, v in self._execute(
                'SELECT "key", value FROM settings').fetchall()}

    def close(self) -> None:
        """Close the connection; the object is unusable afterwards."""
        with self._lock:
            self.conn.close()
