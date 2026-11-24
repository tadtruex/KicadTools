#!/usr/bin/env bash
# Update the lab inventory web app on broadpeak to the head of master
# (git-flow: the current release).
# Installed as /usr/local/sbin/inventory-web-update by
# scripts/deploy_inventory_web.sh; run it with sudo.
#
# A schema change that adds a column needs the database opened once with
# your own account first: the app's database user cannot ALTER tables.

set -euo pipefail

SVC_USER=inventory
CHECKOUT=/srv/inventory/Boards
VENV=/srv/inventory/venv
BRANCH=master
UNIT=kicadtools-web
PORT=8090

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
as_svc() { sudo -u "$SVC_USER" -H -- "$@"; }
cd /srv/inventory   # a working directory $SVC_USER can read

release() { as_svc git -C "$CHECKOUT" describe --tags --always; }
before=$(release)
as_svc git -C "$CHECKOUT" fetch --depth 1 --tags origin "$BRANCH"
as_svc git -C "$CHECKOUT" reset --hard FETCH_HEAD
after=$(release)
echo "checkout: $before -> $after"

as_svc "$VENV/bin/pip" install --quiet -e "$CHECKOUT/Code/KicadTools[server,inventory,web]"
systemctl restart "$UNIT"

for _ in $(seq 1 30); do
    if [[ $(curl -s -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null) == ok ]]; then
        echo "$UNIT: running $after, /healthz ok"
        exit 0
    fi
    sleep 1
done
journalctl -u "$UNIT" -n 40 --no-pager >&2
echo "$UNIT did not answer /healthz (journal above)" >&2
exit 1
