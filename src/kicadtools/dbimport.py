"""The one-time import of a local parts database into the server
(``DB_PLAN.md`` step 5).

:func:`import_parts` copies every table of a SQLite parts file, and the CPL
rotation overrides, into an empty :class:`~kicadtools.partsdb.PartsDb` (the
MariaDB server, or another SQLite file in tests), in one transaction:

- part ids are kept, so ``usage``, ``supplier_pn`` and the parameter tables
  still point at their parts (MariaDB continues ``AUTO_INCREMENT`` above the
  largest id);
- the destination must hold no parts, settings, usage, supplier numbers or
  rotations: the import is the start of the server's data, not a merge;
- after the copy every table is read back and compared row by row with the
  source; a difference rolls the transaction back;
- with *dry_run* the same is done and then rolled back, so a value the
  server's columns refuse (strict mode) is found without writing.

The source is opened read-only and is never changed; renaming it after the
import is the caller's (``kicadtools db import``).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from kicadtools.partsdb import SCHEMA_VERSION, PartsDb

# Copied in this order (parents before the rows that refer to them).
TABLES = ("settings", "part", "capacitor", "resistor", "inductor", "ic", "tht", "led",
          "supplier_pn", "usage")
ROTATIONS = "part_rotation"


class DbImportError(RuntimeError):
    """The import was refused or failed; nothing was written."""


@dataclass
class ImportReport:
    """Rows per table in the source and in the destination after the copy
    (before any rollback)."""

    source: Dict[str, int] = field(default_factory=dict)
    dest: Dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    def lines(self) -> List[str]:
        out = [f"{'table':<14}{'local':>8}{'server':>8}"]
        for table in TABLES + (ROTATIONS,):
            out.append(f"{table:<14}{self.source.get(table, 0):>8}{self.dest.get(table, 0):>8}")
        return out


def _read(path: Path) -> Tuple[Dict[str, Tuple[List[str], List[tuple]]], str]:
    """``({table: (columns, rows)}, schema_version)`` of the SQLite file at
    *path*, opened read-only."""
    if not path.exists():
        raise DbImportError(f"{path}: no such file")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version_row = (conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
                       .fetchone() if "meta" in names else None)
        out = {}
        for table in TABLES:
            if table not in names:
                out[table] = ([], [])
                continue
            cur = conn.execute(f'SELECT * FROM "{table}"')
            out[table] = ([d[0] for d in cur.description], cur.fetchall())
        return out, (version_row[0] if version_row else "")
    finally:
        conn.close()


def _rows(db: PartsDb, table: str, cols: Sequence[str]) -> List[tuple]:
    quoted = ", ".join(f'"{c}"' for c in cols)
    return db._execute(f'SELECT {quoted} FROM "{table}"').fetchall()


def _key(row: tuple) -> tuple:
    return tuple((v is None, "" if v is None else str(v)) for v in row)


def import_parts(source: Path, dest: PartsDb, rotations: Mapping[str, float] = (),
                 *, dry_run: bool = False) -> ImportReport:
    """Copy the SQLite parts file *source* and *rotations* (``{lcsc:
    degrees}``) into the empty *dest*. Returns the row counts. Raises
    :class:`DbImportError` when *source* is missing or of another schema
    version, *dest* already holds data, or the read-back differs; nothing is
    written then."""
    data, version = _read(Path(source))
    if version != str(SCHEMA_VERSION):
        raise DbImportError(
            f"{source}: schema version {version or 'none'}; open it once with this "
            f"KicadTools (version {SCHEMA_VERSION}) before importing")
    rotation_rows = sorted((str(k), float(v) % 360.0) for k, v in dict(rotations).items()
                           if str(k) and float(v) % 360.0)
    report = ImportReport(dry_run=dry_run)
    report.source = {t: len(rows) for t, (_, rows) in data.items()}
    report.source[ROTATIONS] = len(rotation_rows)

    with dest._lock:
        held = {t: dest._execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in TABLES + (ROTATIONS,)}
        if any(held.values()):
            listed = ", ".join(f"{t} {n}" for t, n in held.items() if n)
            raise DbImportError(f"{dest.label} already holds data ({listed}); "
                               "the import only fills an empty database")
        try:
            copy = dict(data)
            copy[ROTATIONS] = (["lcsc", "degrees"], rotation_rows)
            for table in TABLES + (ROTATIONS,):
                cols, rows = copy[table]
                if rows:
                    dest._insert_rows(table, cols, rows)
            for table in TABLES + (ROTATIONS,):
                cols, rows = copy[table]
                back = _rows(dest, table, cols) if cols else []
                report.dest[table] = len(back)
                if sorted(map(_key, back)) != sorted(map(_key, rows)):
                    raise DbImportError(f"{table}: the rows read back differ from the source")
        except Exception:
            dest.conn.rollback()
            raise
        if dry_run:
            dest.conn.rollback()
        else:
            dest.conn.commit()
    return report


def imported_name(source: Path) -> Path:
    """Where the source is renamed after an import: ``parts.sqlite.imported``,
    or ``parts.sqlite.imported-2`` and so on when that exists."""
    target = source.with_name(source.name + ".imported")
    n = 2
    while target.exists():
        target = source.with_name(f"{source.name}.imported-{n}")
        n += 1
    return target
