#!/usr/bin/env bash
# Deploy the lab inventory web app on broadpeak (INVENTORY_PLAN.md step 5).
#
#   sudo bash scripts/deploy_inventory_web.sh
#
# Run it from a checkout of this repo on broadpeak. Safe to run again: every
# step checks what is already there. What it sets up:
#
#   1. a system account `inventory` (home /srv/inventory, no login shell);
#   2. a read-only GitHub deploy key for that account (you add it on GitHub
#      when the script prints it; the script waits);
#   3. a shallow, sparse checkout of master (git-flow: the current
#      release) in /srv/inventory/Boards and a
#      venv in /srv/inventory/venv with the server, inventory and web extras;
#   4. a backup of tadtruex_inventory (mariadb-dump) before the app can
#      write to it;
#   5. a MariaDB user `inventory_web`@`localhost`: SELECT on every table,
#      INSERT/UPDATE/DELETE on bin, item, item_tag and
#      item_picture only. Opening the
#      database runs CREATE TABLE / CREATE INDEX IF NOT EXISTS and
#      INSERT IGNORE INTO meta, and MariaDB checks privileges before it
#      checks whether the table exists, so the user also holds CREATE,
#      INDEX and REFERENCES on the database and INSERT on meta. None of
#      these can drop, alter or change rows in an existing table.
#      The password is random and stored in
#      /srv/inventory/.kicadtools/db_password (0600, owner inventory);
#   6. a systemd unit `kicadtools-web` on 127.0.0.1:8090 (8080 stays free
#      for a development run);
#   7. `tailscale serve`: https://<broadpeak's tailnet name>/ forwards to it;
#   8. /usr/local/sbin/inventory-web-update: pull master, reinstall,
#      restart, check.
#
# After a schema change that adds a column (inventorydb._ADDED_COLUMNS),
# open the database once with your own account (any kicadtools command, or
# a development run of kicadtools-web) before running the update: the web
# app's user cannot ALTER tables.

set -euo pipefail

SVC_USER=inventory
SVC_HOME=/srv/inventory
CHECKOUT=$SVC_HOME/Boards
VENV=$SVC_HOME/venv
KT_DIR=$SVC_HOME/.kicadtools
REPO_URL=git@github.com:tadtruex/Boards
BRANCH=master   # git-flow: master is the current release
DATABASE=tadtruex_inventory
DB_USER=inventory_web
PORT=8090
UNIT=kicadtools-web
UPDATE_CMD=/usr/local/sbin/inventory-web-update
BACKUP_DIR=$SVC_HOME/backups
# GitHub's published SSH host key (docs.github.com, "GitHub's SSH key
# fingerprints"), pinned so the first clone does not trust whatever answers.
GITHUB_HOST_KEY='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }
as_svc() { sudo -u "$SVC_USER" -H -- "$@"; }

[[ $EUID -eq 0 ]] || fail "run with sudo: sudo bash $0"
command -v mariadb   >/dev/null || fail "mariadb client not found"
command -v tailscale >/dev/null || fail "tailscale not found"
command -v python3   >/dev/null || fail "python3 not found"
command -v git       >/dev/null || fail "git not found"
tailscale status >/dev/null 2>&1 || fail "tailscale is not up (tailscale up)"

# --- 1. service account ---------------------------------------------------
say "service account $SVC_USER"
if id "$SVC_USER" >/dev/null 2>&1; then
    echo "exists"
else
    useradd --system --home-dir "$SVC_HOME" --create-home \
            --shell /usr/sbin/nologin --user-group "$SVC_USER"
    echo "created"
fi
chmod 750 "$SVC_HOME"
# Every later step runs from here: started from your checkout, commands run
# as $SVC_USER would have a working directory they cannot read.
cd "$SVC_HOME"
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$SVC_HOME/.ssh" "$KT_DIR"
install -d -m 750 -o root -g root "$BACKUP_DIR"

# --- 2. deploy key -------------------------------------------------------
say "GitHub deploy key"
KEY=$SVC_HOME/.ssh/id_ed25519
if [[ ! -f $KEY ]]; then
    as_svc ssh-keygen -q -t ed25519 -N '' -C "inventory@broadpeak (deploy, read-only)" -f "$KEY"
fi
KNOWN=$SVC_HOME/.ssh/known_hosts
grep -qxF "$GITHUB_HOST_KEY" "$KNOWN" 2>/dev/null || echo "$GITHUB_HOST_KEY" >> "$KNOWN"
chown "$SVC_USER:$SVC_USER" "$KNOWN"; chmod 644 "$KNOWN"

repo_readable() {
    as_svc env GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes" \
        git ls-remote --heads "$REPO_URL" "$BRANCH" >/dev/null 2>&1
}
until repo_readable; do
    cat <<EOF

The account cannot read $REPO_URL yet. Add this key on GitHub:
  https://github.com/tadtruex/Boards/settings/keys -> Add deploy key
  Title: broadpeak inventory     Allow write access: leave unchecked

$(cat "$KEY.pub")

EOF
    read -r -p "Press Enter once the key is added (Ctrl-C to stop) " _
done
echo "the account can read the repository"

# --- 3. checkout and venv ------------------------------------------------
say "checkout $CHECKOUT ($BRANCH)"
if [[ ! -d $CHECKOUT/.git ]]; then
    as_svc git clone --depth 1 --filter=blob:none --sparse --branch "$BRANCH" \
        "$REPO_URL" "$CHECKOUT"
    # Only Code/KicadTools is checked out; the unit sets
    # KICADTOOLS_REPO_ROOT, so repo_root() needs no Libraries folder.
    as_svc git -C "$CHECKOUT" sparse-checkout set Code/KicadTools
else
    as_svc git -C "$CHECKOUT" fetch --depth 1 --tags origin "$BRANCH"
    as_svc git -C "$CHECKOUT" reset --hard FETCH_HEAD
fi
RELEASE=$(as_svc git -C "$CHECKOUT" describe --tags --always)
echo "release $RELEASE"

say "venv $VENV"
[[ -x $VENV/bin/python ]] || as_svc python3 -m venv "$VENV"
as_svc "$VENV/bin/pip" install --quiet --upgrade pip
as_svc "$VENV/bin/pip" install --quiet -e "$CHECKOUT/Code/KicadTools[server,inventory,web]"
"$VENV/bin/kicadtools-web" --help >/dev/null

# --- 4. backup -----------------------------------------------------------
say "backup of $DATABASE"
DUMP=$BACKUP_DIR/$DATABASE-$(date +%Y%m%d-%H%M%S).sql.gz
mariadb-dump --single-transaction --routines "$DATABASE" | gzip > "$DUMP"
chmod 600 "$DUMP"
echo "$DUMP ($(du -h "$DUMP" | cut -f1))"

# --- 5. database user ----------------------------------------------------
say "MariaDB user $DB_USER@localhost"
PW_FILE=$KT_DIR/db_password
if [[ -s $PW_FILE ]]; then
    PW=$(head -n1 "$PW_FILE")
    echo "keeping the stored password"
else
    PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
    ( umask 077; printf '%s\n' "$PW" > "$PW_FILE" )
    chown "$SVC_USER:$SVC_USER" "$PW_FILE"
    echo "new password written to $PW_FILE"
fi
chmod 600 "$PW_FILE"
# token_urlsafe has no quote characters, so it is safe inside '...'.
mariadb <<SQL
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$PW';
ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY '$PW';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '$DB_USER'@'localhost';
GRANT SELECT, CREATE, INDEX, REFERENCES ON \`$DATABASE\`.* TO '$DB_USER'@'localhost';
GRANT INSERT, UPDATE, DELETE ON \`$DATABASE\`.bin      TO '$DB_USER'@'localhost';
GRANT INSERT, UPDATE, DELETE ON \`$DATABASE\`.item     TO '$DB_USER'@'localhost';
GRANT INSERT, UPDATE, DELETE ON \`$DATABASE\`.item_tag TO '$DB_USER'@'localhost';
GRANT INSERT, UPDATE, DELETE ON \`$DATABASE\`.item_picture TO '$DB_USER'@'localhost';
GRANT INSERT ON \`$DATABASE\`.meta TO '$DB_USER'@'localhost';
SQL
mariadb -e "SHOW GRANTS FOR '$DB_USER'@'localhost'" | sed 's/IDENTIFIED BY PASSWORD .*/IDENTIFIED BY PASSWORD .../'

say "config $KT_DIR/config.toml"
cat > "$KT_DIR/config.toml" <<EOF
# Written by scripts/deploy_inventory_web.sh. MariaDB is local: with
# local_port = 3306 the port already answers and no ssh tunnel is started.
[parts_db]
backend = "mariadb"
database = "$DATABASE"
user = "$DB_USER"

[parts_db.ssh]
host = "localhost"   # required by the config reader, unused
user = "$SVC_USER"   # required by the config reader, unused
local_port = 3306
EOF
chown "$SVC_USER:$SVC_USER" "$KT_DIR/config.toml"; chmod 600 "$KT_DIR/config.toml"

# --- 6. systemd unit -----------------------------------------------------
say "systemd unit $UNIT"
cat > /etc/systemd/system/$UNIT.service <<EOF
[Unit]
Description=KicadTools lab inventory web app
After=network-online.target mariadb.service
Wants=network-online.target
Requires=mariadb.service

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$SVC_HOME
Environment=KICADTOOLS_CONFIG=$KT_DIR/config.toml
Environment=KICADTOOLS_REPO_ROOT=$CHECKOUT
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/kicadtools-web --host 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$KT_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"

wait_healthy() {
    for _ in $(seq 1 30); do
        if [[ $(curl -s -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null) == ok ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}
if wait_healthy; then
    echo "http://127.0.0.1:$PORT/healthz: ok"
else
    journalctl -u "$UNIT" -n 40 --no-pager >&2
    fail "$UNIT did not answer /healthz (journal above)"
fi

# --- 7. tailscale serve --------------------------------------------------
say "tailscale serve"
# If HTTPS is not enabled for the tailnet yet, this prints a link to the
# admin console that enables it, and finishes once it is enabled.
tailscale serve --bg --https=443 "http://127.0.0.1:$PORT"
tailscale serve status

# --- 8. update command ---------------------------------------------------
say "update command $UPDATE_CMD"
install -m 755 -o root -g root "$CHECKOUT/Code/KicadTools/scripts/inventory_web_update.sh" "$UPDATE_CMD"

TS_NAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
say "done"
cat <<EOF
App:     https://$TS_NAME/   (devices in the tailnet only)
Service: systemctl status $UNIT     journalctl -u $UNIT -f
Release: $RELEASE
Update:  sudo $(basename "$UPDATE_CMD")
Backup:  $DUMP
EOF
