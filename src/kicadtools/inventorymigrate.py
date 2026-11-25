"""Converting the inventory's first layout to locations and stock rows
(``INVENTORY_PLAN.md``, "Decided 2026-11-24" and "Decided 2026-11-25").

The first layout (release ``kt_v0.1a``): ``bin`` (nested, each with an
optional ``site``, ``description``, a free-text ``location`` and ``notes``),
``item`` in at most one bin with one approximate ``quantity``, ``item_tag``,
and ``item_picture`` where a release had it.

:func:`migrate` turns it into the layout of :mod:`kicadtools.inventorydb`:

- each site becomes a top-level location, and each top-level bin moves
  inside its site's (``17`` with site ``school`` → ``school/17``); a
  top-level bin with no site stays at the top. Bins keep their ids, so a
  stock row's location is the old ``item.bin_id``. A bin's free-text
  ``location`` is added to its description (on the real data none has one);
- an item in a bin gets one stock row there with its quantity; an item in no
  bin gets one in the top-level location *unplaced* (``unknown``, decided
  2026-11-25) with its quantity;
- ``item.bin_id`` and ``item.quantity`` are dropped, then ``bin``, and
  ``meta.inventory_schema`` is set.

SQLite runs all of it in one transaction. MariaDB commits each ``CREATE``,
``ALTER`` and ``DROP`` on its own, so the steps are ordered so that a run
after a failure finds where the last one stopped: the new tables are filled
in one transaction before ``item`` loses its columns; until then a new run
drops them and starts again, and after it the stock rows are complete and
only ``bin`` and the marker are left to do.

:func:`plan` works out everything without writing (the dry run); the
conversion checks the counts against it at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kicadtools.inventorydb import (LAYOUT, SEP, InventoryDb, InventoryError, layout,
                                    path_key, schema, set_layout_marker)
from kicadtools.partsdb import PartsDb

UNPLACED = "unknown"

# The first layout's tables, parents first, as backups copy them.
V1_TABLES = ("bin", "item", "item_tag", "item_picture")

_V1_SQLITE = [
    """CREATE TABLE IF NOT EXISTS bin (
  id          INTEGER PRIMARY KEY,
  parent_id   INTEGER REFERENCES bin(id),
  name        TEXT NOT NULL,
  path        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  site        TEXT COLLATE NOCASE,
  description TEXT,
  location    TEXT,
  notes       TEXT,
  updated_at  TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS bin_parent ON bin(parent_id)",
    """CREATE TABLE IF NOT EXISTS item (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  description   TEXT,
  category      TEXT COLLATE NOCASE,
  manufacturer  TEXT COLLATE NOCASE,
  mpn           TEXT COLLATE NOCASE,
  digikey_pn    TEXT COLLATE NOCASE,
  lcsc          TEXT COLLATE NOCASE,
  quantity      INTEGER,
  bin_id        INTEGER REFERENCES bin(id),
  url           TEXT,
  notes         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS item_bin ON item(bin_id)",
    "CREATE INDEX IF NOT EXISTS item_mpn ON item(mpn)",
    """CREATE TABLE IF NOT EXISTS item_tag (
  item_id     INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  tag         TEXT NOT NULL COLLATE NOCASE,
  PRIMARY KEY (item_id, tag)
)""",
    "CREATE INDEX IF NOT EXISTS item_tag_tag ON item_tag(tag)",
]

_OPTS = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
_CI = "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
_V1_MARIA = [
    f"""CREATE TABLE IF NOT EXISTS bin (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  parent_id   INT,
  name        VARCHAR(100) NOT NULL,
  path        VARCHAR(255) {_CI} NOT NULL,
  site        VARCHAR(64) {_CI},
  description TEXT,
  location    TEXT,
  notes       TEXT,
  updated_at  VARCHAR(32) NOT NULL,
  UNIQUE KEY bin_path (path),
  KEY bin_parent (parent_id),
  FOREIGN KEY (parent_id) REFERENCES bin(id)
)""" + _OPTS,
    f"""CREATE TABLE IF NOT EXISTS item (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  name          VARCHAR(255) NOT NULL,
  description   TEXT,
  category      VARCHAR(64) {_CI},
  manufacturer  VARCHAR(128) {_CI},
  mpn           VARCHAR(128) {_CI},
  digikey_pn    VARCHAR(100) {_CI},
  lcsc          VARCHAR(32) {_CI},
  quantity      INT,
  bin_id        INT,
  url           TEXT,
  notes         TEXT,
  created_at    VARCHAR(32) NOT NULL,
  updated_at    VARCHAR(32) NOT NULL,
  KEY item_bin (bin_id),
  KEY item_mpn (mpn),
  FOREIGN KEY (bin_id) REFERENCES bin(id)
)""" + _OPTS,
    f"""CREATE TABLE IF NOT EXISTS item_tag (
  item_id     INT NOT NULL,
  tag         VARCHAR(64) {_CI} NOT NULL,
  PRIMARY KEY (item_id, tag),
  KEY item_tag_tag (tag),
  FOREIGN KEY (item_id) REFERENCES item(id) ON DELETE CASCADE
)""" + _OPTS,
]

# The first layout's item columns kept by the new item table.
_ITEM_KEPT = ("id", "name", "description", "category", "manufacturer", "mpn", "digikey_pn",
              "lcsc", "url", "notes", "created_at", "updated_at")


class MigrationError(RuntimeError):
    """The conversion was refused (nothing written) or found the result
    wrong."""


def create_v1(db: PartsDb, *, pictures: bool = True) -> None:
    """Create the first layout's tables in *db* (tests, and a backup or
    test copy of a database not converted yet). With *pictures*, also
    ``item_picture`` (a release that had it)."""
    statements = list(_V1_MARIA if db.backend == "mariadb" else _V1_SQLITE)
    if pictures:
        statements += [s for s in schema(db) if "item_picture" in s]
    with db._tx():
        for statement in statements:
            db._execute(statement)


@dataclass
class Plan:
    """What :func:`migrate` writes, worked out from the first layout's rows."""
    # (id, parent_id, name, path, description, notes, updated_at), parents first
    locations: List[Tuple[Any, ...]] = field(default_factory=list)
    # (item_id, location_id, quantity, notes, updated_at)
    stock: List[Tuple[Any, ...]] = field(default_factory=list)
    items: int = 0
    tops: List[str] = field(default_factory=list)     # top-level locations made
    unplaced: List[str] = field(default_factory=list)  # item names put in UNPLACED
    merged_text: List[str] = field(default_factory=list)  # bins whose `location` moved

    def summary(self) -> str:
        lines = [f"{len(self.locations)} locations ({', '.join(self.tops) or 'no new top level'}"
                 f" at the top), {self.items} items, {len(self.stock)} stock rows"]
        if self.unplaced:
            lines.append(f"{len(self.unplaced)} items in no bin go to {UNPLACED!r}: "
                         + ", ".join(self.unplaced))
        if self.merged_text:
            lines.append("free-text location added to the description of: "
                         + ", ".join(self.merged_text))
        return "\n".join(lines)


def _rows(db: PartsDb, sql: str) -> List[Dict[str, Any]]:
    cur = db._execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def plan(db: PartsDb, *, unplaced: str = UNPLACED) -> Plan:
    """What the conversion of *db* would write; :class:`MigrationError` when
    *db* is not in the first layout or its rows cannot be converted
    as described in the module docstring."""
    if layout(db) != "1":
        raise MigrationError(f"the inventory is not in the first layout (found {layout(db)!r})")
    now = db._now()
    bins = _rows(db, "SELECT id, parent_id, name, path, site, description, location, notes,"
                     " updated_at FROM bin")
    items = _rows(db, "SELECT id, name, quantity, bin_id, updated_at FROM item")
    by_id = {b["id"]: b for b in bins}

    def effective_site(b: Dict[str, Any]) -> Optional[str]:
        while b is not None:
            if b["site"]:
                return b["site"]
            b = by_id.get(b["parent_id"])
        return None

    # A bin inside another keeps its place; its own site must agree with the
    # site it is in, since the site becomes where its top-level bin is.
    for b in bins:
        if b["parent_id"] is not None and b["site"]:
            outer = effective_site(by_id[b["parent_id"]])
            if outer and outer.casefold() != b["site"].casefold():
                raise MigrationError(f"bin {b['path']!r} has site {b['site']!r} inside a bin at "
                                     f"{outer!r}; move it or clear its site first")

    out = Plan(items=len(items))
    next_id = max([b["id"] for b in bins] + [0]) + 1
    tops: Dict[str, Tuple[int, str]] = {}      # casefolded name -> (id, spelling)

    def top(name: str) -> int:
        key = name.casefold()
        if key not in tops:
            nonlocal next_id
            tops[key] = (next_id, name)
            out.locations.append((next_id, None, name, name, None, None, now))
            out.tops.append(name)
            next_id += 1
        return tops[key][0]

    for site in sorted({effective_site(b) for b in bins if b["parent_id"] is None} - {None},
                       key=str.casefold):
        top(site)
    unplaced_items = [i for i in items if i["bin_id"] is None]
    if unplaced_items:
        top(unplaced)

    # Bins, parents first; each top-level bin under its site's location.
    new_path: Dict[int, str] = {}
    staying_top = set()
    for b in sorted(bins, key=lambda b: path_key(b["path"])):
        if b["parent_id"] is None:
            site = effective_site(b)
            parent_id = tops[site.casefold()][0] if site else None
            path = f"{tops[site.casefold()][1]}{SEP}{b['name']}" if site else b["name"]
            if not site:
                staying_top.add(b["name"].casefold())
        else:
            parent_id = b["parent_id"]
            path = f"{new_path[b['parent_id']]}{SEP}{b['name']}"
        new_path[b["id"]] = path
        description = b["description"]
        if b["location"]:
            description = f"{description}\n\n{b['location']}" if description else b["location"]
            out.merged_text.append(b["path"])
        out.locations.append((b["id"], parent_id, b["name"], path, description, b["notes"],
                              b["updated_at"]))
    clash = staying_top & set(tops)
    if clash:
        raise MigrationError(f"a top-level bin with no site has the name of a top-level "
                             f"location to be made: {', '.join(sorted(clash))}")
    if len({p[3].casefold() for p in out.locations}) != len(out.locations):
        raise MigrationError("two locations would have the same path")

    for i in sorted(items, key=lambda i: i["id"]):
        if i["bin_id"] is None:
            out.stock.append((i["id"], tops[unplaced.casefold()][0], i["quantity"], None,
                              i["updated_at"]))
            out.unplaced.append(i["name"])
        else:
            out.stock.append((i["id"], i["bin_id"], i["quantity"], None, i["updated_at"]))
    return out


def _create(db: PartsDb, table: str) -> None:
    for statement in schema(db):
        if f"EXISTS {table} " in statement or f"ON {table}(" in statement:
            db._execute(statement)


def _fill(db: PartsDb, p: Plan) -> None:
    db._executemany("INSERT INTO location (id, parent_id, name, path, description, notes,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", p.locations)
    db._executemany("INSERT INTO stock (item_id, location_id, quantity, notes, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)", p.stock)


def _has_table(db: PartsDb, table: str) -> bool:
    return bool(db._execute(db._dialect.has_table(), (table,)).fetchone())


def _item_columns(db: PartsDb) -> List[str]:
    if db.backend == "mariadb":
        rows = db._execute("SELECT column_name FROM information_schema.columns"
                           " WHERE table_schema = DATABASE() AND table_name = 'item'")
        return [r[0] for r in rows.fetchall()]
    return [r[1] for r in db._execute('PRAGMA table_info("item")').fetchall()]


def _migrate_sqlite(db: PartsDb, p: Plan) -> None:
    """All of it in one transaction, ``item`` rebuilt (SQLite does not drop
    a column with a foreign key). Foreign keys are off while ``item`` is
    replaced, so dropping the old one does not cascade to its tags and
    pictures; they are checked before the commit."""
    conn = db.conn
    with db._lock:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            _create(db, "location")
            _create(db, "stock")
            _fill(db, p)
            item_sql = next(s for s in schema(db) if "EXISTS item (" in s)
            db._execute(item_sql.replace("EXISTS item (", "EXISTS item_new ("))
            cols = ", ".join(_ITEM_KEPT)
            db._execute(f"INSERT INTO item_new ({cols}) SELECT {cols} FROM item")
            db._execute("DROP TABLE item")
            db._execute("ALTER TABLE item_new RENAME TO item")
            db._execute("DROP TABLE bin")
            set_layout_marker(db)
            problems = db._execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise MigrationError(f"foreign keys broken: {problems[:5]}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _migrate_mariadb(db: PartsDb, p: Optional[Plan], *, _stop_after: Optional[str]) -> None:
    """Resumable steps (each DDL statement commits on its own)."""
    with db._lock:
        if "bin_id" in _item_columns(db):
            assert p is not None
            for table in ("stock", "location"):       # left by a run that stopped
                db._execute(f"DROP TABLE IF EXISTS {table}")
            _create(db, "location")
            _create(db, "stock")
            with db._tx():
                _fill(db, p)
            if _stop_after == "fill":
                return
            fks = db._execute(
                "SELECT constraint_name FROM information_schema.referential_constraints"
                " WHERE constraint_schema = DATABASE() AND table_name = 'item'"
                " AND referenced_table_name = 'bin'").fetchall()
            drops = [f"DROP FOREIGN KEY `{r[0]}`" for r in fks]
            drops += ["DROP COLUMN bin_id", "DROP COLUMN quantity"]
            db._execute(f"ALTER TABLE item {', '.join(drops)}")
            if _stop_after == "alter":
                return
        db._execute("DROP TABLE IF EXISTS bin")
        with db._tx():
            set_layout_marker(db)


def _counts(db: PartsDb) -> Tuple[int, int, int]:
    return tuple(db._execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("location", "item", "stock"))


def migrate(db: PartsDb, *, unplaced: str = UNPLACED, dry_run: bool = False,
            _stop_after: Optional[str] = None) -> Plan:
    """Convert the inventory of *db* to locations and stock rows. Returns
    the :class:`Plan` it carried out; with *dry_run* only works it out.

    A MariaDB conversion that stopped part way (``layout(db) ==
    "partial"``) is finished by calling this again. *_stop_after* (tests
    only) stops a MariaDB run after ``"fill"`` or ``"alter"``."""
    if db.read_only:
        raise MigrationError("the database is open read-only")
    found = layout(db)
    if found == LAYOUT:
        raise MigrationError("the inventory is already converted")
    if found is None:
        raise MigrationError("there is no inventory in this database")
    resuming = found == "partial"
    if resuming and db.backend != "mariadb":
        raise MigrationError("a SQLite conversion cannot stop part way; this database is "
                             "in an unexpected state")
    if resuming and "bin_id" in _item_columns(db):
        # Stopped before item lost its columns: the bins are all still there.
        for table in ("stock", "location"):
            db._execute(f"DROP TABLE IF EXISTS {table}")
        resuming = False
    p = None if resuming else plan(db, unplaced=unplaced)
    if dry_run:
        if p is None:
            raise MigrationError("a conversion stopped part way; run it without --dry-run "
                                 "to finish it")
        return p
    if db.backend == "mariadb":
        _migrate_mariadb(db, p, _stop_after=_stop_after)
        if _stop_after:
            return p
    else:
        _migrate_sqlite(db, p)
    InventoryDb(db)       # indexes and any table the layout has that is missing
    if p is not None:
        expect = (len(p.locations), p.items, len(p.stock))
        got = _counts(db)
        if got != expect:
            raise MigrationError(f"after the conversion: locations, items, stock = {got}, "
                                 f"expected {expect}")
    else:
        p = Plan(items=_counts(db)[1])
    return p


def copy_v1_rows(source: PartsDb, dest: PartsDb) -> Dict[str, int]:
    """Copy the first layout's inventory rows of *source* into *dest*, which
    holds the first layout's tables, empty (the test copy). Returns rows per
    table."""
    counts: Dict[str, int] = {}
    with dest._tx():
        for table in V1_TABLES:
            if not _has_table(source, table):
                counts[table] = 0
                continue
            cur = source._execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            rows: Sequence[Sequence[Any]] = cur.fetchall()
            if table == "bin":
                at = cols.index("path")
                rows = sorted(rows, key=lambda r: str(r[at]).count(SEP))
            if rows:
                dest._insert_rows(table, cols, rows)
            counts[table] = len(rows)
    return counts


__all__ = ["UNPLACED", "V1_TABLES", "MigrationError", "Plan", "copy_v1_rows", "create_v1",
           "migrate", "plan", "InventoryError"]
