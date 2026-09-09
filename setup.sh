#!/bin/sh
# Render caddy/dex/headscale configs from *.tmpl using values in .env.
# Idempotent: safe to re-run after editing .env or the templates.
set -eu
cd "$(dirname "$0")"

[ -f .env ] || { echo "error: copy .env.example to .env and edit it first" >&2; exit 1; }

# Read .env literally, line by line. Do NOT source it: values like bcrypt
# hashes contain `$` and would be mangled by shell expansion.
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}
    val=${line#*=}
    # Strip one layer of surrounding single quotes. Values containing `$`
    # (bcrypt hashes) are single-quoted in .env so `docker compose` won't try
    # to interpolate them; setup.sh must still see the raw value.
    case $val in
        \'*\') val=${val#\'}; val=${val%\'} ;;
    esac
    export "$key=$val"
done < .env

for v in HEADSCALE_HOST DEX_HOST TAILNET_BASE_DOMAIN LORE_HOST ADMIN_EMAIL ADMIN_PASSWORD_HASH; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || { echo "error: $v is not set in .env" >&2; exit 1; }
done

case "$TAILNET_BASE_DOMAIN" in
    "$HEADSCALE_HOST"|"$DEX_HOST"|"$LORE_HOST")
        echo "error: TAILNET_BASE_DOMAIN must differ from the service hostnames" >&2; exit 1 ;;
esac

# Generate the Headscale<->Dex OIDC secret once and persist it in .env.
if [ -z "${HEADSCALE_OIDC_SECRET:-}" ]; then
    HEADSCALE_OIDC_SECRET=$(openssl rand -hex 32)
    if grep -q '^HEADSCALE_OIDC_SECRET=' .env; then
        sed -i.bak "s/^HEADSCALE_OIDC_SECRET=.*/HEADSCALE_OIDC_SECRET=${HEADSCALE_OIDC_SECRET}/" .env && rm -f .env.bak
    else
        printf 'HEADSCALE_OIDC_SECRET=%s\n' "$HEADSCALE_OIDC_SECRET" >> .env
    fi
    echo "generated HEADSCALE_OIDC_SECRET (stored in .env)"
fi

render() {
    # $1: template path, $2: output path
    python3 - "$1" "$2" <<'PYEOF'
import os, sys
tmpl, out = sys.argv[1], sys.argv[2]
text = open(tmpl).read()
keys = ["HEADSCALE_HOST", "DEX_HOST", "TAILNET_BASE_DOMAIN", "LORE_HOST",
        "ADMIN_EMAIL", "ADMIN_PASSWORD_HASH", "HEADSCALE_OIDC_SECRET"]
for k in keys:
    text = text.replace("{{%s}}" % k, os.environ.get(k, ""))
if "{{" in text:
    sys.exit(f"error: unrendered placeholder remains in {out}")
open(out, "w").write(text)
print(f"rendered {out}")
PYEOF
}

export HEADSCALE_HOST DEX_HOST TAILNET_BASE_DOMAIN LORE_HOST ADMIN_EMAIL ADMIN_PASSWORD_HASH \
       HEADSCALE_OIDC_SECRET

render caddy/Caddyfile.tmpl        caddy/Caddyfile
render dex/config.yaml.tmpl        dex/config.yaml
render headscale/config.yaml.tmpl  headscale/config.yaml

echo "done. next: review headscale/config.yaml against your release's" \
     "config-example.yaml, then: docker compose up -d"
