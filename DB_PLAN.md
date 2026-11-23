# Parts database on a shared server — plan

Move the accepted-parts database from the machine-local `~/Code/Boards/parts.sqlite`
to the MariaDB database on the Reclaim Hosting account. The server is the
source of truth: the Cockpit reads and writes it directly, on every machine,
with nothing to push or sync. A machine can be lost and work continues from
any workstation with KicadTools installed, the database password and an SSH
login to the host.

Local copies exist only as backups and as the read-only offline copy
(decision 1); nothing is ever written to them by the Cockpit except a fresh
download.

Status (2026-11-23): steps 1–7 built; the real import (step 5) not yet run. Steps 8–9 not started.

## What was checked (2026-11-23)

| Item | Finding |
|---|---|
| Server | MariaDB 10.6.27 on `dartmouth01.reclaimhosting.com` (`tadtruex.host.dartmouth.edu`) |
| Direct connection, port 3306 | Port open, login refused (`1045 Access denied … @'129.170.41.125'`) from the Mac and the Linux box; the same login works in a shell on the server. Remote access is not working; whether the host allows it at all was not established. |
| SSH tunnel | Works: `ssh -N -L 3307:127.0.0.1:3306 tadtruex@tadtruex.host.dartmouth.edu`, key login, no password prompt. The server sees `tadtruex_inventory@localhost`. |
| Privileges | `ALL PRIVILEGES ON tadtruex_inventory.*` |
| Engine | InnoDB (transactions, foreign keys, `ON DELETE CASCADE`) |
| Character set | Server default `latin1`. A table created with `utf8mb4` stored and returned `µF ± Würth` unchanged. |
| `sql_mode` | `STRICT_TRANS_TABLES, …`: an over-long or missing value is an error, not a silent truncation |
| `wait_timeout` | 28800 s: an idle connection is closed after 8 hours |
| Driver | PyMySQL 2.2 (pure Python, no client library) |

Data to move today: 47 parts (15 capacitors, 14 resistors, 2 inductors,
5 ICs, 9 through-hole, 2 LEDs), 83 supplier part numbers, 121 usage rows,
11 settings, 1 rotation override.

## Scope

Moves to the server:

- every table of `partsdb.py`: `meta`, `settings`, `part`, `capacitor`,
  `resistor`, `inductor`, `ic`, `tht`, `led`, `supplier_pn`, `usage`;
- `part_rotation` (`partrotation.py`, now in `~/.kicadtools/lcsc_cache.sqlite`):
  the overrides are user decisions, and the memory note on rotation
  transportability asks for exactly this.

Stays local:

- the supplier caches in `~/.kicadtools/lcsc_cache.sqlite` (`dk_to_mpn`,
  `mpn_to_lcsc`, `jlc_part`): they are caches of supplier answers, rebuilt
  on demand, and each machine's copy can differ without harm;
- the DigiKey call counter.

## Decisions (2026-11-23)

1. **Offline: a read-only local copy, impossible to miss.** When the server
   cannot be reached (no network, the tunnel fails) and a local copy exists,
   the Cockpit opens it read-only:
   - a red bar across the top of the window: `OFFLINE — read-only copy of
     the parts database from 2026-11-23 14:02. Accept, Reject and
     Autopopulate are off. Retry connection`;
   - the window title ends `[OFFLINE]`, and the status bar names the copy;
   - stored parts are found and supplier searches run, but Accept, Reject,
     Accept Checked and settings changes are disabled, so no decision is
     made that the server would not hold;
   - **Retry connection** reconnects without restarting; on success the bar
     goes away and the buttons are enabled.

   The local copy is the newest backup (step 7), downloaded on each
   successful start. With no copy, the Parts tab lists requirements and
   cannot search stored parts or accept, and the bar says so.
2. **The Cockpit opens the tunnel** (`ssh -N -L`) on a free local port when
   it opens the database and stops it on exit. With a key on the server
   (this Mac today) it needs no prompt. On a machine with no key, ssh asks
   for the SSH password through a dialog (`SSH_ASKPASS` pointing at a small
   Qt prompt in the Cockpit), so a fresh workstation needs no key setup.
3. **Password: a file outside git.** `~/.kicadtools/db_password`, one line,
   mode `0600`. `~/.kicadtools` is in no repository. When the file does not
   exist, the Cockpit asks for the password, checks it with a login, and
   writes the file. A rejected password gives the prompt again, and the
   file is rewritten. `KICADTOOLS_DB_PASS` overrides the file (tests,
   one-off runs). No keychain: the machines run GNOME and KDE, and one file
   works the same on all of them. The password is never in the repo, the
   config file or a log line.
4. **Settings are shared.** The `settings` table holds the remembered
   defaults (`cap.voltage_min`, `cap.package`, the tolerance defaults) and
   the maker lists: `cap.manufacturers`, `res.manufacturers`,
   `ind.manufacturers`, `led.manufacturers` and `hdr.manufacturers`, the
   manufacturers each search keeps (a supplier hit from a maker not listed
   is left out). A default or a maker added on one machine applies on every
   machine.

## Design

### Configuration

`~/.kicadtools/config.toml` (not in the repo):

```toml
[parts_db]
backend = "mariadb"          # "sqlite" (today's behaviour) or "mariadb"
database = "tadtruex_inventory"
user = "tadtruex_inventory"

[parts_db.ssh]
host = "tadtruex.host.dartmouth.edu"
user = "tadtruex"
remote_port = 3306
```

`KICADTOOLS_PARTS_DB` keeps its meaning (a SQLite path) and wins over the
file, so tests and one-off runs need no config. With no config file the
Cockpit behaves as today.

### One database class, two backends

`PartsDb` keeps its public methods (`accept_part`, `find_parts`,
`find_ics`, `find_tht`, `find_leds`, `record_usage`, settings), so no caller
changes. The SQL that differs between SQLite and MariaDB moves behind a
small dialect object:

| SQLite today | MariaDB |
|---|---|
| `?` placeholders | `%s` |
| `INTEGER PRIMARY KEY` (row id) | `INT AUTO_INCREMENT PRIMARY KEY` |
| `TEXT` in keys and indexes | `VARCHAR(n)`: MariaDB indexes need a length (`mpn`, `manufacturer` 128; `pn` 100; `signature` 255) |
| `REAL` | `DOUBLE` |
| `COLLATE NOCASE` on `mpn`, `manufacturer` | `utf8mb4_unicode_ci` on those two columns (case-insensitive); every other column `utf8mb4_bin`, compared as bytes, as SQLite compares them |
| `INSERT … ON CONFLICT(…) DO UPDATE` | `INSERT … ON DUPLICATE KEY UPDATE` |
| `INSERT OR REPLACE` | `INSERT … ON DUPLICATE KEY UPDATE`. MariaDB's `REPLACE` deletes the row first, and `ON DELETE CASCADE` would then delete the part's child rows. |
| `INSERT OR IGNORE` | `INSERT IGNORE` |
| `PRAGMA foreign_keys = ON` | on by default in InnoDB |
| `executescript(_SCHEMA)` | one `execute` per statement |
| `cursor.lastrowid` | the same attribute in PyMySQL |

`_Dialect` (SQLite) and `_MariaDialect` build these; the rest of
`partsdb.py` is standard SQL and runs on both.

`utf8mb4_unicode_ci` also ignores accents (`Würth` equals `Wurth`). For a
manufacturer name that matches suppliers that spell it either way; the
unique key on (manufacturer, MPN) then treats the two spellings as one
part. That is the intended behaviour; it is listed here because SQLite's
`NOCASE` does not do it.

`key` and `usage` are reserved words in MariaDB. Each MariaDB connection
sets `ANSI_QUOTES`, and the shared SQL writes `"key"` and `"usage"`, which
both engines read as names.

Every table is created with `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_bin`, and the database default is changed once to
`utf8mb4` (`ALTER DATABASE`), so a table created later by hand is UTF-8 too.

### Connections

- The GUI thread holds one connection; each search worker opens its own, as
  it does today (`PartsDb(self._db_path)` in `gui/parts.py`, four places).
  A worker's connection through the tunnel costs one round trip to open.
- Before each use after an idle period, `conn.ping(reconnect=True)`, so the
  8-hour `wait_timeout` closes nothing the Cockpit needs.
- Writes stay in one transaction per accept, as today (`PartsDb._tx`).

### Tunnel (decision 2a)

`kicadtools/dbtunnel.py`: start `ssh -N -o BatchMode=yes -o
ExitOnForwardFailure=yes -L 127.0.0.1:<free port>:127.0.0.1:3306 user@host`,
wait until the local port accepts, return the port; stop the process on
exit. A failure (no key, host unreachable) becomes the offline case of
decision 1. An existing tunnel the user runs is used when the config names
a fixed `local_port` and that port answers.

### Schema version

`meta.schema_version` stays the check it is today. The server starts at
version 2 (today's schema). A later change adds a version and a migration
step that runs in one transaction, as the version-1 → 2 step does now.

## Steps

1. **Dialect layer, SQLite only.** Done. `partsdb._Dialect` builds the
   upserts (`INSERT … ON CONFLICT … DO UPDATE` in place of `INSERT OR
   REPLACE`), `INSERT OR IGNORE`, the table-exists query, the schema
   statements and the connection setup; every statement runs through
   `PartsDb._execute` / `_executemany`, and transactions through
   `PartsDb._tx` (PyMySQL's `with conn:` closes the connection rather than
   committing). `find_ics` compares `norm_mpn` in Python: MariaDB cannot
   call a Python function from SQL. The suite passes unchanged, and the old
   and new code give the same results on a copy of `parts.sqlite` (58
   checks, including an accept / reject / usage / settings sequence
   compared table by table).
2. **MariaDB dialect and DDL.** Done. `partsdb._MariaDialect` and
   `_MARIA_SCHEMA`; `PartsDb.connect_mariadb(host, port, user, password,
   database)` opens the server with the same methods as a file, and
   `PartsDb.backend` names the engine. PyMySQL is the optional `server`
   extra. `tests/dbbackends.py` runs `test_partsdb.py` and `test_thtdb.py`
   on SQLite and, when `KICADTOOLS_TEST_DB_NAME` is set, on MariaDB; it
   refuses a database name not ending `_test` and drops that database's
   tables before each test. On `tadtruex_inventory_test` through the
   tunnel: 118 passed, 8 SQLite-only skipped (about 90 s); the real
   `tadtruex_inventory` was left empty. The test command:

   ```
   KICADTOOLS_TEST_DB_NAME=tadtruex_inventory_test \
   KICADTOOLS_TEST_DB_USER=tadtruex_inventory \
   KICADTOOLS_DB_PASS=… pytest tests/test_partsdb.py tests/test_thtdb.py
   ```
3. **Configuration and credentials.** Done. `kicadtools/dbconfig.py`:
   `load_config` reads `~/.kicadtools/config.toml` (`KICADTOOLS_CONFIG`
   names another file; `KICADTOOLS_PARTS_DB` wins over both) into a
   `DbConfig`, with a `ConfigError` naming the file and the missing field.
   `obtain_password(check, ask)` tries `KICADTOOLS_DB_PASS`, else
   `~/.kicadtools/db_password`; when there is none or the server refuses it
   (MariaDB errors 1044, 1045) it asks, checks the answer, and writes the
   file (mode `0600`, set before the password is written). An unreachable
   server raises without asking. `PartsDb.check_mariadb_login` is the check
   (a login that creates nothing); `gui/db_password.py` (a password dialog)
   and `dbconfig.terminal_ask` are the two *ask*s. Checked against the
   server through the tunnel: a wrong answer was refused and asked again, the
   right one saved, and the next run asked nothing.
4. **Tunnel.** Done. `kicadtools/dbtunnel.py`: `Tunnel(host, user).open()`
   starts `ssh -N -L 127.0.0.1:<free port>:127.0.0.1:3306` and returns the
   port once it answers; `close()` stops ssh. With a configured
   `local_port` that already answers, the user's tunnel is used and left
   running. With no *askpass*, ssh runs with `BatchMode=yes`; with one
   (`gui/askpass.py`, through the wrapper `askpass_script` writes), ssh asks
   for the SSH password in a dialog (`SSH_ASKPASS_REQUIRE=force`). New host
   keys are accepted on first use; `ServerAlive` probes end a dead tunnel.
   A failure is a `TunnelError` with ssh's message, without its
   post-quantum warning. Checked live: up in 0.4 s with the key, a login
   through it, closed cleanly; the 3307 tunnel adopted and left running.
   The askpass dialog is exercised on a machine with no key (step 8).
5. **One-time import.** `kicadtools db import [--dry-run]`, run once when
   the server is set up: load today's `parts.sqlite` and the rotation
   overrides into the empty server in one transaction, keeping part ids so
   `usage` and `supplier_pn` rows still point at their parts. It refuses
   when the server already holds parts, and prints both sides' row counts.
   After it runs, `parts.sqlite` is renamed `parts.sqlite.imported` so no
   tool opens it again by mistake. There is no ongoing push: every later
   change is written to the server when it is made.

   Built: `kicadtools/dbimport.py` (`import_parts`), `kicadtools/dbconnect.py`
   (`open_parts_db`: tunnel, password, connection; the Cockpit uses it in
   step 6) and `kicadtools db import [--from PATH] [--cache PATH]
   [--dry-run]`. The rotation overrides have a `part_rotation` table in the
   parts database on both engines, with `PartsDb.rotation`, `set_rotation`
   and `rotations`. After the copy every table is read back and compared
   row by row; a difference, or a value the server's columns refuse, rolls
   everything back. Tests on SQLite and MariaDB. Dry run against
   `tadtruex_inventory` (2026-11-23): every table's server count matched the
   local one (47 parts, 83 supplier numbers, 121 usage rows, 11 settings,
   1 rotation), rolled back; the server's tables were created and are empty.
   The real import is run when step 6 is in, so the Cockpit never opens a
   new empty local file after `parts.sqlite` is renamed.
6. **Cockpit.** Open the server; the status bar names it (`parts DB:
   tadtruex_inventory (server)`); the offline behaviour of decision 1; the
   rotation store on the same backend.

   Built: `MainWindow._open_database` opens the configured database through
   `dbconnect.open_parts_db` (tunnel with the `gui.askpass` dialog, password
   with the `gui.db_password` dialog); search workers get their own
   connection through the same tunnel (`DbSession.connect_again`, passed as
   `open_db`). Offline (`_go_offline`): the newest
   `~/.kicadtools/backups/parts-*.sqlite` opened with
   `PartsDb.open_read_only`, the red bar with the copy's date, the reason
   and **Retry connection**, `[OFFLINE]` in the title, Autopopulate off,
   Accept and Reject refused with a message, rotation edits refused; a
   connection lost while accepting or rejecting switches to it. A server
   connection idle over 60 s is pinged (and its session settings reapplied
   after a reconnect) before use. Rotations: `partrotation.DbRotationStore`
   over the parts database when the configuration names the server, the
   local cache otherwise; `kicadtools cpl` reads the server's and fails when
   it cannot be reached. Tests: `tests/test_gui_dbstate.py` (offline with
   and without a copy, Retry, online, SQLite). Checked live against
   `tadtruex_inventory_test`: the window opened its own tunnel, a rotation
   written through the LCSC store was read back by a worker's connection,
   and quitting closed the tunnel.
7. **Backups and the offline copy.** `kicadtools db pull [PATH]` writes the
   server's data to a SQLite file. The Cockpit runs it on each successful
   start into `~/.kicadtools/backups/parts-<date>.sqlite`, keeping the last
   10; the newest is the offline copy. Reclaim also keeps its own backups of
   the account.

   Built: `kicadtools/dbbackup.py`. `pull(db, path)` reads every table in
   one transaction (one consistent snapshot on InnoDB), writes a new SQLite
   file under a `.partial` name and renames it when complete.
   `backup_now(db)` writes `parts-YYYY-MM-DD-HHMMSS.sqlite` (the time added,
   so several starts a day each keep theirs) and keeps the newest 10. The
   Cockpit runs it after each successful start on the server (and after a
   Retry that connects) and says so in the status bar; a failed backup is
   reported there and blocks nothing. `kicadtools db pull [PATH]`. Tests on
   SQLite and MariaDB; a live pull from `tadtruex_inventory_test` gave a
   readable version-2 file.
8. **Second machine.** Install on the Linux box, run the Cockpit against
   the server (SSH password prompt first, then a key if wanted), and check
   that a part accepted on one machine is found on the other at once.
9. **Offline check.** Stop the network on one machine: the red bar, the
   disabled buttons and Retry behave as decision 1 says.

## Not in this plan

- Moving the supplier caches.
- A web front end or API on the host.
- Multi-user access (other people's accounts and permissions).
