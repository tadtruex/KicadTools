"""Local SQLite database of parts accepted (or rejected) for use on the boards.

A part accepted in the Cockpit's assign dialog is recorded here with its
parameters and supplier part numbers, so a later search for the same
requirement finds it before any supplier query. A rejected part is recorded
too, with status ``rejected``, and is excluded from every candidate list.

The default file is ``~/Code/Boards/parts.sqlite`` (the same relative layout on
every machine), overridable with ``KICADTOOLS_PARTS_DB``. The file is listed in
``Boards/.gitignore`` and is machine-local.

Tables (see ``CAPACITOR_PLAN.md``):

- ``meta`` — ``schema_version``, stamped at creation and checked on open;
- ``settings`` — last-used defaults (``cap.package``, ``cap.voltage_min``,
  ``res.tolerance`` …);
- ``part`` — one row per (manufacturer, MPN), with its type and decision
  status;
- ``capacitor`` — capacitance, voltage, dielectric and tolerance of a part;
- ``resistor`` — resistance, power and tolerance of a part;
- ``supplier_pn`` — DigiKey and LCSC part numbers of a part;
- ``usage`` — which board references place a part.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from kicadtools.parttypes import CAPACITOR, RESISTOR
from kicadtools.units import dielectric_tokens

SCHEMA_VERSION = 1

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
"""


@dataclass(frozen=True)
class _Table:
    """The parameter table of one part type: its name and the columns that
    hold the value, the rating and (capacitors) the dielectric."""
    name: str
    nominal: str
    rating: str
    dielectric: Optional[str]


_TABLES: Dict[str, _Table] = {
    CAPACITOR.name: _Table("capacitor", "capacitance_f", "voltage_v", "dielectric"),
    RESISTOR.name: _Table("resistor", "resistance_ohm", "power_w", None),
}


def _table(kind: str) -> _Table:
    try:
        return _TABLES[kind]
    except KeyError:
        raise ValueError(f"no parts table for type {kind!r}") from None


def default_path() -> Path:
    """``$KICADTOOLS_PARTS_DB`` when set, else ``~/Code/Boards/parts.sqlite``."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return Path.home() / "Code" / "Boards" / "parts.sqlite"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_dielectric_field(text: object) -> str:
    """A dielectric as stored: each code normalized (``units.norm_dielectric``)
    and joined with ``", "``. A single code is stored as that code
    (``"x7r"`` -> ``"X7R"``); a supplier string naming two codes is stored as
    both (``"C0G/NP0"`` -> ``"C0G, NP0"``) rather than run together."""
    return ", ".join(dielectric_tokens(text))


def _opt_text(text: Optional[str]) -> Optional[str]:
    """*text* stripped, or ``None`` when that leaves it empty."""
    s = (text or "").strip()
    return s or None


@dataclass(frozen=True)
class PartParams:
    """The parameters that make two parts of one type interchangeable on a
    footprint: the value (farads, ohms), the rating (volts, watts), the
    dielectric (capacitors; ``""`` otherwise) and the tolerance."""

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

    Raises :class:`ValueError` when the file was stamped with a different
    schema version.
    """

    def __init__(
        self,
        path: Path | str = ":memory:",
        *,
        now: Optional[Callable[[], str]] = None,
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._now = now or _utc_now
        self._lock = threading.RLock()
        # check_same_thread=False: the GUI opens the database on the main
        # thread and searches it on a worker thread; self._lock serializes use.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self.conn.execute("PRAGMA foreign_keys = ON")
            # The version is checked before the schema is applied, so a file of
            # another version is refused without tables added to it.
            has_meta = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
            ).fetchone()
            if has_meta:
                self._check_version(path)
            self.conn.executescript(_SCHEMA)
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value)"
                    " VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            self._check_version(path)
        except Exception:
            self.conn.close()
            raise

    def _check_version(self, path: Path | str) -> None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None and row[0] != str(SCHEMA_VERSION):
            raise ValueError(
                f"parts database {path} has schema version {row[0]}; "
                f"this KicadTools reads version {SCHEMA_VERSION}"
            )

    # ----- decisions -------------------------------------------------------

    def accept_part(self, choice: PartChoice) -> int:
        """Record *choice* as accepted and return its part id. An existing row
        for the same (manufacturer, MPN) is updated in place, including one
        previously rejected."""
        return self._upsert(choice, ACCEPTED)

    def reject_part(self, choice: PartChoice) -> int:
        """Record *choice* as rejected and return its part id. A rejected part
        is excluded from :meth:`find_parts`."""
        return self._upsert(choice, REJECTED)

    def is_rejected(self, manufacturer: str, mpn: str) -> bool:
        """True when (manufacturer, MPN) is recorded with status rejected."""
        with self._lock:
            row = self.conn.execute(
                "SELECT status FROM part WHERE manufacturer = ? AND mpn = ?",
                ((manufacturer or "").strip(), (mpn or "").strip()),
            ).fetchone()
        return row is not None and row[0] == REJECTED

    def _upsert(self, choice: PartChoice, status: str) -> int:
        table = _table(choice.kind)
        mpn = (choice.mpn or "").strip()
        manufacturer = (choice.manufacturer or "").strip()
        if not mpn:
            raise ValueError(f"a {choice.kind} choice needs an MPN")
        stamp = self._now()
        p = choice.params
        digikey_pn = _opt_text(choice.digikey_pn)
        lcsc = _opt_text(choice.lcsc)
        jlc_library = _opt_text(choice.jlc_library)
        if jlc_library is not None:
            jlc_library = jlc_library.lower()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO part(mpn, manufacturer, type, package, description,"
                " datasheet_url, status, decided_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(manufacturer, mpn) DO UPDATE SET"
                " type = excluded.type, package = excluded.package,"
                " description = excluded.description,"
                " datasheet_url = excluded.datasheet_url,"
                " status = excluded.status, decided_at = excluded.decided_at",
                (mpn, manufacturer, choice.kind, (choice.package or "").strip(),
                 choice.description, choice.datasheet_url, status, stamp),
            )
            part_id = self.conn.execute(
                "SELECT id FROM part WHERE manufacturer = ? AND mpn = ?",
                (manufacturer, mpn),
            ).fetchone()[0]
            # A part whose type changed keeps no row in the other table.
            for other in _TABLES.values():
                if other.name != table.name:
                    self.conn.execute(
                        f"DELETE FROM {other.name} WHERE part_id = ?", (part_id,))
            cols = ["part_id", table.nominal, table.rating, "tolerance_pct"]
            values: list = [part_id, float(p.nominal),
                            None if p.rating is None else float(p.rating),
                            None if p.tolerance_pct is None else float(p.tolerance_pct)]
            if table.dielectric:
                cols.append(table.dielectric)
                values.append(_norm_dielectric_field(p.dielectric))
            self.conn.execute(
                f"INSERT OR REPLACE INTO {table.name}({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                values,
            )
            # A part has at most one PN per supplier: the rows for a supplier
            # named in the choice are replaced, so a changed PN leaves no stale
            # row behind. A supplier left as None or "" keeps its existing row.
            # A supplier PN identifies one part, so a PN recorded against
            # another part moves to this one (INSERT OR REPLACE on the key).
            if digikey_pn:
                self._replace_pn(part_id, "digikey", digikey_pn,
                                 _opt_text(choice.digikey_packaging), None, stamp)
            if lcsc:
                self._replace_pn(part_id, "lcsc", lcsc, None, jlc_library, stamp)
        return int(part_id)

    def _replace_pn(self, part_id: int, supplier: str, pn: str,
                    packaging: Optional[str], jlc_library: Optional[str],
                    stamp: str) -> None:
        self.conn.execute(
            "DELETE FROM supplier_pn WHERE part_id = ? AND supplier = ?",
            (part_id, supplier),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO supplier_pn"
            "(part_id, supplier, pn, packaging, jlc_library, verified_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
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
        0.1 % of *nominal*, the same *package*, a rating of at least
        *rating_min* (any rating when ``None``), a dielectric in *dielectrics*
        (capacitors; compared normalized; any dielectric when empty), and,
        when *tolerance_max_pct* is given, a stated tolerance no looser than
        it. Ordered by usage count (highest first), then JLCPCB Basic
        before Extended, then MPN.

        Each entry of *dielectrics* may itself list codes (``"C0G, NP0"``); a
        stored part matches when any of its codes is allowed."""
        table = _table(kind)
        allowed = {t for d in dielectrics for t in dielectric_tokens(d)}
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
            " (SELECT COUNT(*) FROM usage WHERE part_id = p.id) AS usage_count"
            f" FROM part p JOIN {table.name} c ON c.part_id = p.id"
            " WHERE p.status = ? AND p.package = ?"
            f" AND ABS(c.{table.nominal} - ?) <= ?"
        )
        args: list = [ACCEPTED, (package or "").strip(), float(nominal), band]
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
            rows = self.conn.execute(sql, args).fetchall()
        out: List[DbPart] = []
        for row in rows:
            (pid, mpn, mfr, pkg, desc, url, value, rating, diel, tol,
             dk, lcsc, lib, uses) = row
            # The dielectric filter runs here: a stored value can list two
            # codes, which an SQL IN test would compare as one string.
            if allowed and not allowed.intersection(dielectric_tokens(diel)):
                continue
            out.append(DbPart(
                kind=kind, part_id=int(pid), mpn=mpn, manufacturer=mfr, package=pkg,
                params=PartParams(value, rating, diel or "", tol),
                digikey_pn=dk, lcsc=lcsc, jlc_library=lib,
                usage_count=int(uses),
                description=desc or "", datasheet_url=url or "",
            ))
        return out

    # ----- usage -----------------------------------------------------------

    def record_usage(self, part_id: int, board: str,
                     references: Sequence[str]) -> None:
        """Record that *references* on *board* (schematic path relative to
        ``Boards/``) place part *part_id*. A pair already recorded has its
        ``used_at`` refreshed."""
        stamp = self._now()
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO usage(part_id, board, reference, used_at)"
                " VALUES (?, ?, ?, ?)",
                [(part_id, board, ref, stamp) for ref in references],
            )

    # ----- settings --------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        """The stored value of setting *key*, or ``None``."""
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Store *value* under setting *key*, replacing any earlier value."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )

    def settings(self) -> Dict[str, str]:
        """Every stored setting as ``{key: value}``."""
        with self._lock:
            return {k: v for k, v in self.conn.execute(
                "SELECT key, value FROM settings")}

    def close(self) -> None:
        """Close the connection; the object is unusable afterwards."""
        with self._lock:
            self.conn.close()
