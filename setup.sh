#!/bin/sh
# Render caddy/headscale configs from *.tmpl using values in .env.
# Idempotent: safe to re-run after editing .env or the templates.
set -eu
cd "$(dirname "$0")"

[ -f .env ] || { echo "error: copy .env.example to .env and edit it first" >&2; exit 1; }

# Read .env literally, line by line, so a value is taken verbatim regardless of
# any shell metacharacters it may contain. Do NOT source it.
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}
    val=${line#*=}
    # Strip one layer of surrounding single quotes, so a value single-quoted in
    # .env (to stop docker compose interpolating a `$`) is still seen raw here.
    case $val in
        \'*\') val=${val#\'}; val=${val%\'} ;;
    esac
    export "$key=$val"
done < .env

for v in HEADSCALE_HOST ID_HOST TAILNET_BASE_DOMAIN ADMIN_EMAIL HEADSCALE_OIDC_SECRET; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || { echo "error: $v is not set in .env" >&2; exit 1; }
done

# live/ is on when COMPOSE_PROFILES includes "live" (as .env.example ships);
# the same switch starts its services in compose.
live=0
case ",${COMPOSE_PROFILES:-}," in *,live,*) live=1 ;; esac
if [ "$live" = 1 ]; then
    for v in LIVE_HOST LIVE_PUBLIC_IP LIVE_PUBLISH_PASS; do
        eval "val=\${$v:-}"
        [ -n "$val" ] || { echo "error: $v is not set in .env (or drop live from COMPOSE_PROFILES)" >&2; exit 1; }
    done
    # The password lands in YAML (mediamtx.yml) and in the OBS stream-key URL
    # (?pass=...), so allow only characters that are literal in both.
    case "$LIVE_PUBLISH_PASS" in
        *[!A-Za-z0-9._~-]*)
            echo "error: LIVE_PUBLISH_PASS may contain only A-Z a-z 0-9 . _ ~ - (try: openssl rand -hex 16)" >&2
            exit 1 ;;
    esac
fi

case "$TAILNET_BASE_DOMAIN" in
    "$HEADSCALE_HOST")
        echo "error: TAILNET_BASE_DOMAIN must differ from the service hostnames" >&2; exit 1 ;;
esac

render() {
    # $1: template path, $2: output path
    python3 - "$1" "$2" <<'PYEOF'
import os, sys
tmpl, out = sys.argv[1], sys.argv[2]
text = open(tmpl).read()
keys = ["HEADSCALE_HOST", "ID_HOST", "TAILNET_BASE_DOMAIN", "ADMIN_EMAIL",
        "HEADSCALE_OIDC_SECRET", "LIVE_HOST", "LIVE_PUBLIC_IP", "LIVE_PUBLISH_PASS"]
for k in keys:
    text = text.replace("{{%s}}" % k, os.environ.get(k, ""))
if "{{" in text:
    sys.exit(f"error: unrendered placeholder remains in {out}")
open(out, "w").write(text)
print(f"rendered {out}")
PYEOF
}

export HEADSCALE_HOST ID_HOST TAILNET_BASE_DOMAIN ADMIN_EMAIL HEADSCALE_OIDC_SECRET

render caddy/Caddyfile.tmpl        caddy/Caddyfile
render headscale/config.yaml.tmpl  headscale/config.yaml

mkdir -p caddy/sites
if [ "$live" = 1 ]; then
    render live/Caddyfile.tmpl     caddy/sites/live.caddy
    render live/mediamtx.yml.tmpl  live/mediamtx.yml
    chmod 600 live/mediamtx.yml    # holds the publisher password
else
    rm -f caddy/sites/live.caddy
fi

echo "done. next: review headscale/config.yaml against your release's" \
     "config-example.yaml, then: docker compose up -d"
