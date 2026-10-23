# Sourced by ops/update.sh and ops/install.sh: the iroha-specific parts.

STACK=iroha
HEALTH_TIMEOUT=120

# Gitignored files needed to rebuild the host. headscale/config.yaml is
# hand-merged against the release's config-example.yaml, so it is irreplaceable.
CONFIG_FILES="caddy/Caddyfile headscale/config.yaml"
case ",${COMPOSE_PROFILES:-}," in
    *,live,*) live=1; CONFIG_FILES="$CONFIG_FILES caddy/sites/live.caddy live/mediamtx.yml" ;;
    *) live=0 ;;
esac

# Host schedule (local time). Staggered across the fleet: iroha 02:00,
# kaguya 03:00, yachiyo 04:00; reboots 05:00 / 05:20 / 05:40.
UPDATE_ON_CALENDAR="*-*-* 02:00:00"
REBOOT_TIME="05:00"

stack_backup() {
    # SQLite + node keys. Stop headscale so the copy is consistent; the
    # following `docker compose up -d` starts it again.
    docker compose stop headscale &&
        backup_volume headscale /var/lib/headscale "$1/headscale-data.tgz"
}

stack_health() {
    probe "$HEADSCALE_HOST" /health || return 1
    [ "$live" = 1 ] || return 0
    # live: any non-5xx from MediaMTX's catch-all route proves Caddy -> MediaMTX.
    # Deliberately not a forward-auth route, so an Authentik (yachiyo) outage
    # cannot make an iroha deploy roll back.
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 \
        --resolve "$LIVE_HOST:443:127.0.0.1" "https://$LIVE_HOST/healthcheck/") || return 1
    [ "$code" -ge 200 ] && [ "$code" -lt 500 ]
}
