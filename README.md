# iroha

Self-hosted Tailscale-compatible VPN coordination (**Headscale**) with automatic
TLS (**Caddy**), on a single small VPS. Identity is delegated to an external
**Authentik** IdP running on a separate host, so who may join the tailnet is
governed by the same account system as the rest of the fleet.

```
[laptop] ──┐
[home PC] ─┼── WireGuard mesh (peer-to-peer; data never touches the VPS)
[phone]  ──┘
     │ join / key exchange only
     ▼
[VPS]  Caddy ── headscale.your.domain → Headscale
                                         │ OIDC (auth-code + PKCE)
                                         ▼
                          Authentik (id.your.domain, separate host)
```

## Requirements

- A VPS with a public IP, ports 80/443 open, Docker + Compose
- A domain with one A record (`headscale.`) pointing at the VPS, **not** proxied
  through a CDN (Headscale's long-lived connections and ACME both want a direct
  path)
- `python3` on the VPS (for `setup.sh`)
- A reachable Authentik instance with an OAuth2 provider + application for
  Headscale — see [Authentik provider](#authentik-provider)

## Quick start

```sh
cp .env.example .env
$EDITOR .env          # hostnames, admin email, Authentik client secret
./setup.sh            # renders configs
# merge headscale/config.yaml into your pinned release's config-example.yaml
docker compose up -d
```

Join a device (official Tailscale clients):

```sh
tailscale up --login-server https://headscale.your.domain
```

A browser opens Authentik; only identities in `oidc.allowed_users` may join.
Manage the tailnet with the embedded CLI:

```sh
docker compose exec headscale headscale nodes list
```

## Authentik provider

Create, in Authentik, an OAuth2/OpenID provider and an application for Headscale:

- **Client type** confidential; put the generated client secret in `.env` as
  `HEADSCALE_OIDC_SECRET` (the client id is `headscale`).
- **Redirect URI** `https://<HEADSCALE_HOST>/oidc/callback`.
- **Scopes** must include a mapping that emits `email` **and**
  `email_verified: true` — Headscale rejects unverified emails by default, and
  Authentik's stock email mapping hard-codes `email_verified: false`.
- The application's issuer is `https://<ID_HOST>/application/o/<slug>/`; the
  trailing slash is load-bearing — it must match the issuer in the discovery
  document, which is what `headscale/config.yaml.tmpl` points at.

Headscale keys users on `iss/sub`. If you ever switch the issuer or provider the
identifier changes, and existing users must have their `provider_identifier`
column updated to the new `iss/sub` (`url.JoinPath(iss, sub)`), or logins fail
with a name/email collision against the old row.

## Automated updates

Updates are pull-based: nothing outside this VPS can push to it.

- **Images:** [Renovate](https://github.com/apps/renovate) opens PRs that bump
  the image tags and digests pinned in `docker-compose.yml` (see `renovate.json`).
  Patch, minor and digest bumps merge automatically once CI passes. Major bumps,
  and Headscale/Authentik feature releases, wait for you and are marked
  `[manual-deploy]`.
- **Deploy:** a nightly `iroha-update` timer runs `ops/update.sh`. It
  fast-forwards to `origin/main`, pulls the images, backs up (`ops/stack.sh`),
  restarts, and health-checks. If the checks fail it rolls back to the previous
  commit and does not retry that commit. It does not deploy commits marked
  `[manual-deploy]`, or commits that change `*.tmpl`, `.env.example` or
  `setup.sh`; it sends a reminder each night instead.
- **OS:** unattended security upgrades, with a reboot at 05:00 if one is
  needed. Docker is held back, because upgrading the engine restarts every
  container.

Setup, once per VPS: install the Renovate GitHub app on this repo, add
`UPDATE_NOTIFY_URL` to `.env` (optional; an [ntfy](https://ntfy.sh) topic URL
works), then:

```sh
sudo ops/install.sh               # timer at 02:00 + unattended-upgrades
systemctl list-timers iroha-update  # next run
journalctl -u iroha-update          # past runs
```

For a `[manual-deploy]` change: read the upstream notes, do any manual steps,
then run `ops/update.sh --force`. For Headscale, merge the new release's
`config-example.yaml` into `headscale/config.yaml` first.

A change to `*.tmpl`, `.env.example` or `setup.sh` is always deployed by hand,
because `update.sh` never re-renders configs: `git pull`, `./setup.sh` (then
re-merge `headscale/config.yaml`), then
`docker compose pull && docker compose up -d`.

Backups go to `/var/backups/iroha/<time>-<previous commit>/` (newest 5 kept;
local only, so copy them off the host yourself). Rollback never restores data.
If a one-way migration ran and the old version cannot start, restore by hand
from that directory:

```sh
docker compose stop headscale
docker run --rm -i --volumes-from "$(docker compose ps -aq headscale)" busybox:1.37 \
    sh -c 'rm -rf /var/lib/headscale/* && tar xzf - -C /var/lib/headscale' < headscale-data.tgz
docker compose up -d
```

## Design notes

- **Configs are rendered, not edited.** `*.tmpl` + `.env` + `setup.sh` keep
  hostnames and secrets in one place; rendered files are gitignored.
- **Boring failure mode.** If this VPS dies, established WireGuard connections
  keep working — only joins, re-auth, and config pushes stop. Back up the
  `headscale-data` volume; Caddy re-issues certificates by itself.
- **Node expiry is disabled** (`oidc.expiry: "0"`) so unattended machines don't
  silently fall off the tailnet; expire lost devices manually.
- **Headscale's config schema moves between releases.** The rendered
  `headscale/config.yaml` contains only the load-bearing values; merging it into
  the pinned tag's `config-example.yaml` is a required step, not a suggestion.
- DERP relaying uses Tailscale's public relays by default.

## License

MIT — see LICENSE (fill in your name).
