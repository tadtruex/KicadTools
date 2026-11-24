"""The lab inventory: what is on the shelves, in which bin, about how many
(``INVENTORY_PLAN.md``, step 1).

The tables are in the parts database, beside the parts tables, and
:class:`InventoryDb` works on an open :class:`~kicadtools.partsdb.PartsDb`
(its connection, lock, transactions and engine). They are separate from
``part``: ``part`` holds design decisions, these tables hold what is on hand.

- ``bin`` — a place things are kept. Bins nest to any depth: a cabinet
  ``C12`` holds drawers ``C12/3`` and ``C12/5``. ``name`` is the bin's own
  name (no ``/``); ``path`` is the names from the top joined with ``/``,
  unique and compared ignoring case, so a URL or a QR code finds a bin with
  one lookup. ``site`` (``"school"``, ``"home"`` …) is optional; a bin with
  none takes its parent's (:meth:`InventoryDb.site_of`).
- ``item`` — one kind of thing in one bin: a name (the only required field),
  an optional approximate quantity, and optional category, manufacturer,
  MPN, DigiKey PN, LCSC number, URL, description and notes.
- ``item_tag`` — free words on an item (``arduino``, ``avr``), one row per
  tag, unique per item ignoring case.

Nothing is a fixed list: sites, categories, manufacturers and tags take any
value, and :meth:`InventoryDb.values` and :meth:`InventoryDb.tags` give the
ones used so far, for a box that offers them as you type.

The tables are created with ``CREATE TABLE IF NOT EXISTS`` when an
:class:`InventoryDb` is opened, within parts schema version 2 (as the
``inductor``, ``tht`` and ``led`` tables were added), so no migration is
needed and an older KicadTools ignores them.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from kicadtools.partsdb import PartsDb

# Copied in this order by backups (parents before the rows that refer to them).
INVENTORY_TABLES = ("bin", "item", "item_tag")

SEP = "/"

_SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bin (
  id          INTEGER PRIMARY KEY,
  parent_id   INTEGER REFERENCES bin(id),
  name        TEXT NOT NULL,
  path        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  site        TEXT COLLATE NOCASE,
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

# MariaDB: the same tables. Text compared ignoring case uses utf8mb4_unicode_ci
# (as partsdb's mpn and manufacturer do); the rest of a table compares bytes.
_OPTS = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
_CI = "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
_MARIA_SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS bin (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  parent_id   INT,
  name        VARCHAR(100) NOT NULL,
  path        VARCHAR(255) {_CI} NOT NULL,
  site        VARCHAR(64) {_CI},
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

# The item fields set by add_item / update_item besides name, quantity, bin
# and tags. All optional text: "" or None clears one.
_TEXT_FIELDS = ("description", "category", "manufacturer", "mpn", "digikey_pn", "lcsc",
                "url", "notes")
_ITEM_COLS = ("id", "name", "description", "category", "manufacturer", "mpn",
              "digikey_pn", "lcsc", "quantity", "bin_id", "url", "notes",
              "created_at", "updated_at")
_BIN_COLS = ("id", "parent_id", "name", "path", "site", "location", "notes", "updated_at")

# Fields whose used values values() lists: item columns and bin columns.
_ITEM_VALUE_FIELDS = ("category", "manufacturer")
_BIN_VALUE_FIELDS = ("site",)

# Lengths of the MariaDB columns, checked on both engines so a value SQLite
# would take is not refused later by the server.
_MAX = {"bin.name": 100, "bin.path": 255, "site": 64, "name": 255, "category": 64,
        "manufacturer": 128, "mpn": 128, "digikey_pn": 100, "lcsc": 32, "tag": 64}

_UNSET: Any = object()


class InventoryError(ValueError):
    """A refused inventory change: a bad name, a bin that does not exist or
    already exists, a non-empty bin deleted, a bin moved inside itself."""


@dataclass(frozen=True)
class Bin:
    id: int
    parent_id: Optional[int]
    name: str
    path: str
    site: Optional[str]          # this bin's own; see InventoryDb.site_of
    location: Optional[str]
    notes: Optional[str]
    updated_at: str

    @property
    def depth(self) -> int:
        return self.path.count(SEP)


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
    quantity: Optional[int]
    bin_id: Optional[int]
    bin_path: Optional[str]
    url: Optional[str]
    notes: Optional[str]
    tags: Tuple[str, ...]
    created_at: str
    updated_at: str


def natural_key(text: str) -> tuple:
    """Sort key putting ``"2"`` before ``"10"`` and ``"C2"`` before
    ``"C10"``, ignoring case."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                 for p in re.split(r"(\d+)", text.casefold()) if p)


def _clean(text: Optional[str]) -> Optional[str]:
    """*text* with surrounding and repeated whitespace removed; ``None`` for
    ``None`` or blank."""
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text or None


def _check_len(field: str, value: Optional[str]) -> None:
    limit = _MAX.get(field)
    if value is not None and limit is not None and len(value) > limit:
        raise InventoryError(f"{field} is longer than {limit} characters: {value!r}")


def bin_name(name: str) -> str:
    """*name* cleaned for use as one bin's name; refuses blank names and
    names holding ``/``."""
    cleaned = _clean(name)
    if cleaned is None:
        raise InventoryError("a bin needs a name")
    if SEP in cleaned:
        raise InventoryError(f"a bin name cannot hold {SEP!r}: {cleaned!r}")
    _check_len("bin.name", cleaned)
    return cleaned


def split_path(path: str) -> List[str]:
    """The names in a bin path, cleaned: ``" C12 / 5 "`` → ``["C12", "5"]``."""
    parts = [p for p in (path or "").split(SEP)]
    if not any(p.strip() for p in parts):
        raise InventoryError("an empty bin path")
    return [bin_name(p) for p in parts]


def norm_path(path: str) -> str:
    """*path* with each name cleaned: ``" C12 / 5 "`` → ``"C12/5"``."""
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


class InventoryDb:
    """The inventory tables of the open parts database *db*; creates them
    when missing."""

    def __init__(self, db: PartsDb) -> None:
        self.db = db
        self._depth = 0
        schema = _MARIA_SCHEMA if db.backend == "mariadb" else _SQLITE_SCHEMA
        if not db.read_only:
            with self._tx():
                for statement in schema:
                    db._execute(statement)

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

    def _x(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.db._execute(sql, params)

    def _new_id(self, cur: Any) -> int:
        return int(cur.lastrowid)

    # ----- bins ------------------------------------------------------------

    @staticmethod
    def _bin(row: Sequence[Any]) -> Bin:
        return Bin(*row)

    def _bin_where(self, where: str, params: Sequence[Any]) -> List[Bin]:
        rows = self._x(f"SELECT {', '.join(_BIN_COLS)} FROM bin WHERE {where}", params)
        return [self._bin(r) for r in rows.fetchall()]

    def bin(self, path: str) -> Optional[Bin]:
        """The bin at *path* (case ignored), ``None`` when there is none."""
        with self.db._lock:
            found = self._bin_where("path = ?", (norm_path(path),))
        return found[0] if found else None

    def bin_by_id(self, bin_id: int) -> Optional[Bin]:
        with self.db._lock:
            found = self._bin_where("id = ?", (int(bin_id),))
        return found[0] if found else None

    def _need_bin(self, path: str) -> Bin:
        found = self.bin(path)
        if found is None:
            raise InventoryError(f"no bin {path!r}")
        return found

    def bins(self) -> List[Bin]:
        """Every bin, parents before children, siblings in natural order."""
        with self.db._lock:
            found = self._bin_where("1 = 1", ())
        return sorted(found, key=lambda b: [natural_key(n) for n in b.path.split(SEP)])

    def children(self, path: Optional[str] = None) -> List[Bin]:
        """The bins directly inside *path*; the top-level bins for ``None``."""
        with self.db._lock:
            if path is None:
                found = self._bin_where("parent_id IS NULL", ())
            else:
                found = self._bin_where("parent_id = ?", (self._need_bin(path).id,))
        return sorted(found, key=lambda b: natural_key(b.name))

    def subtree(self, path: str) -> List[Bin]:
        """The bin at *path* and every bin inside it, at any depth, parents
        first."""
        root = self._need_bin(path)
        with self.db._lock:
            found = self._bin_where("path = ? OR path LIKE ? ESCAPE '!'",
                                    (root.path, _like_prefix(root.path)))
        return sorted(found, key=lambda b: [natural_key(n) for n in b.path.split(SEP)])

    def site_of(self, path: str) -> Optional[str]:
        """The bin's site, or the nearest enclosing bin's; ``None`` when no
        bin on the way up has one."""
        names = split_path(path)
        with self.db._lock:
            for depth in range(len(names), 0, -1):
                row = self._x("SELECT site FROM bin WHERE path = ?",
                              (SEP.join(names[:depth]),)).fetchone()
                if row is not None and row[0]:
                    return row[0]
        return None

    def add_bin(self, name: str, parent: Optional[str] = None, *, site: Optional[str] = None,
                location: Optional[str] = None, notes: Optional[str] = None) -> Bin:
        """A new bin *name* inside *parent* (top level for ``None``)."""
        name = bin_name(name)
        site = _clean(site)
        _check_len("site", site)
        with self._tx():
            parent_bin = self._need_bin(parent) if parent is not None else None
            path = f"{parent_bin.path}{SEP}{name}" if parent_bin else name
            _check_len("bin.path", path)
            if self.bin(path) is not None:
                raise InventoryError(f"bin {path!r} already exists")
            cur = self._x(
                "INSERT INTO bin (parent_id, name, path, site, location, notes, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (parent_bin.id if parent_bin else None, name, path, site,
                 _clean(location), _clean(notes), self.db._now()))
            new_id = self._new_id(cur)
        return self.bin_by_id(new_id)

    def ensure_bin(self, path: str, *, site: Optional[str] = None) -> Bin:
        """The bin at *path*, made with any missing bins above it. *site* is
        set on the top bin when that bin is made here."""
        names = split_path(path)
        with self._tx():
            parent: Optional[str] = None
            for depth, name in enumerate(names):
                here = SEP.join(names[:depth + 1])
                if self.bin(here) is None:
                    self.add_bin(name, parent, site=site if depth == 0 else None)
                parent = here
        return self._need_bin(path)

    def update_bin(self, path: str, *, site: Any = _UNSET, location: Any = _UNSET,
                   notes: Any = _UNSET) -> Bin:
        """Set the bin's site, location or notes; a field not given is kept,
        and ``None`` or ``""`` clears one."""
        changes: Dict[str, Optional[str]] = {}
        for field, value in (("site", site), ("location", location), ("notes", notes)):
            if value is not _UNSET:
                changes[field] = _clean(value)
        _check_len("site", changes.get("site"))
        with self._tx():
            target = self._need_bin(path)
            if changes:
                sets = ", ".join(f"{f} = ?" for f in changes)
                self._x(f"UPDATE bin SET {sets}, updated_at = ? WHERE id = ?",
                        (*changes.values(), self.db._now(), target.id))
        return self.bin_by_id(target.id)

    def _repath(self, target: Bin, new_parent: Optional[Bin], new_name: str) -> Bin:
        """Give *target* the name *new_name* inside *new_parent* and rewrite
        the paths of every bin inside it, in the caller's transaction."""
        new_path = f"{new_parent.path}{SEP}{new_name}" if new_parent else new_name
        _check_len("bin.path", new_path)
        clash = self.bin(new_path)
        if clash is not None and clash.id != target.id:
            raise InventoryError(f"bin {new_path!r} already exists")
        below = [b for b in self.subtree(target.path) if b.id != target.id]
        stamp = self.db._now()
        self._x("UPDATE bin SET parent_id = ?, name = ?, path = ?, updated_at = ? WHERE id = ?",
                (new_parent.id if new_parent else None, new_name, new_path, stamp, target.id))
        for b in below:
            moved = new_path + b.path[len(target.path):]
            _check_len("bin.path", moved)
            self._x("UPDATE bin SET path = ?, updated_at = ? WHERE id = ?",
                    (moved, stamp, b.id))
        return self.bin_by_id(target.id)

    def rename_bin(self, path: str, new_name: str) -> Bin:
        """Rename the bin at *path*; the bins inside it follow."""
        new_name = bin_name(new_name)
        with self._tx():
            target = self._need_bin(path)
            parent = self.bin_by_id(target.parent_id) if target.parent_id else None
            return self._repath(target, parent, new_name)

    def move_bin(self, path: str, new_parent: Optional[str]) -> Bin:
        """Move the bin at *path* (and everything in it) inside *new_parent*,
        or to the top level for ``None``."""
        with self._tx():
            target = self._need_bin(path)
            parent = self._need_bin(new_parent) if new_parent is not None else None
            if parent is not None and (parent.id == target.id
                                       or parent.path.casefold().startswith(
                                           target.path.casefold() + SEP)):
                raise InventoryError(f"cannot move {target.path!r} inside itself")
            return self._repath(target, parent, target.name)

    def delete_bin(self, path: str) -> None:
        """Delete the bin at *path*; refused while it holds items or bins."""
        with self._tx():
            target = self._need_bin(path)
            if self._x("SELECT 1 FROM bin WHERE parent_id = ?", (target.id,)).fetchone():
                raise InventoryError(f"bin {target.path!r} holds other bins")
            if self._x("SELECT 1 FROM item WHERE bin_id = ?", (target.id,)).fetchone():
                raise InventoryError(f"bin {target.path!r} holds items")
            self._x("DELETE FROM bin WHERE id = ?", (target.id,))

    # ----- items -----------------------------------------------------------

    def _items_where(self, where: str, params: Sequence[Any]) -> List[Item]:
        cols = ", ".join(f"item.{c}" for c in _ITEM_COLS)
        rows = self._x(f"SELECT {cols}, bin.path FROM item"
                       f" LEFT JOIN bin ON bin.id = item.bin_id WHERE {where}",
                       params).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        tags: Dict[int, List[str]] = {i: [] for i in ids}
        marks = ", ".join("?" * len(ids))
        for item_id, tag in self._x(
                f"SELECT item_id, tag FROM item_tag WHERE item_id IN ({marks})", ids):
            tags[item_id].append(tag)
        out = []
        for r in rows:
            values = dict(zip(_ITEM_COLS, r[:len(_ITEM_COLS)]))
            out.append(Item(bin_path=r[len(_ITEM_COLS)],
                            tags=tuple(sorted(tags[values["id"]], key=natural_key)),
                            **values))
        return out

    def item(self, item_id: int) -> Optional[Item]:
        with self.db._lock:
            found = self._items_where("item.id = ?", (int(item_id),))
        return found[0] if found else None

    def items(self, bin: Optional[str] = None, *, recursive: bool = True) -> List[Item]:
        """Every item; with *bin*, the items in that bin and, when
        *recursive*, in every bin inside it. Ordered by bin path, then name."""
        with self.db._lock:
            if bin is None:
                found = self._items_where("1 = 1", ())
            else:
                root = self._need_bin(bin)
                if recursive:
                    found = self._items_where(
                        "bin.path = ? OR bin.path LIKE ? ESCAPE '!'",
                        (root.path, _like_prefix(root.path)))
                else:
                    found = self._items_where("item.bin_id = ?", (root.id,))
        return sorted(found, key=lambda i: (
            [natural_key(n) for n in (i.bin_path or "").split(SEP)], natural_key(i.name)))

    def unbinned(self) -> List[Item]:
        """The items in no bin."""
        with self.db._lock:
            found = self._items_where("item.bin_id IS NULL", ())
        return sorted(found, key=lambda i: natural_key(i.name))

    @staticmethod
    def _quantity(quantity: Optional[int]) -> Optional[int]:
        if quantity is None:
            return None
        quantity = int(quantity)
        if quantity < 0:
            raise InventoryError(f"a quantity cannot be negative: {quantity}")
        return quantity

    def _set_tags(self, item_id: int, tags: Iterable[str]) -> None:
        self._x("DELETE FROM item_tag WHERE item_id = ?", (item_id,))
        for tag in _norm_tags(tags):
            self._x("INSERT INTO item_tag (item_id, tag) VALUES (?, ?)", (item_id, tag))

    def add_item(self, name: str, *, bin: Optional[str] = None,
                 quantity: Optional[int] = None, tags: Iterable[str] = (),
                 **fields: Optional[str]) -> Item:
        """A new item. *bin* is a bin path, made (with the bins above it) when
        it does not exist. *fields* are any of description, category,
        manufacturer, mpn, digikey_pn, lcsc, url and notes."""
        name = _clean(name)
        if name is None:
            raise InventoryError("an item needs a name")
        _check_len("name", name)
        unknown = set(fields) - set(_TEXT_FIELDS)
        if unknown:
            raise TypeError(f"unknown item fields: {', '.join(sorted(unknown))}")
        values = {f: _clean(fields.get(f)) for f in _TEXT_FIELDS}
        for f, v in values.items():
            _check_len(f, v)
        quantity = self._quantity(quantity)
        stamp = self.db._now()
        with self._tx():
            bin_id = self.ensure_bin(bin).id if bin is not None else None
            cols = ("name",) + _TEXT_FIELDS + ("quantity", "bin_id", "created_at", "updated_at")
            cur = self._x(
                f"INSERT INTO item ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (name, *values.values(), quantity, bin_id, stamp, stamp))
            item_id = self._new_id(cur)
            self._set_tags(item_id, tags)
        return self.item(item_id)

    def _need_item(self, item_id: int) -> Item:
        found = self.item(item_id)
        if found is None:
            raise InventoryError(f"no item {item_id}")
        return found

    def update_item(self, item_id: int, *, name: Any = _UNSET, quantity: Any = _UNSET,
                    bin: Any = _UNSET, tags: Any = _UNSET, **fields: Any) -> Item:
        """Change the given fields of an item; a field not given is kept.
        ``None`` or ``""`` clears an optional field; *bin* ``None`` takes the
        item out of every bin, and a bin path that does not exist is made."""
        unknown = set(fields) - set(_TEXT_FIELDS)
        if unknown:
            raise TypeError(f"unknown item fields: {', '.join(sorted(unknown))}")
        changes: Dict[str, Any] = {f: _clean(v) for f, v in fields.items()}
        for f, v in changes.items():
            _check_len(f, v)
        if name is not _UNSET:
            name = _clean(name)
            if name is None:
                raise InventoryError("an item needs a name")
            _check_len("name", name)
            changes["name"] = name
        if quantity is not _UNSET:
            changes["quantity"] = self._quantity(quantity)
        with self._tx():
            target = self._need_item(item_id)
            if bin is not _UNSET:
                changes["bin_id"] = self.ensure_bin(bin).id if bin is not None else None
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

    def adjust_quantity(self, item_id: int, delta: int) -> Item:
        """Add *delta* to the item's quantity, never below zero; an item with
        no quantity counts from zero."""
        with self._tx():
            target = self._need_item(item_id)
            new = max(0, (target.quantity or 0) + int(delta))
            return self.update_item(target.id, quantity=new)

    def delete_item(self, item_id: int) -> None:
        with self._tx():
            self._need_item(item_id)
            self._x("DELETE FROM item_tag WHERE item_id = ?", (int(item_id),))
            self._x("DELETE FROM item WHERE id = ?", (int(item_id),))

    # ----- values used so far ---------------------------------------------

    def values(self, field: str) -> List[str]:
        """The values already used in an open-ended field (``"category"``,
        ``"manufacturer"``, ``"site"``), one spelling per value ignoring case,
        in natural order."""
        if field in _ITEM_VALUE_FIELDS:
            table = "item"
        elif field in _BIN_VALUE_FIELDS:
            table = "bin"
        else:
            raise ValueError(f"no list of used values for {field!r}")
        with self.db._lock:
            rows = self._x(f"SELECT DISTINCT {field} FROM {table}"
                           f" WHERE {field} IS NOT NULL").fetchall()
        return sorted({r[0].casefold(): r[0] for r in rows}.values(), key=natural_key)

    def tags(self) -> List[str]:
        """Every tag in use, one spelling per tag ignoring case."""
        with self.db._lock:
            rows = self._x("SELECT DISTINCT tag FROM item_tag").fetchall()
        return sorted({r[0].casefold(): r[0] for r in rows}.values(), key=natural_key)
