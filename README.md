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
- For [live](#live-streaming) (on by default): a second A record (`live.`),
  ports 1935/tcp, 554/tcp and 8189/tcp+udp open, and a forward-auth Proxy
  provider in Authentik — or turn it off by removing `live` from
  `COMPOSE_PROFILES`

## Quick start

```sh
cp .env.example .env
$EDITOR .env          # hostnames, admin email, Authentik client secret, live/ values
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

## live (streaming)

`live/` is a separate part of this stack, on by default: a
[MediaMTX](https://github.com/bluenviron/mediamtx) streaming server (OBS
publishes over RTMP; viewers watch over WebRTC or RTSP) plus a small control
panel. It is kept apart from Headscale on purpose:

- **Its own directory.** `live/` holds its compose file, MediaMTX config
  template, Caddy site template, panel and stream-key service (`keys/`). The
  root `docker-compose.yml` only
  `include`s it.
- **One switch.** Every live service is in the `live` compose profile.
  `.env.example` turns it on (`COMPOSE_PROFILES=live`), and setup.sh renders
  its files only while it is on. To run Headscale alone, follow
  [Turning live on or off](#turning-live-on-or-off).
- **Shared plumbing.** It joins this project's network and Caddy. setup.sh
  renders its site to `caddy/sites/live.caddy`, which the main Caddyfile
  imports. `ops/update.sh` pulls, backs up and health-checks it along with
  Headscale.

Routing on `LIVE_HOST`:

| Path | Goes to | Access |
| --- | --- | --- |
| `/` | panel | Authentik login (forward auth), `streamers` only |
| `/api/*` | keys service: filtered MediaMTX API (see below) | Authentik login (forward auth), `streamers` only |
| `/keys/*` | keys service: the caller's own stream key | Authentik login (forward auth), `streamers` only |
| anything else | MediaMTX (viewer pages, WHIP/WHEP) | anyone may watch; publishing needs a stream key |

### Stream keys

Every member of the Authentik group `streamers` has their own stream key,
shown (and reset) on the panel. In OBS, use the server
`rtmp://<LIVE_HOST>:1935/` and the stream key `<name>?pass=<key>`, which the
panel builds for you. The key alone identifies the streamer; `<name>` is free
and becomes the stream path. A name that is live cannot be taken over by
another publisher (`overridePublisher: false`).

MediaMTX asks the `keys` service (`live/keys/keys.py`) about every publish. It
allows the stream if the key belongs to a user who is currently in
`streamers` (looked up through the Authentik API, cached for a minute; during
an Authentik outage the last answer is used for up to an hour). Resetting a
key also disconnects the streams started with the old one.

The panel reads MediaMTX's API through the keys service, never directly. It
passes only the lists the panel shows (paths, RTMP connections, RTSP and
WebRTC sessions), blanks every connection's query (where RTMP carries the
key), and fills each connection's `user` with the streamer whose key opened
it. That record (`conns.json` in the `live-keys` volume) survives restarts of
the keys service; once a minute it drops the connections MediaMTX no longer
has open. A streamer can kick only their own stream and its viewers. Removing
someone from `streamers` stops their next publish; to end a stream they are
running, kick it from iroha's shell, where MediaMTX's own API is on
`127.0.0.1:9997`:

```sh
curl -X POST http://127.0.0.1:9997/v3/rtmpconns/kick/<id>   # ids: .../rtmpconns/list
```

Keys are kept as-is in the `live-keys` volume (`keys.json`), so the panel can
show them again; `ops/update.sh` backs the volume up. Anyone with that file or
a backup of it can read every key. If it is lost, streamers create new keys.

### Setup

Set its values in `.env` (setup.sh refuses to run while live is on and they
are missing):

```sh
COMPOSE_PROFILES=live           # the default; remove "live" to turn it off
LIVE_HOST=live.example.com      # A record -> this VPS, DNS-only; also advertised to WebRTC clients
LIVE_AUTHENTIK_TOKEN=<token>    # see "In Authentik" below
```

You also need:

- **Ports:** open 1935/tcp (RTMP), 554/tcp (RTSP) and 8189/tcp+udp (WebRTC
  media).
- **In Authentik:**
  - a group `streamers`;
  - a Proxy provider in forward-auth mode for `LIVE_HOST`, with its
    application bound to the embedded outpost and to the `streamers` group
    (Policy / Group / User Bindings);
  - a service account (e.g. `live-keys`) with a role that may only view the
    `streamers` group (object permission "Can view group" on that group), and
    a non-expiring API token for it. Put the token in `LIVE_AUTHENTIK_TOKEN`.
- **On the Authentik host:** its Caddy must trust this VPS's
  `X-Forwarded-*` headers (`trusted_proxies`), or the outpost cannot match the
  application.

### Turning live on or off

On a running host, changing `COMPOSE_PROFILES` isn't enough. Caddy only reads
its config when it starts or reloads, and Compose ignores the containers of a
profile that's turned off, so they would keep running.

To turn it on, add `live` to `COMPOSE_PROFILES` and set the `LIVE_*` values,
then:

```sh
./setup.sh
docker compose up -d
docker compose exec -w /etc/caddy caddy caddy reload
```

To turn it off, remove the containers first, while the profile is still known:

```sh
docker compose --profile live rm -sf mediamtx panel keys
```

Then remove `live` from `COMPOSE_PROFILES` and run:

```sh
./setup.sh
docker compose exec -w /etc/caddy caddy caddy reload
```

Changing a `LIVE_*` value works the same way as turning it on (the same three
commands). A changed `mediamtx.yml` is only picked up when MediaMTX restarts,
so also run `docker compose restart mediamtx`.

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
