# Trying develop beside the released app

The released inventory app and a development run of `develop` can run on
broadpeak at the same time. They share the MariaDB server and nothing else.

| | Released | Develop |
|---|---|---|
| Code | `/srv/inventory/Boards` (`master`) | `~/Documents/Code/Boards` (`develop`) |
| Port | 127.0.0.1:8090 | 127.0.0.1:8080 |
| Database | `tadtruex_inventory` (real) | `tadtruex_inventory_test` |
| Config | `/srv/inventory/.kicadtools/config.toml` | `~/.kicadtools/config_test.toml` |
| Address | `https://broadpeak.tail7c4deb.ts.net` | `https://broadpeak.tail7c4deb.ts.net:8443` (when published, below) |

## The one rule

**A develop run always names the test config.** Never start it on the
default `~/.kicadtools/config.toml`: that is the real database, with your
own account's full rights. Opening a database runs the schema statements of
the code doing the opening, so develop code with new tables or columns
would add them to the database the release is using.

## One-time setup

```bash
sed 's/^database = .*/database = "tadtruex_inventory_test"/' \
    ~/.kicadtools/config.toml > ~/.kicadtools/config_test.toml
chmod 600 ~/.kicadtools/config_test.toml
grep '^database' ~/.kicadtools/config_test.toml   # must end _test
```

Same user and password as the real database (`db_password`).

## Each time

On broadpeak, in `~/Documents/Code/Boards/Code/KicadTools`:

```bash
git switch develop && git pull
. .venv/bin/activate && pip install -q -e ".[dev,server,inventory,web]"

# 1. Fresh copy of the real data into the test database (only reads the real one)
python scripts/copy_inventory_to_test.py ~/.kicadtools/config_test.toml

# 2. Start the develop app (Ctrl-C stops it; --reload restarts on code changes)
kicadtools-web --config ~/.kicadtools/config_test.toml --reload
```

Step 1 opens the real database **read-only**: no schema statements run on
it and the server refuses any write, so develop code cannot change it. While
the real database still has the release's layout (bins), the copy takes its
tables as they are and then converts the test copy with the same
`inventory migrate` code the cutover will use, so every copy is a trial of
the conversion on the real data. Once the real database is converted, the
copy takes the new tables directly.

**Stop a develop app before step 1.** The copy drops and remakes the test
database's inventory tables, and MariaDB makes a `DROP TABLE` wait for every
connection that still has the table open in a transaction. The develop app
no longer keeps one open between requests (fixed 2026-11-25), but a
release from before that fix does, and so would any other long-lived
connection.

## Looking at it

- **From the Mac:** `ssh -L 8080:127.0.0.1:8080 tad@broadpeak.tail7c4deb.ts.net`,
  then http://localhost:8080.
- **From the phone** (or any tailnet device): publish it on a second port,
  then open `https://broadpeak.tail7c4deb.ts.net:8443`. The release stays
  on the plain address.

  ```bash
  sudo tailscale serve --bg --https=8443 http://127.0.0.1:8080   # publish
  sudo tailscale serve --https=8443 off                          # remove
  tailscale serve status                                         # what is published
  ```

## Things that reset the test database

- The MariaDB test run (`KICADTOOLS_TEST_DB_NAME=tadtruex_inventory_test
  pytest …`, see `NOTES.md`) drops every table in it. Run step 1 again
  afterwards.
- Step 1 empties and refills its inventory tables, so anything entered in
  the develop app is lost.

## The released app

| Task | Command |
|---|---|
| Update to the head of `master` | `sudo inventory-web-update` |
| Status, log | `systemctl status kicadtools-web`, `journalctl -u kicadtools-web -f` |
