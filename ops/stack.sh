# Sourced by ops/update.sh and ops/install.sh: the iroha-specific parts.

STACK=iroha
HEALTH_TIMEOUT=120

# Gitignored files needed to rebuild the host. headscale/config.yaml is
# hand-merged against the release's config-example.yaml, so it is irreplaceable.
CONFIG_FILES="caddy/Caddyfile headscale/config.yaml"

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
    probe "$HEADSCALE_HOST" /health
}
