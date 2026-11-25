#!/usr/bin/env bash
# Serve the lab inventory web app at https://inventory.tadtruex.net with
# Caddy, in place of `tailscale serve` (INVENTORY_PLAN.md, "QR codes use a
# name the user owns").
#
#   sudo bash scripts/deploy_inventory_https.sh
#
# Run it on broadpeak after scripts/deploy_inventory_web.sh (the app must
# already answer on 127.0.0.1:8090). Safe to run again. What it sets up:
#
#   1. Caddy from Caddy's apt repository, with the Cloudflare DNS plugin
#      (`caddy add-package`). The package is held: an apt upgrade would
#      replace the binary with one that has no plugin. Upgrade with
#      `sudo caddy upgrade`, which keeps the plugin.
#   2. The Cloudflare API token (Zone > DNS > Edit, tadtruex.net only) in
#      /etc/caddy/cloudflare.env (0600 root), read by systemd. Asked for
#      the first time, then kept. Caddy uses it for the Let's Encrypt DNS-01
#      challenge: the check is a TXT record in public DNS, so broadpeak
#      needs no port open to the internet.
#   3. tailscaled lets the caddy user fetch the certificate for broadpeak's
#      ts.net name (TS_PERMIT_CERT_UID=caddy), so the old address keeps
#      working. This restarts tailscaled once, which drops connections over
#      the tailnet for a few seconds: run it over the LAN or at the console.
#   4. /etc/caddy/Caddyfile: both names, forwarding to 127.0.0.1:8090, bound
#      to broadpeak's tailnet addresses only (as with `tailscale serve`,
#      nothing on the LAN or the internet reaches it). A Caddyfile not
#      written by this script is kept as Caddyfile.orig.
#   5. `tailscale serve` on port 443 turned off (Caddy takes the port), and
#      if ufw is active, 443 allowed in on tailscale0. Caddy does not listen
#      on port 80 (no HTTP-to-HTTPS redirect).
#   6. Caddy (re)started and both names checked over HTTPS.
#
# DNS (done by hand at Cloudflare): an A record `inventory` ->
# broadpeak's tailnet address, proxy off ("DNS only").

set -euo pipefail

NAME=inventory.tadtruex.net
PORT=8090
TOKEN_FILE=/etc/caddy/cloudflare.env
CADDYFILE=/etc/caddy/Caddyfile
MARKER="# Written by Code/KicadTools/scripts/deploy_inventory_https.sh"
DROPIN=/etc/systemd/system/caddy.service.d/inventory.conf
TS_DEFAULTS=/etc/default/tailscaled

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run with sudo: sudo bash $0"
command -v tailscale >/dev/null || fail "tailscale not found"
tailscale status >/dev/null 2>&1 || fail "tailscale is not up (tailscale up)"
[[ $(curl -s -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null) == ok ]] \
    || fail "the app does not answer on 127.0.0.1:$PORT (run deploy_inventory_web.sh first)"

TS_NAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
TS_IP4=$(tailscale ip -4 | head -n1)
TS_IP6=$(tailscale ip -6 | head -n1)
[[ -n $TS_NAME && -n $TS_IP4 ]] || fail "could not read broadpeak's tailnet name and address"
echo "tailnet: $TS_NAME $TS_IP4 $TS_IP6"

# --- 1. Caddy with the Cloudflare plugin ---------------------------------
say "Caddy"
if ! dpkg -s caddy >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
              /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
fi
if caddy list-modules 2>/dev/null | grep -qx 'dns.providers.cloudflare'; then
    echo "Cloudflare plugin present"
else
    caddy add-package github.com/caddy-dns/cloudflare
    caddy list-modules | grep -qx 'dns.providers.cloudflare' || fail "the Cloudflare plugin did not install"
fi
apt-mark hold caddy >/dev/null
caddy version

# --- 2. Cloudflare token -------------------------------------------------
say "Cloudflare token $TOKEN_FILE"
token_ok() {
    # The token goes to curl on stdin, not on its command line (ps shows that).
    curl -s -m 10 -K - https://api.cloudflare.com/client/v4/user/tokens/verify <<<"header = \"Authorization: Bearer $1\"" \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("success") else 1)' 2>/dev/null
}
TOKEN=""
if [[ -s $TOKEN_FILE ]]; then
    TOKEN=$(sed -n 's/^CLOUDFLARE_API_TOKEN=//p' "$TOKEN_FILE")
fi
if [[ -n $TOKEN ]] && token_ok "$TOKEN"; then
    echo "keeping the stored token (Cloudflare says it is valid)"
else
    [[ -n $TOKEN ]] && echo "the stored token is not valid any more"
    until [[ -n $TOKEN ]] && token_ok "$TOKEN"; do
        read -r -s -p "Cloudflare API token (Zone > DNS > Edit, tadtruex.net): " TOKEN; echo
        [[ -n $TOKEN ]] && ! token_ok "$TOKEN" && echo "Cloudflare does not accept that token; try again"
    done
    ( umask 077; printf 'CLOUDFLARE_API_TOKEN=%s\n' "$TOKEN" > "$TOKEN_FILE" )
    echo "token written"
fi
chown root:root "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"

say "systemd drop-in $DROPIN"
install -d -m 755 "$(dirname "$DROPIN")"
cat > "$DROPIN" <<EOF
$MARKER
[Unit]
# Caddy binds to the tailnet addresses, which exist once tailscaled is up.
After=tailscaled.service
Wants=tailscaled.service

[Service]
EnvironmentFile=$TOKEN_FILE
# The packaged unit runs \`caddy run --environ\`, which writes the whole
# environment, the token included, to the journal at every start.
ExecStart=
ExecStart=/usr/bin/caddy run --config $CADDYFILE
Restart=on-failure
RestartSec=5
EOF
systemctl daemon-reload

# --- 3. ts.net certificate for the caddy user ----------------------------
say "tailscaled: certificate access for caddy"
if grep -qx 'TS_PERMIT_CERT_UID=caddy' "$TS_DEFAULTS" 2>/dev/null; then
    echo "already set"
else
    if grep -q '^TS_PERMIT_CERT_UID=' "$TS_DEFAULTS" 2>/dev/null; then
        sed -i 's/^TS_PERMIT_CERT_UID=.*/TS_PERMIT_CERT_UID=caddy/' "$TS_DEFAULTS"
    else
        echo 'TS_PERMIT_CERT_UID=caddy' >> "$TS_DEFAULTS"
    fi
    echo "set; restarting tailscaled"
    systemctl restart tailscaled
    for _ in $(seq 1 30); do tailscale status >/dev/null 2>&1 && break; sleep 1; done
    tailscale status >/dev/null 2>&1 || fail "tailscale did not come back up"
fi

# --- 4. Caddyfile --------------------------------------------------------
say "Caddyfile $CADDYFILE"
if [[ -f $CADDYFILE ]] && ! grep -qxF "$MARKER" "$CADDYFILE"; then
    [[ -f $CADDYFILE.orig ]] || cp -p "$CADDYFILE" "$CADDYFILE.orig"
    echo "the existing Caddyfile is kept as $CADDYFILE.orig"
fi
BIND="$TS_IP4${TS_IP6:+ $TS_IP6}"
cat > "$CADDYFILE" <<EOF
$MARKER
# The lab inventory web app (kicadtools-web on 127.0.0.1:$PORT). Both names
# answer only on broadpeak's tailnet addresses.
# No HTTP-to-HTTPS redirect, so nothing listens on port 80: browsers try
# https first, and the certificates come by DNS (Cloudflare) and from
# tailscaled, neither of which needs port 80.
{
	default_bind $BIND
	auto_https disable_redirects
}

$NAME {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy 127.0.0.1:$PORT
}

# Certificate from tailscaled.
$TS_NAME {
	reverse_proxy 127.0.0.1:$PORT
}
EOF
CLOUDFLARE_API_TOKEN=$TOKEN caddy validate --adapter caddyfile --config "$CADDYFILE" >/dev/null
echo "valid"

# --- 5. port 443: tailscale serve off, firewall --------------------------
say "tailscale serve off on 443"
tailscale serve --https=443 off >/dev/null 2>&1 || true
tailscale serve status 2>&1 || true
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow in on tailscale0 to any port 443 proto tcp >/dev/null
    echo "ufw: 443 allowed in on tailscale0"
fi

# --- 6. start and check --------------------------------------------------
say "port 443"
systemctl stop caddy
# Anything still listening on 443 (on every address, or on a tailnet one)
# stops Caddy from starting.
holders=$(ss -ltnpH 'sport = :443' | grep -E "(\\*|0\\.0\\.0\\.0|\\[::\\]|$TS_IP4|\\[$TS_IP6\\]):443" || true)
[[ -z $holders ]] || fail "something else listens on port 443:
$holders"
echo "free"

say "Caddy (re)start"
systemctl enable caddy >/dev/null
systemctl restart caddy

check() {   # name: wait up to 3 minutes for /healthz over HTTPS at the tailnet address
    for _ in $(seq 1 36); do
        if [[ $(curl -s -m 5 --resolve "$1:443:$TS_IP4" "https://$1/healthz" 2>/dev/null) == ok ]]; then
            echo "https://$1/: ok"
            return 0
        fi
        sleep 5
    done
    return 1
}
failed=0
check "$NAME"    || { echo "https://$NAME/ did not answer" >&2; failed=1; }
check "$TS_NAME" || { echo "https://$TS_NAME/ did not answer" >&2; failed=1; }
if (( failed )); then
    journalctl -u caddy -n 60 --no-pager >&2
    fail "Caddy is not serving every name (journal above)"
fi

say "done"
cat <<EOF
App:     https://$NAME/   and   https://$TS_NAME/   (tailnet only)
Caddy:   systemctl status caddy     journalctl -u caddy -f
Config:  $CADDYFILE   token: $TOKEN_FILE
Upgrade: sudo caddy upgrade   (apt holds the package; keeps the plugin)
EOF
