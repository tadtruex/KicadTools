"""The lab inventory: what is on the shelves, where, about how many
(``INVENTORY_PLAN.md``).

The tables are in the parts database, beside the parts tables, and
:class:`InventoryDb` works on an open :class:`~kicadtools.partsdb.PartsDb`
(its connection, lock, transactions and engine). They are separate from
``part``: ``part`` holds design decisions, these tables hold what is on hand.

- ``location`` — a place things are kept: a lab (``school``), a cabinet
  (``school/C12``), a drawer (``school/C12/5``), a box. Locations nest to any
  depth. ``name`` is the location's own name (no ``/``); ``path`` is the
  names from the top joined with ``/``, unique and compared ignoring case,
  so a URL or a QR code finds a location with one lookup. The top level is
  where a thing is (the lab); there is no separate site. ``description``
  (what it is and where to find it) and ``notes`` are free text.
- ``item`` — a kind of thing: a name (the only required field) and optional
  category, manufacturer, MPN, DigiKey PN, LCSC number, URL, description and
  notes. It has no location and no quantity: its stock rows do.
- ``stock`` — some of an item in one location, with its own approximate
  quantity (``None``: unknown) and notes. One row per item and location; an
  item can be in any number of locations, or none. A row at 0 is kept: it
  records where the item is kept when there is some.
- ``item_tag`` — free words on an item (``arduino``, ``avr``), one row per
  tag, unique per item ignoring case.
- ``item_picture`` — any number of pictures of an item, in order (the first
  is the one a list shows). Each is a JPEG and its thumbnail, kept in the
  database so backups and ``db pull`` carry them; :mod:`kicadtools.pictures`
  makes them from an upload.

Nothing is a fixed list: categories, manufacturers and tags take any value,
and :meth:`InventoryDb.values` and :meth:`InventoryDb.tags` give the ones
used so far, for a box that offers them as you type.

The tables are created with ``CREATE TABLE IF NOT EXISTS`` when an
:class:`InventoryDb` is opened on a database without an inventory, within
parts schema version 2, and ``meta.inventory_schema`` records this layout.
A database holding the first layout (bins with a site, an item in one bin
with one quantity) is refused with :class:`InventoryLayoutError` until
``kicadtools inventory migrate`` (:mod:`kicadtools.inventorymigrate`) has
converted it.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from kicadtools.partsdb import PartsDb

# Copied in this order by backups (rows before the rows that refer to them).
INVENTORY_TABLES = ("location", "item", "item_tag", "item_picture", "stock")

# meta key and value marking this layout (the first layout has no row).
LAYOUT_KEY = "inventory_schema"
LAYOUT = "2"

SEP = "/"

_SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS location (
  id          INTEGER PRIMARY KEY,
  parent_id   INTEGER REFERENCES location(id),
  name        TEXT NOT NULL,
  path        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  description TEXT,
  notes       TEXT,
  updated_at  TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS location_parent ON location(parent_id)",
    """CREATE TABLE IF NOT EXISTS item (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  description   TEXT,
  category      TEXT COLLATE NOCASE,
  manufacturer  TEXT COLLATE NOCASE,
  mpn           TEXT COLLATE NOCASE,
  digikey_pn    TEXT COLLATE NOCASE,
  lcsc          TEXT COLLATE NOCASE,
  url           TEXT,
  notes         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS item_mpn ON item(mpn)",
    """CREATE TABLE IF NOT EXISTS item_tag (
  item_id     INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  tag         TEXT NOT NULL COLLATE NOCASE,
  PRIMARY KEY (item_id, tag)
)""",
    "CREATE INDEX IF NOT EXISTS item_tag_tag ON item_tag(tag)",
    """CREATE TABLE IF NOT EXISTS item_picture (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,
  content_type  TEXT NOT NULL,
  width         INTEGER NOT NULL,
  height        INTEGER NOT NULL,
  data          BLOB NOT NULL,
  thumb         BLOB NOT NULL,
  caption       TEXT,
  created_at    TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS item_picture_item ON item_picture(item_id, position)",
    """CREATE TABLE IF NOT EXISTS stock (
  id           INTEGER PRIMARY KEY,
  item_id      INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  location_id  INTEGER NOT NULL REFERENCES location(id),
  quantity     INTEGER,
  notes        TEXT,
  updated_at   TEXT NOT NULL,
  UNIQUE (item_id, location_id)
)""",
    "CREATE INDEX IF NOT EXISTS stock_location ON stock(location_id)",
]

# MariaDB: the same tables. Text compared ignoring case uses utf8mb4_unicode_ci
# (as partsdb's mpn and manufacturer do); the rest of a table compares bytes.
_OPTS = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
_CI = "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
_MARIA_SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS location (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  parent_id   INT,
  name        VARCHAR(100) NOT NULL,
  path        VARCHAR(255) {_CI} NOT NULL,
  description TEXT,
  notes       TEXT,
  updated_at  VARCHAR(32) NOT NULL,
  UNIQUE KEY location_path (path),
  KEY location_parent (parent_id),
  FOREIGN KEY (parent_id) REFERENCES location(id)
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
  url           TEXT,
  notes         TEXT,
  created_at    VARCHAR(32) NOT NULL,
  updated_at    VARCHAR(32) NOT NULL,
  KEY item_mpn (mpn)
)""" + _OPTS,
    f"""CREATE TABLE IF NOT EXISTS item_tag (
  item_id     INT NOT NULL,
  tag         VARCHAR(64) {_CI} NOT NULL,
  PRIMARY KEY (item_id, tag),
  KEY item_tag_tag (tag),
  FOREIGN KEY (item_id) REFERENCES item(id) ON DELETE CASCADE
)""" + _OPTS,
    # MEDIUMBLOB holds 16 MB; a stored picture is a few hundred kB.
    """CREATE TABLE IF NOT EXISTS item_picture (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  item_id       INT NOT NULL,
  position      INT NOT NULL,
  content_type  VARCHAR(64) NOT NULL,
  width         INT NOT NULL,
  height        INT NOT NULL,
  data          MEDIUMBLOB NOT NULL,
  thumb         MEDIUMBLOB NOT NULL,
  caption       TEXT,
  created_at    VARCHAR(32) NOT NULL,
  KEY item_picture_item (item_id, position),
  FOREIGN KEY (item_id) REFERENCES item(id) ON DELETE CASCADE
)""" + _OPTS,
    """CREATE TABLE IF NOT EXISTS stock (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  item_id      INT NOT NULL,
  location_id  INT NOT NULL,
  quantity     INT,
  notes        TEXT,
  updated_at   VARCHAR(32) NOT NULL,
  UNIQUE KEY stock_item_location (item_id, location_id),
  KEY stock_location (location_id),
  FOREIGN KEY (item_id) REFERENCES item(id) ON DELETE CASCADE,
  FOREIGN KEY (location_id) REFERENCES location(id)
)""" + _OPTS,
]

# The item fields set by add_item / update_item besides name and tags. All
# optional text: "" or None clears one.
_TEXT_FIELDS = ("description", "category", "manufacturer", "mpn", "digikey_pn", "lcsc",
                "url", "notes")
_ITEM_COLS = ("id", "name", "description", "category", "manufacturer", "mpn",
              "digikey_pn", "lcsc", "url", "notes", "created_at", "updated_at")
_LOCATION_COLS = ("id", "parent_id", "name", "path", "description", "notes", "updated_at")
_STOCK_SELECT = ("SELECT stock.id, stock.item_id, stock.location_id, location.path,"
                 " stock.quantity, stock.notes, stock.updated_at"
                 " FROM stock JOIN location ON location.id = stock.location_id")
_PICTURE_COLS = ("id", "item_id", "position", "content_type", "width", "height",
                 "caption", "created_at")

# Columns added after a table was first created: (table, column, SQLite type,
# MariaDB type). CREATE TABLE IF NOT EXISTS does not add a column to a table
# that exists, so InventoryDb adds each one that is missing when it opens.
_ADDED_COLUMNS: Tuple[Tuple[str, str, str, str], ...] = ()

# Free-text fields: only surrounding whitespace is removed, so line breaks
# in a description are kept. Other text is cleaned of repeated spaces too.
_FREE_TEXT = ("description", "notes", "url", "caption")

# Fields whose used values values() lists (item columns).
_VALUE_FIELDS = ("category", "manufacturer")

# Lengths of the MariaDB columns, checked on both engines so a value SQLite
# would take is not refused later by the server.
_MAX = {"location.name": 100, "location.path": 255, "name": 255, "category": 64,
        "manufacturer": 128, "mpn": 128, "digikey_pn": 100, "lcsc": 32, "tag": 64}

_UNSET: Any = object()


class InventoryError(ValueError):
    """A refused inventory change: a bad name, a location that does not
    exist or already exists, a non-empty location deleted, a location moved
    inside itself."""


class InventoryLayoutError(RuntimeError):
    """The database holds the inventory's first layout (bins); it has to be
    converted with ``kicadtools inventory migrate`` first."""


@dataclass(frozen=True)
class Location:
    id: int
    parent_id: Optional[int]
    name: str
    path: str
    description: Optional[str]
    notes: Optional[str]
    updated_at: str

    @property
    def depth(self) -> int:
        return self.path.count(SEP)

    @property
    def top(self) -> str:
        """The top-level location this one is in (itself at the top)."""
        return self.path.split(SEP)[0]


@dataclass(frozen=True)
class Stock:
    """Some of an item in one location."""
    id: int
    item_id: int
    location_id: int
    location_path: str
    quantity: Optional[int]     # approximate; None: unknown
    notes: Optional[str]
    updated_at: str


@dataclass(frozen=True)
class Item:
    id: int
    name: str
    description: Optional[str]
    category: Optional[str]
    manufacturer: Optional[str]
    mpn: Optional[str]
    digikey_pn: Optional[str]
    lcsc: Optional[str]
    url: Optional[str]
    notes: Optional[str]
    created_at: str
    updated_at: str
    tags: Tuple[str, ...] = ()
    pictures: Tuple[int, ...] = ()   # picture ids in order; the first is the main one
    stock: Tuple[Stock, ...] = ()    # by location path

    @property
    def picture(self) -> Optional[int]:
        """The main picture's id, ``None`` when the item has none."""
        return self.pictures[0] if self.pictures else None

    @property
    def on_hand(self) -> Optional[int]:
        """The sum of the known quantities of its stock rows; ``None`` when
        no row has a known quantity (or there is no row)."""
        known = [s.quantity for s in self.stock if s.quantity is not None]
        return sum(known) if known else None

    @property
    def locations(self) -> Tuple[str, ...]:
        return tuple(s.location_path for s in self.stock)

    def stock_in(self, path: str) -> Tuple[Stock, ...]:
        """Its stock rows in the location *path* or any location inside it."""
        return tuple(s for s in self.stock if in_location(s.location_path, path))


@dataclass(frozen=True)
class Picture:
    """A stored picture's details; the image itself is
    :meth:`InventoryDb.picture_data`."""
    id: int
    item_id: int
    position: int               # 0 is the main picture
    content_type: str
    width: int
    height: int
    caption: Optional[str]
    created_at: str


def natural_key(text: str) -> tuple:
    """Sort key putting ``"2"`` before ``"10"`` and ``"C2"`` before
    ``"C10"``, ignoring case."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                 for p in re.split(r"(\d+)", text.casefold()) if p)


def path_key(path: Optional[str]) -> list:
    """Sort key for a location path: by level, each level naturally."""
    return [natural_key(n) for n in (path or "").split(SEP)]


def in_location(path: Optional[str], within: str) -> bool:
    """Whether the location *path* is *within* or inside it (ignoring
    case)."""
    if not path:
        return False
    a, b = path.casefold(), within.casefold()
    return a == b or a.startswith(b + SEP)


def _clean(text: Optional[str]) -> Optional[str]:
    """*text* with surrounding and repeated whitespace removed; ``None`` for
    ``None`` or blank."""
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text or None


def _clean_free(text: Optional[str]) -> Optional[str]:
    """Free text with surrounding whitespace removed and line breaks kept;
    ``None`` for ``None`` or blank."""
    if text is None:
        return None
    text = str(text).strip()
    return text or None


def _clean_field(field: str, text: Optional[str]) -> Optional[str]:
    return _clean_free(text) if field in _FREE_TEXT else _clean(text)


def _check_len(field: str, value: Optional[str]) -> None:
    limit = _MAX.get(field)
    if value is not None and limit is not None and len(value) > limit:
        raise InventoryError(f"{field} is longer than {limit} characters: {value!r}")


def location_name(name: str) -> str:
    """*name* cleaned for use as one location's name; refuses blank names
    and names holding ``/``."""
    cleaned = _clean(name)
    if cleaned is None:
        raise InventoryError("a location needs a name")
    if SEP in cleaned:
        raise InventoryError(f"a location name cannot hold {SEP!r}: {cleaned!r}")
    _check_len("location.name", cleaned)
    return cleaned


def split_path(path: str) -> List[str]:
    """The names in a location path, cleaned: ``" school / C12 "`` →
    ``["school", "C12"]``."""
    parts = [p for p in (path or "").split(SEP)]
    if not any(p.strip() for p in parts):
        raise InventoryError("an empty location path")
    return [location_name(p) for p in parts]


def norm_path(path: str) -> str:
    """*path* with each name cleaned: ``" school / C12 "`` → ``"school/C12"``."""
    return SEP.join(split_path(path))


def _norm_tags(tags: Iterable[str]) -> List[str]:
    """Tags cleaned, blank ones dropped, the first spelling of each kept when
    two differ only in case."""
    out: List[str] = []
    seen = set()
    for tag in tags:
        tag = _clean(tag)
        if tag is None:
            continue
        _check_len("tag", tag)
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            out.append(tag)
    return out


def _like_prefix(path: str) -> str:
    """A LIKE pattern for the paths below *path* (``!`` escapes)."""
    escaped = path.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return escaped + SEP + "%"


def _quantity(quantity: Optional[int]) -> Optional[int]:
    if quantity is None:
        return None
    quantity = int(quantity)
    if quantity < 0:
        raise InventoryError(f"a quantity cannot be negative: {quantity}")
    return quantity


def add_quantities(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """Two approximate quantities in one: unknown only when both are."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def _join_notes(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a and b and a != b:
        return f"{a}\n{b}"
    return a or b


def layout(db: PartsDb) -> Optional[str]:
    """Which inventory layout *db* holds: ``"2"`` (this module's), ``"1"``
    (bins: not converted yet), ``"partial"`` (a conversion that stopped
    part way), or ``None`` (no inventory tables)."""
    def has(table: str) -> bool:
        return bool(db._execute(db._dialect.has_table(), (table,)).fetchone())

    marker = None
    if has("meta"):
        row = db._execute('SELECT value FROM meta WHERE "key" = ?', (LAYOUT_KEY,)).fetchone()
        marker = row[0] if row else None
    if marker == LAYOUT:
        return LAYOUT
    if has("bin"):
        return "1" if not has("location") else "partial"
    if has("location"):
        return "partial"
    if has("item"):
        return "1"
    return None


def set_layout_marker(db: PartsDb) -> None:
    """Record this layout in ``meta``, in the caller's transaction."""
    db._execute('DELETE FROM meta WHERE "key" = ?', (LAYOUT_KEY,))
    db._execute('INSERT INTO meta ("key", value) VALUES (?, ?)', (LAYOUT_KEY, LAYOUT))


def schema(db: PartsDb) -> List[str]:
    """This layout's ``CREATE … IF NOT EXISTS`` statements for *db*'s engine."""
    return list(_MARIA_SCHEMA if db.backend == "mariadb" else _SQLITE_SCHEMA)


class InventoryDb:
    """The inventory tables of the open parts database *db*; creates them
    when there is no inventory yet. Raises :class:`InventoryLayoutError` on
    a database holding the first layout."""

    def __init__(self, db: PartsDb) -> None:
        self.db = db
        self._depth = 0
        found = layout(db)
        if found in ("1", "partial"):
            what = ("the first inventory layout (bins)" if found == "1"
                    else "an inventory conversion that stopped part way")
            raise InventoryLayoutError(
                f"this database holds {what}; convert it with "
                "`kicadtools inventory migrate` (INVENTORY_PLAN.md)")
        if not db.read_only:
            with self._tx():
                for statement in schema(db):
                    db._execute(statement)
                self._add_missing_columns()
                if found is None:
                    set_layout_marker(db)
        else:
            db.conn.commit()        # end the read transaction layout() began

    def _columns(self, table: str) -> List[str]:
        if self.db.backend == "mariadb":
            rows = self._x("SELECT column_name FROM information_schema.columns"
                           " WHERE table_schema = DATABASE() AND table_name = ?", (table,))
            return [r[0] for r in rows.fetchall()]
        return [r[1] for r in self._x(f'PRAGMA table_info("{table}")').fetchall()]

    def _add_missing_columns(self) -> None:
        """Add each column of :data:`_ADDED_COLUMNS` its table lacks (a table
        created before the column was defined). Existing rows get NULL."""
        mariadb = self.db.backend == "mariadb"
        for table, column, sqlite_type, maria_type in _ADDED_COLUMNS:
            if column not in self._columns(table):
                self._x(f'ALTER TABLE {table} ADD COLUMN {column} '
                        f'{maria_type if mariadb else sqlite_type}')

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """One transaction, holding the database's lock. A block inside
        another joins it: only the outermost commits, and a failure anywhere
        rolls the whole of it back (``PartsDb._tx`` commits at the end of
        every block, so the methods here, which call one another, do not use
        it directly)."""
        with self.db._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            try:
                with self.db._tx():
                    yield
            finally:
                self._depth = 0

    @contextmanager
    def _read(self) -> Iterator[None]:
        """Reads holding the database's lock. Outside a transaction of this
        class the read transaction is ended afterwards: with MariaDB
        (REPEATABLE READ, no autocommit) a connection left inside one keeps
        reading the snapshot its first read took, so a long-lived connection
        (the web app) would not see what other connections write, and its
        open transaction holds metadata locks that make a DROP or ALTER from
        another connection wait."""
        with self.db._lock:
            try:
                yield
            finally:
                if not self._depth:
                    # Ends the snapshot. commit, not rollback: anything another
                    # user of the connection left uncommitted is kept.
                    self.db.conn.commit()

    def _x(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.db._execute(sql, params)

    def _new_id(self, cur: Any) -> int:
        return int(cur.lastrowid)

    # ----- locations -------------------------------------------------------

    def _locations_where(self, where: str, params: Sequence[Any]) -> List[Location]:
        rows = self._x(f"SELECT {', '.join(_LOCATION_COLS)} FROM location WHERE {where}",
                       params)
        return [Location(*r) for r in rows.fetchall()]

    def location(self, path: str) -> Optional[Location]:
        """The location at *path* (case ignored), ``None`` when there is none."""
        with self._read():
            found = self._locations_where("path = ?", (norm_path(path),))
        return found[0] if found else None

    def location_by_id(self, location_id: int) -> Optional[Location]:
        with self._read():
            found = self._locations_where("id = ?", (int(location_id),))
        return found[0] if found else None

    def _need_location(self, path: str) -> Location:
        found = self.location(path)
        if found is None:
            raise InventoryError(f"no location {path!r}")
        return found

    def locations(self) -> List[Location]:
        """Every location, parents before children, siblings in natural order."""
        with self._read():
            found = self._locations_where("1 = 1", ())
        return sorted(found, key=lambda loc: path_key(loc.path))

    def children(self, path: Optional[str] = None) -> List[Location]:
        """The locations directly inside *path*; the top level for ``None``."""
        with self._read():
            if path is None:
                found = self._locations_where("parent_id IS NULL", ())
            else:
                found = self._locations_where("parent_id = ?", (self._need_location(path).id,))
        return sorted(found, key=lambda loc: natural_key(loc.name))

    def subtree(self, path: str) -> List[Location]:
        """The location at *path* and every location inside it, at any
        depth, parents first."""
        root = self._need_location(path)
        with self._read():
            found = self._locations_where("path = ? OR path LIKE ? ESCAPE '!'",
                                          (root.path, _like_prefix(root.path)))
        return sorted(found, key=lambda loc: path_key(loc.path))

    def add_location(self, name: str, parent: Optional[str] = None, *,
                     description: Optional[str] = None,
                     notes: Optional[str] = None) -> Location:
        """A new location *name* inside *parent* (top level for ``None``)."""
        name = location_name(name)
        with self._tx():
            parent_loc = self._need_location(parent) if parent is not None else None
            path = f"{parent_loc.path}{SEP}{name}" if parent_loc else name
            _check_len("location.path", path)
            if self.location(path) is not None:
                raise InventoryError(f"location {path!r} already exists")
            cur = self._x(
                "INSERT INTO location (parent_id, name, path, description, notes, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (parent_loc.id if parent_loc else None, name, path,
                 _clean_free(description), _clean_free(notes), self.db._now()))
            new_id = self._new_id(cur)
        return self.location_by_id(new_id)

    def ensure_location(self, path: str) -> Location:
        """The location at *path*, made with any missing locations above it."""
        names = split_path(path)
        with self._tx():
            parent: Optional[str] = None
            for depth, name in enumerate(names):
                here = SEP.join(names[:depth + 1])
                if self.location(here) is None:
                    self.add_location(name, parent)
                parent = here
        return self._need_location(path)

    def update_location(self, path: str, *, description: Any = _UNSET,
                        notes: Any = _UNSET) -> Location:
        """Set the location's description or notes; a field not given is
        kept, and ``None`` or ``""`` clears one."""
        changes: Dict[str, Optional[str]] = {}
        for field, value in (("description", description), ("notes", notes)):
            if value is not _UNSET:
                changes[field] = _clean_free(value)
        with self._tx():
            target = self._need_location(path)
            if changes:
                sets = ", ".join(f"{f} = ?" for f in changes)
                self._x(f"UPDATE location SET {sets}, updated_at = ? WHERE id = ?",
                        (*changes.values(), self.db._now(), target.id))
        return self.location_by_id(target.id)

    def _repath(self, target: Location, new_parent: Optional[Location],
                new_name: str) -> Location:
        """Give *target* the name *new_name* inside *new_parent* and rewrite
        the paths of every location inside it, in the caller's transaction."""
        new_path = f"{new_parent.path}{SEP}{new_name}" if new_parent else new_name
        _check_len("location.path", new_path)
        clash = self.location(new_path)
        if clash is not None and clash.id != target.id:
            raise InventoryError(f"location {new_path!r} already exists")
        below = [loc for loc in self.subtree(target.path) if loc.id != target.id]
        stamp = self.db._now()
        self._x("UPDATE location SET parent_id = ?, name = ?, path = ?, updated_at = ?"
                " WHERE id = ?",
                (new_parent.id if new_parent else None, new_name, new_path, stamp, target.id))
        for loc in below:
            moved = new_path + loc.path[len(target.path):]
            _check_len("location.path", moved)
            self._x("UPDATE location SET path = ?, updated_at = ? WHERE id = ?",
                    (moved, stamp, loc.id))
        return self.location_by_id(target.id)

    def rename_location(self, path: str, new_name: str) -> Location:
        """Rename the location at *path*; the locations inside it follow."""
        new_name = location_name(new_name)
        with self._tx():
            target = self._need_location(path)
            parent = self.location_by_id(target.parent_id) if target.parent_id else None
            return self._repath(target, parent, new_name)

    def move_location(self, path: str, new_parent: Optional[str]) -> Location:
        """Move the location at *path* (and everything in it) inside
        *new_parent*, or to the top level for ``None``."""
        with self._tx():
            target = self._need_location(path)
            parent = self._need_location(new_parent) if new_parent is not None else None
            if parent is not None and (parent.id == target.id
                                       or in_location(parent.path, target.path)):
                raise InventoryError(f"cannot move {target.path!r} inside itself")
            return self._repath(target, parent, target.name)

    def delete_location(self, path: str) -> None:
        """Delete the location at *path*; refused while it holds stock or
        other locations."""
        with self._tx():
            target = self._need_location(path)
            if self._x("SELECT 1 FROM location WHERE parent_id = ?", (target.id,)).fetchone():
                raise InventoryError(f"location {target.path!r} holds other locations")
            if self._x("SELECT 1 FROM stock WHERE location_id = ?", (target.id,)).fetchone():
                raise InventoryError(f"location {target.path!r} holds items")
            self._x("DELETE FROM location WHERE id = ?", (target.id,))

    # ----- items -----------------------------------------------------------

    def _items_where(self, where: str, params: Sequence[Any]) -> List[Item]:
        cols = ", ".join(f"item.{c}" for c in _ITEM_COLS)
        rows = self._x(f"SELECT {cols} FROM item WHERE {where}", params).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        marks = ", ".join("?" * len(ids))
        tags: Dict[int, List[str]] = {i: [] for i in ids}
        for item_id, tag in self._x(
                f"SELECT item_id, tag FROM item_tag WHERE item_id IN ({marks})", ids):
            tags[item_id].append(tag)
        pictures: Dict[int, List[int]] = {i: [] for i in ids}
        for item_id, picture_id in self._x(
                f"SELECT item_id, id FROM item_picture WHERE item_id IN ({marks})"
                " ORDER BY item_id, position", ids):
            pictures[item_id].append(picture_id)
        stock: Dict[int, List[Stock]] = {i: [] for i in ids}
        for r in self._x(f"{_STOCK_SELECT} WHERE stock.item_id IN ({marks})", ids):
            stock[r[1]].append(Stock(*r))
        out = []
        for r in rows:
            values = dict(zip(_ITEM_COLS, r))
            i = values["id"]
            out.append(Item(tags=tuple(sorted(tags[i], key=natural_key)),
                            pictures=tuple(pictures[i]),
                            stock=tuple(sorted(stock[i], key=lambda s: path_key(s.location_path))),
                            **values))
        return out

    def item(self, item_id: int) -> Optional[Item]:
        with self._read():
            found = self._items_where("item.id = ?", (int(item_id),))
        return found[0] if found else None

    def _need_item(self, item_id: int) -> Item:
        found = self.item(item_id)
        if found is None:
            raise InventoryError(f"no item {item_id}")
        return found

    def items(self, location: Optional[str] = None, *, recursive: bool = True) -> List[Item]:
        """Every item, by name; with *location*, the items with stock in that
        location and, when *recursive*, in any location inside it."""
        with self._read():
            if location is None:
                found = self._items_where("1 = 1", ())
            else:
                root = self._need_location(location)
                if recursive:
                    found = self._items_where(
                        "item.id IN (SELECT stock.item_id FROM stock JOIN location"
                        " ON location.id = stock.location_id"
                        " WHERE location.path = ? OR location.path LIKE ? ESCAPE '!')",
                        (root.path, _like_prefix(root.path)))
                else:
                    found = self._items_where(
                        "item.id IN (SELECT item_id FROM stock WHERE location_id = ?)",
                        (root.id,))
        return sorted(found, key=lambda i: natural_key(i.name))

    def unplaced(self) -> List[Item]:
        """The items with no stock row: known, but in no location."""
        with self._read():
            found = self._items_where("item.id NOT IN (SELECT item_id FROM stock)", ())
        return sorted(found, key=lambda i: natural_key(i.name))

    def matching(self, *, name: Optional[str] = None, mpn: Optional[str] = None,
                 digikey_pn: Optional[str] = None) -> List[Item]:
        """The items with the same name, MPN or DigiKey PN as given (ignoring
        case and spacing): what quick add offers before making a second item
        for the same thing."""
        wanted = [(f, _clean(v)) for f, v in (("name", name), ("mpn", mpn),
                                              ("digikey_pn", digikey_pn))]
        wanted = [(f, v.casefold()) for f, v in wanted if v]
        if not wanted:
            return []
        return [i for i in self.items()
                if any(getattr(i, f) and " ".join(getattr(i, f).split()).casefold() == v
                       for f, v in wanted)]

    def _set_tags(self, item_id: int, tags: Iterable[str]) -> None:
        self._x("DELETE FROM item_tag WHERE item_id = ?", (item_id,))
        for tag in _norm_tags(tags):
            self._x("INSERT INTO item_tag (item_id, tag) VALUES (?, ?)", (item_id, tag))

    def add_item(self, name: str, *, location: Optional[str] = None,
                 quantity: Optional[int] = None, tags: Iterable[str] = (),
                 **fields: Optional[str]) -> Item:
        """A new item. With *location* (a path, made with the locations above
        it when it does not exist) it gets its first stock row there, holding
        *quantity*. *fields* are any of description, category, manufacturer,
        mpn, digikey_pn, lcsc, url and notes."""
        name = _clean(name)
        if name is None:
            raise InventoryError("an item needs a name")
        _check_len("name", name)
        unknown = set(fields) - set(_TEXT_FIELDS)
        if unknown:
            raise TypeError(f"unknown item fields: {', '.join(sorted(unknown))}")
        values = {f: _clean_field(f, fields.get(f)) for f in _TEXT_FIELDS}
        for f, v in values.items():
            _check_len(f, v)
        quantity = _quantity(quantity)
        if quantity is not None and location is None:
            raise InventoryError("a quantity needs a location")
        stamp = self.db._now()
        with self._tx():
            cols = ("name",) + _TEXT_FIELDS + ("created_at", "updated_at")
            cur = self._x(
                f"INSERT INTO item ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (name, *values.values(), stamp, stamp))
            item_id = self._new_id(cur)
            self._set_tags(item_id, tags)
            if location is not None:
                self.add_stock(item_id, location, quantity)
        return self.item(item_id)

    def update_item(self, item_id: int, *, name: Any = _UNSET, tags: Any = _UNSET,
                    **fields: Any) -> Item:
        """Change the given fields of an item; a field not given is kept.
        ``None`` or ``""`` clears an optional field. Where it is and how many
        are its stock rows (:meth:`add_stock` and the others)."""
        unknown = set(fields) - set(_TEXT_FIELDS)
        if unknown:
            raise TypeError(f"unknown item fields: {', '.join(sorted(unknown))}")
        changes: Dict[str, Any] = {f: _clean_field(f, v) for f, v in fields.items()}
        for f, v in changes.items():
            _check_len(f, v)
        if name is not _UNSET:
            name = _clean(name)
            if name is None:
                raise InventoryError("an item needs a name")
            _check_len("name", name)
            changes["name"] = name
        with self._tx():
            target = self._need_item(item_id)
            if changes:
                sets = ", ".join(f"{f} = ?" for f in changes)
                self._x(f"UPDATE item SET {sets}, updated_at = ? WHERE id = ?",
                        (*changes.values(), self.db._now(), target.id))
            if tags is not _UNSET:
                self._set_tags(target.id, tags)
                if not changes:
                    self._x("UPDATE item SET updated_at = ? WHERE id = ?",
                            (self.db._now(), target.id))
        return self.item(target.id)

    def delete_item(self, item_id: int) -> None:
        """Delete the item with its stock rows, tags and pictures."""
        with self._tx():
            self._need_item(item_id)
            for table in ("stock", "item_picture", "item_tag"):
                self._x(f"DELETE FROM {table} WHERE item_id = ?", (int(item_id),))
            self._x("DELETE FROM item WHERE id = ?", (int(item_id),))

    # ----- stock -----------------------------------------------------------

    def stock_row(self, stock_id: int) -> Optional[Stock]:
        with self._read():
            row = self._x(f"{_STOCK_SELECT} WHERE stock.id = ?", (int(stock_id),)).fetchone()
        return Stock(*row) if row else None

    def _need_stock(self, stock_id: int) -> Stock:
        found = self.stock_row(stock_id)
        if found is None:
            raise InventoryError(f"no stock row {stock_id}")
        return found

    def _stock_at(self, item_id: int, location_id: int) -> Optional[Stock]:
        row = self._x(f"{_STOCK_SELECT} WHERE stock.item_id = ? AND stock.location_id = ?",
                      (item_id, location_id)).fetchone()
        return Stock(*row) if row else None

    def stock_in(self, location: str, *, recursive: bool = True) -> List[Stock]:
        """The stock rows in *location* and, when *recursive*, in every
        location inside it, by location path."""
        root = self._need_location(location)
        with self._read():
            if recursive:
                rows = self._x(f"{_STOCK_SELECT} WHERE location.path = ?"
                               " OR location.path LIKE ? ESCAPE '!'",
                               (root.path, _like_prefix(root.path)))
            else:
                rows = self._x(f"{_STOCK_SELECT} WHERE stock.location_id = ?", (root.id,))
            found = [Stock(*r) for r in rows.fetchall()]
        return sorted(found, key=lambda s: path_key(s.location_path))

    def add_stock(self, item_id: int, location: str, quantity: Optional[int] = None, *,
                  notes: Optional[str] = None) -> Stock:
        """Put some of the item in *location* (made when it does not exist).
        When the item already has a row there, *quantity* is added to it and
        that row is returned."""
        quantity = _quantity(quantity)
        notes = _clean_free(notes)
        with self._tx():
            target = self._need_item(item_id)
            loc = self.ensure_location(location)
            stamp = self.db._now()
            there = self._stock_at(target.id, loc.id)
            if there is not None:
                self._x("UPDATE stock SET quantity = ?, notes = ?, updated_at = ? WHERE id = ?",
                        (add_quantities(there.quantity, quantity),
                         _join_notes(there.notes, notes), stamp, there.id))
                return self.stock_row(there.id)
            cur = self._x("INSERT INTO stock (item_id, location_id, quantity, notes, updated_at)"
                          " VALUES (?, ?, ?, ?, ?)",
                          (target.id, loc.id, quantity, notes, stamp))
            new_id = self._new_id(cur)
        return self.stock_row(new_id)

    def update_stock(self, stock_id: int, *, quantity: Any = _UNSET,
                     notes: Any = _UNSET) -> Stock:
        """Set the row's quantity (``None``: unknown) or notes; a field not
        given is kept."""
        changes: Dict[str, Any] = {}
        if quantity is not _UNSET:
            changes["quantity"] = _quantity(quantity)
        if notes is not _UNSET:
            changes["notes"] = _clean_free(notes)
        with self._tx():
            target = self._need_stock(stock_id)
            if changes:
                sets = ", ".join(f"{f} = ?" for f in changes)
                self._x(f"UPDATE stock SET {sets}, updated_at = ? WHERE id = ?",
                        (*changes.values(), self.db._now(), target.id))
        return self.stock_row(target.id)

    def adjust_stock(self, stock_id: int, delta: int) -> Stock:
        """Add *delta* to the row's quantity, never below zero (the row
        stays); an unknown quantity counts from zero."""
        with self._tx():
            target = self._need_stock(stock_id)
            return self.update_stock(target.id,
                                     quantity=max(0, (target.quantity or 0) + int(delta)))

    def move_stock(self, stock_id: int, location: str) -> Stock:
        """Move the row to *location* (made when it does not exist). When the
        item already has a row there, the two become one, quantities added."""
        with self._tx():
            target = self._need_stock(stock_id)
            loc = self.ensure_location(location)
            if loc.id == target.location_id:
                return target
            there = self._stock_at(target.item_id, loc.id)
            stamp = self.db._now()
            if there is not None:
                self._x("UPDATE stock SET quantity = ?, notes = ?, updated_at = ? WHERE id = ?",
                        (add_quantities(there.quantity, target.quantity),
                         _join_notes(there.notes, target.notes), stamp, there.id))
                self._x("DELETE FROM stock WHERE id = ?", (target.id,))
                return self.stock_row(there.id)
            self._x("UPDATE stock SET location_id = ?, updated_at = ? WHERE id = ?",
                    (loc.id, stamp, target.id))
        return self.stock_row(target.id)

    def delete_stock(self, stock_id: int) -> None:
        """Remove the row: none of the item is kept there any more."""
        with self._tx():
            target = self._need_stock(stock_id)
            self._x("DELETE FROM stock WHERE id = ?", (target.id,))

    # ----- pictures --------------------------------------------------------

    def _pictures_where(self, where: str, params: Sequence[Any]) -> List[Picture]:
        rows = self._x(f"SELECT {', '.join(_PICTURE_COLS)} FROM item_picture"
                       f" WHERE {where} ORDER BY item_id, position", params)
        return [Picture(*r) for r in rows.fetchall()]

    def pictures(self, item_id: int) -> List[Picture]:
        """The item's pictures in order, the main one first."""
        with self._read():
            return self._pictures_where("item_id = ?", (int(item_id),))

    def picture(self, picture_id: int) -> Optional[Picture]:
        with self._read():
            found = self._pictures_where("id = ?", (int(picture_id),))
        return found[0] if found else None

    def _need_picture(self, picture_id: int) -> Picture:
        found = self.picture(picture_id)
        if found is None:
            raise InventoryError(f"no picture {picture_id}")
        return found

    def picture_data(self, picture_id: int, *,
                     thumb: bool = False) -> Optional[Tuple[str, bytes]]:
        """``(content type, bytes)`` of the picture or its thumbnail;
        ``None`` when there is no such picture."""
        column = "thumb" if thumb else "data"
        with self._read():
            row = self._x(f"SELECT content_type, {column} FROM item_picture WHERE id = ?",
                          (int(picture_id),)).fetchone()
        return (row[0], bytes(row[1])) if row else None

    def add_picture(self, item_id: int, image: bytes, *,
                    caption: Optional[str] = None) -> Picture:
        """Add the image *image* (the bytes of any picture Pillow reads) to
        the item, after its other pictures. It is stored as
        :func:`kicadtools.pictures.prepare` makes it; a bad image raises
        :class:`~kicadtools.pictures.PictureError`."""
        from kicadtools.pictures import prepare

        prepared = prepare(image)
        with self._tx():
            target = self._need_item(item_id)
            position = len(target.pictures)
            stamp = self.db._now()
            cur = self._x(
                "INSERT INTO item_picture (item_id, position, content_type, width, height,"
                " data, thumb, caption, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (target.id, position, prepared.content_type, prepared.width,
                 prepared.height, prepared.data, prepared.thumb, _clean_free(caption), stamp))
            new_id = self._new_id(cur)
            self._x("UPDATE item SET updated_at = ? WHERE id = ?", (stamp, target.id))
        return self.picture(new_id)

    def copy_picture(self, picture_id: int, item_id: int) -> Picture:
        """Give the item *item_id* a copy of the picture *picture_id* (of any
        item), after its other pictures. The stored image is copied as it
        is, not made again."""
        with self._tx():
            source = self._need_picture(picture_id)
            target = self._need_item(item_id)
            stamp = self.db._now()
            cur = self._x(
                "INSERT INTO item_picture (item_id, position, content_type, width, height,"
                " data, thumb, caption, created_at) SELECT ?, ?, content_type, width, height,"
                " data, thumb, caption, ? FROM item_picture WHERE id = ?",
                (target.id, len(target.pictures), stamp, source.id))
            new_id = self._new_id(cur)
            self._x("UPDATE item SET updated_at = ? WHERE id = ?", (stamp, target.id))
        return self.picture(new_id)

    def update_picture(self, picture_id: int, *, caption: Optional[str]) -> Picture:
        """Set the picture's caption; ``None`` or ``""`` clears it."""
        with self._tx():
            target = self._need_picture(picture_id)
            self._x("UPDATE item_picture SET caption = ? WHERE id = ?",
                    (_clean_free(caption), target.id))
        return self.picture(target.id)

    def _renumber(self, item_id: int, order: List[int]) -> None:
        for position, picture_id in enumerate(order):
            self._x("UPDATE item_picture SET position = ? WHERE id = ?", (position, picture_id))
        self._x("UPDATE item SET updated_at = ? WHERE id = ?", (self.db._now(), item_id))

    def move_picture(self, picture_id: int, position: int) -> Picture:
        """Put the picture at *position* among its item's pictures (0 makes
        it the main one); a position past the end puts it last."""
        with self._tx():
            target = self._need_picture(picture_id)
            order = [p.id for p in self.pictures(target.item_id) if p.id != target.id]
            order.insert(max(0, min(int(position), len(order))), target.id)
            self._renumber(target.item_id, order)
        return self.picture(target.id)

    def delete_picture(self, picture_id: int) -> None:
        """Delete the picture; the ones after it move up."""
        with self._tx():
            target = self._need_picture(picture_id)
            self._x("DELETE FROM item_picture WHERE id = ?", (target.id,))
            self._renumber(target.item_id, [p.id for p in self.pictures(target.item_id)])

    # ----- values used so far ---------------------------------------------

    def values(self, field: str) -> List[str]:
        """The values already used in an open-ended item field
        (``"category"``, ``"manufacturer"``), one spelling per value ignoring
        case, in natural order."""
        if field not in _VALUE_FIELDS:
            raise ValueError(f"no list of used values for {field!r}")
        with self._read():
            rows = self._x(f"SELECT DISTINCT {field} FROM item"
                           f" WHERE {field} IS NOT NULL").fetchall()
        return sorted({r[0].casefold(): r[0] for r in rows}.values(), key=natural_key)

    def tags(self) -> List[str]:
        """Every tag in use, one spelling per tag ignoring case."""
        with self._read():
            rows = self._x("SELECT DISTINCT tag FROM item_tag").fetchall()
        return sorted({r[0].casefold(): r[0] for r in rows}.values(), key=natural_key)
