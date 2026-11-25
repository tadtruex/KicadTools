# Lab inventory — plan (sketch)

Status (2026-11-25): steps 1–5 done. The web app runs on broadpeak as a service on the real `tadtruex_inventory`, release `kt_v0.1a` (bins), at `https://broadpeak.tail7c4deb.ts.net` (tailnet only); not yet tried on the phone. Built on `develop`, not released: locations instead of bins and sites, items with stock rows, item pictures (no web pages for pictures yet), and `kicadtools inventory migrate` to convert the real database at the cutover (see "Locations and stock — as built"). Steps 6–7 not started. QR codes will encode a name under the user's own domain, which needs a nameserver first. No inventory CLI beyond the import and the migration (user decision 2026-11-24: the web app is the interface).

Two uses for one inventory of what is physically in the labs:

1. **Lab lookup (first).** A web page on `broadpeak` where a keyword search
   ("arduino", "stlink", "m6 screw") lists what is on hand, about how many,
   and which bin and where that bin is, fast enough to answer a student at
   the door. Adding a new item ("STLink V3", "M6 x 5 mm pan head screw")
   takes a few seconds. Bins nest (a cabinet, its drawers), and each has a
   printed QR code that opens a list of everything in it and in the bins
   inside it.
2. **BOM creation (later).** The Cockpit checks the inventory when it picks
   parts, and the BOM says which parts are already on a shelf.

The source today is `Libraries/Master Component List.numbers` (122 rows, SMD
board parts). What is kept from it: MPN and DigiKey PN where present,
description, approximate quantity, and the bin. The bin is the **Comp
Number** column: 113 rows have one, all different (one item per bin today);
1–142 are small static-safe storage boxes, 10001 and 10004 are partial reels
(or boxes). Everything in the file is at school. 9 rows have no bin
(`100uF`, `150uF`, `Gecko BT Module`, `47pF`, `1uF`, `10nF`, `USBDF01W5`,
`USBDF02W6`, `30V 100mA Schottky`) and were imported with none.

## Decided 2026-11-24: sites become locations, bins become locations

Not built yet. This changes the "Bins" and "Imported bins" decisions
below and the data model, search and web app sections after them; those
sections still describe what is built today.

- **No site.** The site is the first element of the hierarchy, not a
  field: what is `17` with site `school` today becomes `school/17`. The
  `site` column, `site_of`, `values("site")` and the inherit-from-parent
  rule go.
- **"Bin" becomes "Location".** A location is anything in the hierarchy:
  a lab (`school`), a cabinet (`school/C12`), a drawer (`school/C12/5`), a
  box. The word covers all of them; "bin" fits only the last two. Applies
  to the table, the code (`InventoryDb`, `inventory.py`, the web app), the
  URLs (`/location/school/C12/5`, the address a QR code encodes) and every
  word on the pages.

What follows from it:

- **Data:** the 113 imported bins move under one top-level location
  `school`; the 9 items with no bin stay with no location.
- **Schema:** `bin` → `location`, `site` dropped; `item.bin_id` moves
  into stock rows (see "an item is a thing" below). This is a migration on a populated database
  (rename, move, drop), unlike the `CREATE TABLE IF NOT EXISTS` additions
  so far. It runs from the user's account: the web app's database user
  (`deploy_inventory_web.sh`) cannot ALTER, and its grants name `bin`, so
  the script changes with it. Backups and `kicadtools db pull` change to
  the new table name.
- **Search:** the site filter becomes a filter on the top-level location
  (still remembered in a cookie); "site" leaves the searched fields, since
  the location path already holds it.
- **Name clash:** a bin has a free-text field `location` today ("rack by
  the bench, top shelf"). With the table called `location` it goes: a
  location has one free-text `description`, which says both what it is and
  where to find it (decided 2026-11-24). On `tadtruex_inventory` every
  bin's `description`, `location` and `notes` is empty (checked
  2026-11-24, read only), so the column is dropped with nothing to merge.
  Search drops it from the searched fields.
- **When:** before labels are printed (step 6), since a printed QR code
  holds the URL. Best before real data is entered through the deployed app,
  so the migration moves only the imported rows.

## Decided 2026-11-24: an item is a thing; stock rows place it

Not built yet. Changes the data model below (`item.bin_id`,
`item.quantity`) together with the locations decision above.

- **An item** is a kind of thing ("Nano Every", "0.1µF 0603
  CL10B104MB8NNNC", "M6 x 5 mm pan head screw"): name, description,
  category, tags, manufacturer, part numbers, URL, notes. It has no
  location and no quantity.
- **A stock row** says some of an item is in a location, with its own
  approximate quantity: `(item, location, quantity, notes, updated_at)`,
  one row per item and location. An item can be in any number of
  locations, or in none (known, none on hand that anyone has placed).
  The table is `stock` (decided 2026-11-24).

What follows from it:

- **Schema:** a new table `stock` (`item_id` FK → item, `location_id` FK
  → location, `quantity` optional integer, `notes`, `updated_at`; unique
  on `item_id, location_id`). `item.bin_id` and `item.quantity` go. A
  location holding stock rows is not deleted, as with items today. The web
  app's grants add `stock` to the tables it writes. Backups and `db pull`
  add the table.
- **Migration**, in the same run as the locations one: each item in a bin
  gets one stock row there with its quantity; the 9 items in no bin get
  none. Checked on `tadtruex_inventory` (2026-11-24, read only): 122 items,
  113 in a bin, each bin holding one item; no MPN or DigiKey PN appears
  twice. Four names repeat (`0.1uF 0603` in 57 and 58, `1uF 0603`, `56uF`,
  `USB SOT23-5`), each pair with different MPNs: different things, kept as
  separate items. So the migration is one to one and merges nothing.
- **Item page:** the item's fields once, then its stock rows (location,
  ~quantity, − / +, move to another location), and "add to another
  location". Moving a row to a location the item already has adds the
  quantities into one row.
- **Location page:** the stock rows in it and in the locations inside it,
  grouped by location, each showing the item.
- **Quick add:** name, location, quantity, as today; it makes the item and
  its first stock row. A name or part number that matches an existing item
  offers that item instead, so a second box of something adds a stock row,
  not a second item.
- **Search:** one hit per item, listing its locations with their
  quantities. The location filter keeps items with a stock row in that
  location or below it; "in stock only" keeps items with a row whose
  quantity is above 0 or unknown.
- **BOM use (step 7):** on hand is the sum of an item's stock rows, with
  each location listed ("~50: school/57, ~10: home/C3/2").
- **Quantity 0:** a stock row at 0 is kept, a record of where the item
  is stored when there is some (decided 2026-11-24). − stops at 0 and
  leaves the row; removing a row is a separate action on the item page.
  Later, optional maintenance: marking a row obsolete (no longer stocked
  there) or similar. Not planned yet.

## Decided 2026-11-25: how the locations and stock migration runs

- **An explicit command:** `kicadtools inventory migrate [--dry-run]`, run
  from the user's account. Never on open: code with the new layout that
  opens a database still in the old one stops with a message naming the
  command. `meta` gets `inventory_schema = 2` to tell the layouts apart.
- **Shape of the migration:** a top-level location `school`; each bin
  becomes a location under it, keeping its id, so a stock row's
  `location_id` is the old `item.bin_id`; `item` is rebuilt without
  `bin_id` and `quantity`, with `item_tag` and `item_picture` pointing at
  the new table. MariaDB commits each DDL statement on its own, so it is
  not one transaction: a backup first, every new table built before an old
  one is touched, counts checked at the end (on the real data: 122 items,
  113 stock rows, 114 locations), and a run after a failure clears the
  half-built tables and starts again.
- **Cutover on the real database, with the service stopped:** finish the
  release that holds the migration (so `master` has it);
  `sudo systemctl stop kicadtools-web`; `kicadtools inventory migrate` from
  the user's checkout of that release; then the deploy script again, which
  sets the grants (`location`, `stock`, `item_picture`), fetches `master`
  and restarts the service on it (no separate `inventory-web-update`). The
  old release never runs against the new layout.
- **Test copy:** `copy_inventory_to_test.py` copies the real database's
  tables as they are (old layout) into the test database, then runs the
  migration there, so every copy tests it on the real data. Once the real
  database is migrated, the copy finds nothing to migrate.

## Locations and stock — as built (2026-11-25)

- **`inventorydb.py`** holds the new layout only: `location`, `item` (no
  `bin_id`, no `quantity`), `stock`, `item_tag`, `item_picture`, and
  `meta.inventory_schema = 2`. Opening a database still in the first
  layout raises `InventoryLayoutError` naming `kicadtools inventory
  migrate`; so does one whose conversion stopped part way.
- **API:** locations `add_location`, `ensure_location`, `location`,
  `locations`, `children`, `subtree`, `update_location` (description,
  notes), `rename_location`, `move_location`, `delete_location` (refused
  while it holds stock, a row at 0 included, or locations). Items
  `add_item(name, location=, quantity=)` (the first stock row), `item`,
  `items(location)`, `unplaced`, `matching(name=, mpn=, digikey_pn=)`,
  `update_item`, `delete_item`. Stock `add_stock` (adds to a row the item
  already has there), `update_stock`, `adjust_stock` (stops at 0, keeps the
  row), `move_stock` (merges into a row already there), `delete_stock`,
  `stock_in(location)`. `Item.stock`, `Item.on_hand`, `Item.locations`.
- **Reads end their transaction** (`InventoryDb._read`). Found 2026-11-25
  on the test database: with MariaDB's REPEATABLE READ and no autocommit,
  a long-lived connection kept reading its first snapshot, so the web app
  did not see writes from other connections until it wrote itself, and its
  open transaction made a `DROP TABLE` from another connection wait. The
  released app (`kt_v0.1a`) has this bug; it does not matter while the app
  is the only writer.
- **`inventorymigrate.py`:** `plan` (the dry run), `migrate` (as decided
  2026-11-25; the 9 items in no bin get a stock row in `unknown`, user
  decision 2026-11-25), `create_v1` and `copy_v1_rows` (tests, the test
  copy), and the first layout's schema. `db pull` and backups copy an
  unconverted database in its own layout.
- **CLI:** `kicadtools inventory migrate [--dry-run] [--no-backup]`; the
  backup is `~/.kicadtools/backups/before-migrate-<date>.sqlite`, a name the
  Cockpit's pruning does not touch. `kicadtools inventory import FILE
  --location school` (was `--site`).
- **On the real data** (dry run, read only, 2026-11-25): 115 locations
  (`school`, `unknown`, the 113 bins under `school`), 122 items, 122 stock
  rows. The test copy converted it on MariaDB and every item's name, part
  numbers, location and quantity matched the original.
- **Search:** one hit per item with its stock rows; the filter on the
  top-level location (cookie `top`, was `site`) and the location filter keep
  the rows there; "in stock" looks at those rows.
- **Web app:** `/location/<path>` (was `/bin/`), `/locations` (was
  `/bins`), stock rows on the item page (− / +, change, move, "not kept here
  any more", add to another location), quick add offers a matching item.
  Checked against the converted test copy: every page 3–20 ms.
- **Deploy script:** grants on `location` and `stock` instead of `bin`.
- **Tests:** suite 2027 passed on SQLite; the database tests 338 passed on
  SQLite and MariaDB 11.8, including a MariaDB conversion stopped after
  each non-transactional step and finished by the next run.

## Interface to-do (asked 2026-11-25; built 2026-11-25)

Built as below on `develop`. Beyond the list: the item page shows its
pictures (thumbnails, full size opens in a new tab, "Main", delete), and a
result row shows the item's main picture as a thumbnail. Pasting an image
(a screenshot) works like dropping one. The list size is remembered in the
`list_n` cookie and follows the "Where" and "In stock" filters. A copy
copies the stored picture as it is (`InventoryDb.copy_picture`), and opens
the new item's page. Drag and drop (`static/pictures.js`) is not yet
tried in a real browser.


1. **Landing page item list.** Below the information on the search page, a
   scrollable box showing the first *n* items, with a dropdown for *n*:
   10, 20, 50, All.
2. **Selecting an item opens its page.** Clicking anywhere on an item's row
   (in that list and in search results) goes to `/item/{id}`.
3. **Copy item** button on the item page: makes a new item with the same
   fields and tags and opens it to edit. The copy form shows the item's
   existing pictures, each with a checkbox, so any of them can be carried
   over to the copy. Location and quantity start blank (no stock rows are
   copied).
4. **Easy picture adding** on the item page and the new item page: drag
   and drop one or more image files onto the page (and a plain "choose
   files" button, which on the phone also offers the camera). Each upload
   goes through `pictures.prepare` as usual.

## Interface to-do, second list (asked 2026-11-25, after kt_v0.2a)

1. **Locations page as a tree** that expands and collapses, every branch
   collapsed at first. Each location keeps its link and item count; a
   branch opens to show the locations inside it.
2. **Version in the footer** of every page, so it is plain which release
   the service runs (e.g. `kt_v0.2a`). Not yet decided where it comes
   from: the release tag of the checkout (`git describe`, read when the
   app starts) or `__version__` / `pyproject.toml` (both still `0.1.0`,
   never bumped), which would then be bumped at each release.

## Pictures (decided and built 2026-11-25)

An item has any number of pictures, 0 or more, in order; the first is the
main one, the one a list or search result would show.

- **Table** `item_picture`: `id`, `item_id` (FK → item, cascade),
  `position` (0, 1, 2 … kept dense: deleting or moving renumbers),
  `content_type`, `width`, `height`, `data`, `thumb`, `caption`,
  `created_at`. `MEDIUMBLOB` on MariaDB.
- **In the database, not in files:** backups, `db pull` and the test copy
  carry them with nothing more, the Mac reads them through the tunnel, and
  the service (writable only in `.kicadtools`) needs no picture folder. At a
  few hundred kB a picture, a thousand pictures is a few hundred MB.
- **Stored form** (`kicadtools/pictures.py`, Pillow, in the `inventory` and
  `web` extras): any image Pillow reads becomes a JPEG at most 1600 px on
  its longer side, plus a 320 px thumbnail; turned upright by its EXIF
  orientation, then the metadata dropped (a phone photo's EXIF holds where
  it was taken); transparency on white. Uploads over 30 MB or 60 MP are
  refused.
- **InventoryDb:** `add_picture(item_id, image_bytes, caption=)` (adds it
  last), `pictures(item_id)`, `picture(id)`, `picture_data(id, thumb=)`,
  `update_picture(id, caption=)`, `move_picture(id, position)` (0 makes it
  the main one), `delete_picture(id)`. `Item.pictures` is the ids in order,
  `Item.picture` the main one's. `delete_item` deletes them.
- **Released database:** `item_picture` is part of the new layout and
  ships with the locations release; the migration creates it when the
  release had none. The web app's grants add it (deploy script).
- **The item migration** rebuilds `item` on SQLite with foreign keys off,
  so `item_tag` and `item_picture` keep pointing at `item`; on MariaDB
  `item` is altered in place (tested both ways).
- **Not built yet:** the web app's part: pictures on the item page, upload
  from the phone's camera (`<input type=file accept="image/*"
  capture>`), the main picture's thumbnail in search results and on a
  location's page, reorder and delete.

## Decided 2026-11-24: QR codes use a name the user owns

The QR codes do not encode `broadpeak.tail7c4deb.ts.net`. That name is
Tailscale's, and it changes if the tailnet or the machine is renamed or
moved off Tailscale, which would make every printed label wrong. The labels
encode a name under a domain the user controls (e.g.
`inventory.<domain>`), which can point somewhere else later.

- **Prerequisite (the user's):** a nameserver up and running for that
  domain. Before step 6.
- **Labels:** the base URL a QR code encodes is a setting
  (`config.toml` on the service account), not taken from the request and
  not written in the code.
- **What the name resolves to:** broadpeak's tailnet address
  (`100.120.78.89`), so it still answers only to devices in the tailnet.
  Not decided: a public record holding a private address, or the
  nameserver added to the tailnet as split DNS for the domain.
- **Certificate:** `tailscale serve` has a certificate for the `ts.net`
  name only. HTTPS on the new name needs its own certificate, e.g. from
  Let's Encrypt with a DNS-01 challenge against the new nameserver
  (broadpeak's ports 80 and 443 are closed to the internet, so HTTP-01 is
  out), and something on broadpeak that presents it (Caddy, or uvicorn
  with the certificate files). Plain HTTP over the tailnet is the
  alternative: WireGuard already encrypts it, but phones warn about
  `http://` pages.

## Decisions (2026-11-23)

| Topic | Decision |
|---|---|
| Where it is used | Three labs (home, barn, school), each on a different network. The app has to be reachable from all three, and so do the QR codes. |
| Network access | Tailscale (see "Hosting"). |
| Users | Only the user for now. No student accounts. |
| Bins | Free-form names, nested to any depth: a cabinet `C12` holds drawers `C12/3` and `C12/5`. A bin's page and QR code list everything in it and in every bin inside it, so `C12` lists the whole cabinet and `C12/5` the one drawer. A bin is an object of its own with an optional site, description, location ("rack by the bench, top shelf") and notes, each set directly on the bin; a bin with no site takes its parent's. |
| Imported bins | The numbers carry over as top-level bins named `"17"`, `"10001"`, site `school`, with nothing else set. |
| Open-ended values | Nothing is a fixed list: sites, categories, tags, manufacturers and bins are not limited to the values that exist so far. Every such field offers the values already used and takes a new one typed in, in the same box. |
| Quantities | Approximate. Shown as "~50"; − / + buttons adjust them. No exact counting, no transactions. |
| QR labels | Generated by the app, printed on a Brother label printer; the label size is chosen per label, since it depends on the bin. `Libraries/QRCodes` has nothing to do with this. |

## Facts about broadpeak (2026-11-23)

| Item | Finding |
|---|---|
| Machine | Ubuntu 26.04.1, 32 cores, 61 GB RAM, 148 GB free on `/` |
| Installed | MariaDB 11.8 (127.0.0.1 only), Python 3.14, Node 22, git |
| Not installed | Docker, nginx, Caddy |
| Tailscale (2026-11-24) | 1.104.7; tailnet name `broadpeak.tail7c4deb.ts.net`, address `100.120.78.89`. The Mac (`t2864`) is in the tailnet too; from the school network its traffic goes through Tailscale's NYC relay (about 30 ms), not a direct connection |
| Ports | 80 and 443 closed from this Mac; SSH, Samba and a few others open |
| Network | `10.0.10.13`, a private address: reachable from the home network only |
| Sudo | needs a password (the user runs the root steps) |

## Data model

Three new tables in `tadtruex_inventory`, beside the parts tables. They are
separate from `part`: `part` holds design decisions (what a board should
use), the new tables hold what is on a shelf. They are joined by MPN or
DigiKey PN, not by a foreign key, because most shelf items (screws, dev
boards, tools) will never be a `part`.

```
bin                                 item
  id         PK                       id           PK
  parent_id  FK → bin; NULL at top    name         short title: "Nano Every"
  name       "C12", "5", "17"         description  free text
             (no "/")                 category     "Dev board", "Resistor", …
  path       "C12/5": the names       manufacturer
             from the top, unique,    mpn          optional
             case-insensitive         digikey_pn   optional
  site       optional; NULL takes     lcsc         optional
             the parent's             quantity     optional integer, approximate
  description free text; optional     bin_id       FK → bin, optional
  location   free text; optional      url          datasheet or product page
  notes                               notes
  updated_at                          created_at, updated_at
item_tag
  item_id    FK → item (cascade)
  tag        "arduino", "avr"; one row per tag, unique per item, case-insensitive
```

`path` is stored so a QR code's URL (`/bin/C12/5`) finds its bin with one
lookup, and "everything in C12" is the bins whose path is `C12` or starts
`C12/`. Renaming or moving a bin rewrites the paths below it in the same
transaction. A bin is deleted only when it holds no items and no bins.

Only `name` is required for an item. Category and tags are what make
"arduino" find a Nano Every whose name does not say Arduino. Tags are a
table of their own so the tag list for autocomplete is one query and a tag
filter is exact.

The tables are added the way `inductor`, `tht` and `led` were: `CREATE
TABLE IF NOT EXISTS` statements run on every open, within schema version 2,
so no migration is needed and an older KicadTools on another machine keeps
opening the database (it ignores tables it does not know). They are defined
in `kicadtools/inventorydb.py`, which works on an open `PartsDb` (same
connection, same transactions), not in `partsdb.py`. `kicadtools db pull`
and the Cockpit's backups add the three tables, so backups keep covering
everything.

## Search

The inventory is small (hundreds to a few thousand rows), so search runs in
Python over all rows rather than through MariaDB `FULLTEXT`:

- every word typed must match one of name, description, category, tags,
  manufacturer, MPN, DigiKey PN, bin path, site, or the bin's description
  or location, as a
  prefix or substring, ignoring case ("nano" finds "Nano Every", "m6" finds
  "M6 x 5mm");
- typos are tolerated with a small edit distance on longer words
  ("arduiono" finds "arduino"), using `rapidfuzz`;
- results rank name matches above description matches, and items with a
  quantity above items without;
- filters beside the box: site, category, bin, "in stock only". The site
  filter defaults to the last one used, so at school the results show what
  is at school first.

`FULLTEXT` ignores words under 3 characters and does no substring match,
both of which matter here ("M6", "V3"). Search is one function in
`kicadtools/inventory.py` with tests, used by both the web app and the
Cockpit.

## Web app

A small Python web app in this repo (`src/kicadtools/web/`), FastAPI with
server-rendered HTML and htmx, no JavaScript build step:

- **Search page** (the front page): one box, results update as you type,
  each row showing name, ~quantity, bin, site and the start of the
  description. Usable on a phone.
- **Item page**: all fields, edit in place, quantity − / + buttons, "move to
  bin".
- **Quick add**: name, bin, quantity and Save; everything else optional.
  A field for a DigiKey PN or MPN that fills manufacturer, description and
  datasheet from DigiKey (the existing `suppliers.py` lookups), so
  electronic parts are one paste. Opened from a bin's page, the bin is
  filled in.
- **Bin page** (`/bin/C12`, `/bin/C12/5`): the bin's site, description and
  location (editable in place),
  the bins inside it, everything in it and in the bins inside it (grouped by
  bin), "add item here" and "add bin inside". This is the page a QR code
  opens.
- **Bins list**: the bin tree, by site, with item counts.
- **Labels**: pick a bin and a label size, get a label with the bin's path
  in large text and a QR code of the bin page's URL, as an image sized for
  the Brother printer's tape or die-cut label. QR codes are made in the app
  with `segno` (pure Python).
- **Every open-ended field** (site, category, manufacturer, tags, bin) is
  one box that lists matching values already used as you type and keeps
  whatever new value is typed.

The app on broadpeak connects to MariaDB on `127.0.0.1:3306` directly, no
tunnel. It uses its own MariaDB user with rights only on `bin`, `item` and
`item_tag` (read and write) and read on the parts tables, so a bug in the web app
cannot damage the Cockpit's data.

## Hosting on broadpeak

The three labs are on different networks and broadpeak has a private
address, so the app needs a way in from outside the home network:
**Tailscale** (decided).

- broadpeak, the Mac and the phone join one tailnet (free for one user).
  broadpeak gets a fixed name, `broadpeak.<tailnet>.ts.net`, that answers
  from any network the phone or laptop is on, including the school's.
- `tailscale serve` publishes the app at
  `https://broadpeak.<tailnet>.ts.net` with a real certificate that
  Tailscale renews, and only to devices in the tailnet. No port is opened
  on the home router and nothing is on the public internet, so no login
  page is needed while the user is the only user: being in the tailnet is
  the login.
- The QR codes encode a name under the user's own domain that resolves to
  broadpeak in the tailnet, not the `ts.net` URL (decided 2026-11-24, see
  "QR codes use a name the user owns"), so a scan works in any of the
  three labs.
- The same tailnet fixes the Cockpit's database access away from home:
  `config.toml` names `broadpeak` instead of `10.0.10.13`, and the SSH
  tunnel works from the barn and the school (DB plan step 8).
- The app itself: a checkout of the Boards repo under a service account, a
  venv, and a systemd unit running `uvicorn` on `127.0.0.1:8080`, which
  `tailscale serve` forwards to. Updates are `git pull` and
  `systemctl restart`, scripted as one command.

The alternative considered was a public name with a reverse proxy (Caddy)
on an open port 443 and a login page. It works without an app on the phone,
but puts the app on the internet and needs a router port forward at home.

## Steps

1. **Schema.** `bin` (nested, with site, description, location and notes), `item` and
   `item_tag` in `inventorydb.py`, with the operations the later steps need
   (add, edit, rename and move bins; add and edit items; contents of a bin
   and the bins inside it; the values already used in each open-ended
   field), the backup and pull changes; tests on SQLite and MariaDB.
2. **Import.** `kicadtools inventory import FILE [--site school] [--dry-run]`
   reads the Numbers file (`numbers-parser`) or a CSV export: Comp Number →
   bin (one top-level bin per number, named `"17"`, site `school`, nothing
   else set: a bin's description, location and notes are set on the bin
   later), Component Type →
   category (`R` → Resistor, `C` → Capacitor, `L` → Inductor, `D` → Diode,
   `Q` → Transistor, `U` → IC, `VR` → Regulator, `P` → Processor, `Y` →
   Crystal), Value + Package → name (`"4.7k 0603"`), Description and
   Tolerance → description, Approx Stock → quantity, Manufacturer PN → MPN,
   DigiKey PN. Package numbers are written back as text with leading zeros
   (`603.0` → `0603`). Run once; the spreadsheet is retired after.
3. **Search.** `inventory.search(query, filters)` and tests built from real
   examples ("arduino", "stlink v3", "m6 screw", "0.1uf 0603").
4. **Web app, run locally** against `tadtruex_inventory_test` through the
   tunnel: search, item page, quick add, bin page, bins list.
5. **Deploy on broadpeak**: Tailscale on broadpeak, the Mac and the phone;
   service account, MariaDB user, systemd unit, `tailscale serve`. The root
   steps are written as a script for the user to run.
6. **Labels and filling it in**: the labels page with QR codes; DigiKey
   autofill on quick add; optional photo per item.
7. **BOM use**: the Parts tab and LCSC tab look up each part in `item` by
   MPN or DigiKey PN, and for passives by value and package with the
   existing parsers in `units.py`; the row says "on hand: ~50, bin 17
   (school)", and the BOM export can leave on-hand parts out of the order.

Steps 1–5 make the lookup usable from all three labs; 6 makes labelling and
adding things fast; 7 is the BOM use.

## Step 1 — as built (2026-11-23)

- `src/kicadtools/inventorydb.py`: the `bin`, `item` and `item_tag` tables on
  both engines, and `InventoryDb(parts_db)` with:
  - bins: `add_bin`, `ensure_bin` (makes missing levels: `"C12/5"` makes
    `C12` too), `bin`, `bins`, `children`, `subtree`, `site_of`,
    `update_bin`, `rename_bin`, `move_bin` (refuses a move inside itself),
    `delete_bin` (refuses a bin holding items or bins);
  - items: `add_item` (only a name required; a bin path that does not exist
    is made), `item`, `items(bin, recursive=True)`, `unbinned`,
    `update_item`, `adjust_quantity` (never below zero), `delete_item`;
  - values used so far: `values("category" | "manufacturer" | "site")`,
    `tags()`.
- A bin is a row of its own with a description, a location and notes, set
  with `add_bin` or later with `update_bin(path, description=…)`.
  `description` was added after the tables were first created on the
  server; `InventoryDb` adds a column a table lacks when it opens
  (`_ADDED_COLUMNS`), so an older table gains it with its rows kept.
- Free text (description, location, notes, URL) keeps its line breaks and
  loses only surrounding whitespace. Names, categories, manufacturers, part
  numbers, sites and tags are cleaned of surrounding and repeated
  whitespace. Paths, sites, categories, manufacturers, part numbers and
  tags compare ignoring case. Lengths are checked against the MariaDB columns on both
  engines. Bins and items sort naturally (`2` before `10`).
- `InventoryDb` has its own transaction that nests: the methods call one
  another (`add_item` → `ensure_bin` → `add_bin`), and only the outermost
  block commits, so a failure rolls back the bins it made too.
- `kicadtools db pull` and the Cockpit's backups copy the three tables when
  the source has them, parents before children.
- Tests: `tests/test_inventorydb.py` (25 tests), including that a second
  connection sees every change at once and nothing of a failed one. On
  SQLite and on MariaDB 11.8 (`tadtruex_inventory_test` on broadpeak).
  Suite: 1920 passed on SQLite; the database tests 191 passed on both.
- The tables are created on a database the first time anything opens an
  `InventoryDb` on it; on `tadtruex_inventory` that was the step 2 dry run.

## Step 2 — as built (2026-11-23)

- `src/kicadtools/inventoryimport.py`: `read_table` (`.numbers` through
  `numbers-parser`, the optional `inventory` extra, or a `.csv` export),
  `convert` (columns found by header, ignoring case and spacing),
  `import_items` (one transaction; refuses an inventory that holds any bin
  or item; `dry_run` does everything and rolls back).
- `kicadtools inventory import FILE --site SITE [--dry-run]`, on the
  configured parts database (the server, or a SQLite file).
- Conversions beyond the plan, from what the sheet holds:
  - a resistor value stored as a number gets `Ω` (`330.0` → `330Ω 0603`);
    one written as text keeps its spelling (`4.7k 0603`);
  - Tolerance holds a capacitor's dielectric (`X7R`) or a digipot's step
    count (`256 Steps`) as text, kept; a fraction is a percentage;
  - a number in Description is a capacitor's voltage or an inductor's
    current (`50.0` → `50V`, `3.5` → `3.5A`);
  - a part number stored as a number keeps every digit
    (`860160674023`, not `8.60161e+11`);
  - CSV text converts the same as the Numbers cells (`"603"` → `0603`).
- Bins: one top-level bin per Comp Number, with the site given and nothing
  else. A bin is a row of its own; its notes and location are set later with
  `update_bin(path, …)`, one update by path.
- Dry run against `tadtruex_inventory` (2026-11-23): 122 items, 113 bins,
  9 items in no bin, rolled back; the three tables were created and are
  empty.
- Imported into `tadtruex_inventory` (2026-11-23), `--site school`: 122
  items, 113 bins (`1`–`142` with gaps, `10001`, `10004`), 9 items in no
  bin; no bin has a description, location or notes yet. Read back from a
  new connection. The server's `bin` table, created by the dry run before
  `description` existed, gained the column when the import opened it.
- Tests: `tests/test_inventoryimport.py` (18 tests, including the real file
  when it is checked out). Suite 1938 passed on SQLite; the database tests
  226 passed on SQLite and MariaDB 11.8.
- Left as the sheet has them: bin 19 (`1M 0603`) has Tolerance `1%%` and a
  DigiKey PN for a 10k part (`RMCF0603JG10K0CT-ND`, MPN `RMCF0603FT1M00`);
  bin 3's MPN reads `CR003-JW-331ELF` beside DigiKey `CR0603-JW-331ELFCT-ND`.
  These are fixed in the app after the import.

## Step 3 — as built (2026-11-24)

- `src/kicadtools/inventory.py`: `search(inv, query, *, site=None,
  category=None, bin=None, in_stock=False)` reads every item and bin once
  and calls `search_items(items, bins, query, …)`, which needs no database
  (the web app can call either). Returns `SearchHit`s: the item, its `Bin`,
  its site (own or inherited), the score and the fields that matched.
- Matching beyond the plan:
  - besides the plain substring, the word and the field are compared with
    everything but letters and digits removed (`stlink` finds `ST-LINK/V3`);
  - `µ`/`μ` read as `u` and `Ω` as `ohm` (`0.1uf` finds `0.1µF`);
  - typos only on words of 5 or more letters, never on a word with a digit:
    one edit turns `100nf` into `10nF` (seen on the real data before the
    rule);
  - the description and location of the item's bin **and of every bin it is
    inside** are searched, so a drawer is found by its cabinet's location.
- Ranking: field weights name 10, tags and category 6, part numbers 5,
  manufacturer 4, bin path 3, site and bin text 2, description 1; a typo
  match counts half. Ties: items with a quantity first, then by name. An
  empty query lists everything the filters allow, by bin then name.
- Filters ignore case; `bin` includes the bins inside it; `in_stock` drops
  quantity 0 and keeps an unknown quantity; an item in no bin has no site.
- `rapidfuzz` added to the `inventory` extra.
- Tests: `tests/test_inventory_search.py` (13 tests, SQLite and MariaDB).
  Suite 1958 passed on SQLite. On `tadtruex_inventory` (read only): about
  5 ms a query; `0.1uf 0603` → bins 57, 58; `stm32` → 4; `usb hub` → 2;
  `100nf` → nothing (`0.1uF` and `100nF` are not yet the same value).

## Step 4 — as built (2026-11-24)

- `src/kicadtools/web/`: `app.py` (`create_app(inventory_or_opener)`), Jinja
  templates, `static/style.css` (light and dark), and htmx 2.0.4 vendored
  as `static/htmx.min.js`, so the pages need nothing from the internet.
  The `web` extra: FastAPI, uvicorn, Jinja2, python-multipart, rapidfuzz,
  httpx (tests only).
- Run: `kicadtools-web [--config FILE] [--host H] [--port 8080] [--reload]`,
  on the database the config names; `uvicorn --factory
  kicadtools.web.app:configured_app` does the same.
- Pages as planned: search (htmx as you type, plain GET without JS; "Nothing
  found" links to quick add with the name filled in), item (every field, −
  / +, bin box to move it; a new bin path makes the bin), quick add (name,
  bin, quantity, the rest under "More fields"; after Save the form comes
  back empty with the same bin, for the next item), bin page (contents of
  it and every bin inside, grouped; edit, add bin inside, rename, move,
  delete when empty; a missing bin's page offers to make it), bins list (by
  site, with counts, and the items in no bin). `/healthz` for the service.
- Open-ended fields are `<input list>` with a `<datalist>` of the values
  already used: the browser's own "pick one or type a new one", no JS.
  Tags are one comma-separated box.
- The site filter is remembered in a cookie (a year); the search page with
  a remembered site offers "search every site" when nothing is found.
- One database connection for the app, used under `PartsDb`'s lock from
  FastAPI's thread pool; `PartsDb` pings it after 60 s idle.
- No login and no CSRF tokens: the plan's access control is the tailnet
  (step 5). Until then it listens on 127.0.0.1 only.
- Tests: `tests/test_web.py` (22 tests, SQLite). Suite 1980 passed.
- Live on broadpeak against `tadtruex_inventory_test` holding a copy of
  the real bins and items: every page 5–18 ms.
- Not built from the step 4 list: nothing. From later steps: DigiKey
  autofill on quick add and labels (step 6).

## Step 5 — as built (2026-11-24)

- `scripts/deploy_inventory_web.sh`, run once by the user with sudo from
  their checkout on broadpeak; safe to run again. It installs
  `scripts/inventory_web_update.sh` as `/usr/local/sbin/inventory-web-update`.
- **Release, not develop:** the service runs `master`, the current
  git-flow release (`kt_v0.1a` at deploy). `sudo inventory-web-update`
  fetches `master` and its tags, reinstalls, restarts, checks `/healthz`
  and prints the release before and after.
- **Account:** system user `inventory`, home `/srv/inventory` (mode 750,
  closed to `tad`), no login shell. GitHub access through a read-only
  deploy key on the Boards repo (the repo is private); GitHub's host key is
  pinned in the script.
- **Checkout:** `/srv/inventory/Boards`, depth 1, blobs on demand, sparse:
  only `Code/KicadTools` (about 5 MB). `KICADTOOLS_REPO_ROOT` in the unit
  points `repo_root()` at it, so no `Libraries` folder is needed. Venv
  `/srv/inventory/venv` with the `server,inventory,web` extras.
- **Database user** `inventory_web@localhost`, password random, in
  `/srv/inventory/.kicadtools/db_password` (0600); config beside it names
  `tadtruex_inventory` on port 3306 (no tunnel). Grants: SELECT on the
  database; INSERT, UPDATE, DELETE on `bin`, `item`, `item_tag`; plus
  CREATE, INDEX and REFERENCES on the database and INSERT on `meta`.
  The extra four are beyond the plan: opening the database runs `CREATE
  TABLE` / `CREATE INDEX IF NOT EXISTS` and `INSERT IGNORE INTO meta`, and
  MariaDB checks privileges before it checks whether the table exists.
  None of them can drop, alter or change rows in an existing table. The
  user has no ALTER: a release that adds a column (`_ADDED_COLUMNS`) needs
  the database opened once from the user's own account before the update.
- **Backup** before the first start: `mariadb-dump` of `tadtruex_inventory`
  to `/srv/inventory/backups/` (root only).
- **Service:** systemd unit `kicadtools-web`, enabled at boot, on
  `127.0.0.1:8090`. The plan said 8080; that port stays free for a
  development run of `kicadtools-web`. `ProtectSystem=strict`,
  `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`; writable only
  `/srv/inventory/.kicadtools`.
- **Tailscale:** `tailscale serve --bg --https=443` forwards
  `https://broadpeak.tail7c4deb.ts.net/` to it, tailnet only, with a Let's
  Encrypt certificate Tailscale renews (valid to 2027-02-22 at deploy).
- Checked from the Mac over the tailnet: `/healthz` ok, search (`stm32` →
  4 items) and the bins list, 0.14–0.23 s a page through the relay.
- Not done from the step 5 list: the phone (Tailscale on it, then a look
  at the pages). The Cockpit's `config.toml` naming `broadpeak` instead of
  `10.0.10.13` (DB plan step 8) is now possible but not done.

## Open questions

1. **Brother model.** Which one (a QL with die-cut labels or continuous
   tape, or a P-touch with TZe tape)? That sets the label sizes offered and
   whether the app can print to it directly (`brother_ql`) or produces an
   image to print from the Mac.
2. **Boxes 1–142.** Do they sit in something worth making a bin (a rack or
   a cabinet at school)? They were imported top-level; `move_bin` can put
   them inside one at any time.
3. ~~**The free-text `location` field.**~~ Decided 2026-11-24: it goes;
   the location's `description` holds it.
4. **The QR code name.** Which domain and name, whether it resolves through
   public DNS or the tailnet's split DNS, and how HTTPS works on it (see
   "Decided 2026-11-24: QR codes use a name the user owns").
