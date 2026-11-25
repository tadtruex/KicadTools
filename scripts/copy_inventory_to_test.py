"""Copy the real inventory into a test database, to try the web app on real
data without touching it.

Run from Code/KicadTools with the venv:

    .venv/bin/python scripts/copy_inventory_to_test.py TEST_CONFIG

The source is the database the normal KicadTools configuration names
(``~/.kicadtools/config.toml`` or ``$KICADTOOLS_CONFIG``). It is opened
read-only: no schema statements run on it and the server refuses any write,
so a ``develop`` checkout with new tables or columns does not add them to
the real database the release uses.

*TEST_CONFIG* is a copy of that file with ``database =`` set to a name
ending ``_test`` (a SQLite file: named ``…_test.sqlite``). Its inventory
tables, of either layout, are dropped and made again from the source:

- a source still in the first layout (bins, release ``kt_v0.1a``) is copied
  as it is and then converted with ``inventorymigrate.migrate``, so every
  copy tries the conversion on the real data;
- a converted source is copied table by table.

Nothing else in either database is touched.
"""
import os
import sys

from kicadtools.dbconfig import load_config, terminal_ask
from kicadtools.dbconnect import open_parts_db
from kicadtools.inventorydb import INVENTORY_TABLES, LAYOUT, LAYOUT_KEY, InventoryDb, layout
from kicadtools.inventorymigrate import copy_v1_rows, create_v1, migrate

# Children before the tables they refer to, both layouts.
DROP_ORDER = ("stock", "item_picture", "item_tag", "item", "location", "bin")


def _has(db, table):
    return bool(db._execute(db._dialect.has_table(), (table,)).fetchone())


def main(test_config: str) -> None:
    source = open_parts_db(load_config(), terminal_ask, read_only=True)
    os.environ["KICADTOOLS_CONFIG"] = test_config
    target = open_parts_db(load_config(), terminal_ask)
    try:
        src, dst = source.db, target.db
        if dst.backend == "mariadb":
            name = dst._execute("SELECT DATABASE()").fetchone()[0]
        else:   # the file's name without its extension: parts_test.sqlite
            path = dst._execute("PRAGMA database_list").fetchone()[2]
            name = os.path.splitext(path)[0] if path else ":memory:"
        if not name.endswith("_test"):
            sys.exit(f"refusing to overwrite {name!r}: the target must end in _test")
        found = layout(src)
        if found not in ("1", LAYOUT):
            sys.exit(f"the source inventory is in layout {found!r}; nothing copied")

        with dst._tx():
            for table in DROP_ORDER:
                dst._execute(f"DROP TABLE IF EXISTS {table}")
            dst._execute('DELETE FROM meta WHERE "key" = ?', (LAYOUT_KEY,))

        if found == "1":
            create_v1(dst, pictures=_has(src, "item_picture"))
            counts = copy_v1_rows(src, dst)
            print(f"copied (first layout): {counts}")
            done = migrate(dst)
            print("converted: " + done.summary().replace("\n", "\n  "))
        else:
            InventoryDb(dst)
            with dst._tx():
                for table in INVENTORY_TABLES:
                    cur = src._execute(f"SELECT * FROM {table}")
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    if table == "location":
                        at = cols.index("path")
                        rows = sorted(rows, key=lambda r: str(r[at]).count("/"))
                    if rows:
                        dst._insert_rows(table, cols, rows)
            print("copied (converted source)")
        inv = InventoryDb(dst)
        items = inv.items()
        print(f"{name}: {len(inv.locations())} locations, {len(items)} items, "
              f"{sum(len(i.stock) for i in items)} stock rows, "
              f"{sum(len(i.pictures) for i in items)} pictures")
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
