"""Copy the real inventory (bins, items, tags) into a test database, to try
the web app on real data without touching it.

Run from Code/KicadTools with the venv:

    .venv/bin/python scripts/copy_inventory_to_test.py TEST_CONFIG

The source is the database the normal KicadTools configuration names
(``~/.kicadtools/config.toml`` or ``$KICADTOOLS_CONFIG``). It is opened
read-only: no schema statements run on it and the server refuses any write,
so a ``develop`` checkout with new tables or columns does not add them to
the real database the release uses. Its tables must be ones this code
reads.
*TEST_CONFIG* is a copy of that file with ``database =`` set to a name
ending ``_test`` (a SQLite file: named ``…_test.sqlite``); its inventory
tables are emptied and refilled. Nothing else in either database is touched.
"""
import os
import sys

from kicadtools.dbconfig import load_config, terminal_ask
from kicadtools.dbconnect import open_parts_db
from kicadtools.inventorydb import InventoryDb

TEXT_FIELDS = ("description", "category", "manufacturer", "mpn", "digikey_pn",
               "lcsc", "url", "notes")


def main(test_config: str) -> None:
    source = open_parts_db(load_config(), terminal_ask, read_only=True)
    os.environ["KICADTOOLS_CONFIG"] = test_config
    target = open_parts_db(load_config(), terminal_ask)
    try:
        if target.db.backend == "mariadb":
            name = target.db._execute("SELECT DATABASE()").fetchone()[0]
        else:   # the file's name without its extension: parts_test.sqlite
            path = target.db._execute("PRAGMA database_list").fetchone()[2]
            name = os.path.splitext(path)[0] if path else ":memory:"
        if not name.endswith("_test"):
            sys.exit(f"refusing to overwrite {name!r}: the target must end in _test")
        src, dst = InventoryDb(source.db), InventoryDb(target.db)
        with dst._tx():
            for table in ("item_tag", "item", "bin"):
                dst._x(f"DELETE FROM {table}")
            for b in sorted(src.bins(), key=lambda b: b.depth):
                parent = b.path.rsplit("/", 1)[0] if "/" in b.path else None
                dst.add_bin(b.name, parent, site=b.site, description=b.description,
                            location=b.location, notes=b.notes)
            for i in src.items():
                dst.add_item(i.name, bin=i.bin_path, quantity=i.quantity, tags=i.tags,
                             **{f: getattr(i, f) for f in TEXT_FIELDS})
        print(f"{name}: {len(dst.bins())} bins, {len(dst.items())} items")
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
