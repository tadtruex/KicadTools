"""Backups of the parts database, and the offline copy (``DB_PLAN.md``
step 7).

:func:`pull` writes every table of an open :class:`~kicadtools.partsdb.PartsDb`
(the server) to a new SQLite file: the tables of :mod:`kicadtools.dbimport`
and, when the source has them, the inventory tables of
:mod:`kicadtools.inventorydb` (or of the first layout, unconverted, for a
database :mod:`kicadtools.inventorymigrate` has not converted yet), read
within one transaction, so the copy is one consistent state even while
another machine writes. The file is written under a ``.partial`` name and
renamed when complete, so a half-written file is never taken for a backup.

:func:`backup_now` writes ``~/.kicadtools/backups/parts-<date>-<time>.sqlite``
and keeps the newest :data:`KEEP`; the Cockpit runs it after each successful
start on the server. The newest backup is the offline copy
(:func:`kicadtools.dbconfig.newest_backup`).
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from kicadtools.dbconfig import backups_dir
from kicadtools.dbimport import ROTATIONS, TABLES
from kicadtools.inventorydb import INVENTORY_TABLES, InventoryDb, layout
from kicadtools.inventorymigrate import V1_TABLES, create_v1
from kicadtools.partsdb import PartsDb

KEEP = 10


def pull(source: PartsDb, dest: Path) -> Dict[str, int]:
    """Copy every table of *source* into a new SQLite file at *dest*
    (replaced if it exists). Returns the rows copied per table."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    partial.unlink(missing_ok=True)
    counts: Dict[str, int] = {}
    data = {}
    with source._lock:
        try:
            # One transaction: InnoDB gives every SELECT in it the same
            # snapshot (REPEATABLE READ), as SQLite does for one connection.
            first_layout = layout(source) == "1"
            tables = V1_TABLES if first_layout else INVENTORY_TABLES
            inventory = tuple(t for t in tables
                              if source._execute(source._dialect.has_table(), (t,)).fetchone())
            for table in TABLES + (ROTATIONS,) + inventory:
                cur = source._execute(f'SELECT * FROM "{table}"')
                data[table] = ([d[0] for d in cur.description], cur.fetchall())
        finally:
            source.conn.rollback()      # ends the read transaction
    for nested in ("location", "bin"):
        if nested in data:
            # A location refers to its parent: parents (fewer "/" in the
            # path) first, whatever their ids, since a moved location can
            # have a smaller id than its new parent.
            cols, rows = data[nested]
            at = cols.index("path")
            data[nested] = (cols, sorted(rows, key=lambda r: str(r[at]).count("/")))
    copy = PartsDb(partial)
    try:
        if first_layout:
            create_v1(copy, pictures="item_picture" in inventory)
        else:
            InventoryDb(copy)
        with copy._tx():
            for table in TABLES + (ROTATIONS,) + tables:
                cols, rows = data.get(table, ([], []))
                if rows:
                    copy._insert_rows(table, cols, rows)
                counts[table] = len(rows)
    finally:
        copy.close()
    os.replace(partial, dest)
    return counts


def backup_name(when: Optional[datetime] = None) -> str:
    """``parts-2026-11-23-140215.sqlite``."""
    return f"parts-{(when or datetime.now()):%Y-%m-%d-%H%M%S}.sqlite"


def prune(folder: Optional[Path] = None, keep: int = KEEP) -> None:
    """Delete all but the newest *keep* ``parts-*.sqlite`` backups (by
    modification time)."""
    folder = folder or backups_dir()
    files = sorted((f for f in folder.glob("parts-*.sqlite") if f.is_file()),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def backup_now(source: PartsDb, folder: Optional[Path] = None, keep: int = KEEP,
               when: Optional[datetime] = None) -> Path:
    """Write a dated backup of *source* to *folder* (``~/.kicadtools/backups``)
    and keep the newest *keep*. Returns the new file."""
    folder = folder or backups_dir()
    path = folder / backup_name(when)
    pull(source, path)
    prune(folder, keep)
    return path
