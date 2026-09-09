# headscale-dex-vps

Self-hosted Tailscale-compatible VPN coordination (**Headscale**) with a
self-hosted identity provider (**Dex**) and automatic TLS (**Caddy**), on a
single small VPS.

The point of this stack, compared to Headscale-only templates: **one IdP for
everything**. The same Dex instance that gates who may join your tailnet can
also issue tokens for applications running *inside* it (a build server, a
version-control server, anything that verifies OIDC JWTs) — one user
database, one login screen, one place to revoke access.

```
[laptop] ──┐
[home PC] ─┼── WireGuard mesh (peer-to-peer; data never touches the VPS)
[phone]  ──┘
     │ join / key exchange only
     ▼
[VPS]  Caddy ── headscale.your.domain → Headscale ──┐  OIDC
             └─ dex.your.domain       → Dex      ←──┘
                                         ↑
                    your in-tailnet apps verify JWTs against the same issuer
```

## Requirements

- A VPS with a public IP, ports 80/443 open, Docker + Compose
- A domain with two A records (`headscale.`, `dex.`) pointing at the VPS,
  **not** proxied through a CDN (Headscale's long-lived connections and
  ACME both want a direct path)
- `openssl` and `python3` on the VPS (for `setup.sh`)

## Quick start

```sh
cp .env.example .env
$EDITOR .env          # hostnames, admin email, bcrypt password hash
./setup.sh            # renders configs, generates the OIDC secret
# merge headscale/config.yaml into your pinned release's config-example.yaml
docker compose up -d
```

Join a device (official Tailscale clients):

```sh
tailscale up --login-server https://headscale.your.domain
```

A browser opens Dex; only identities in `oidc.allowed_users` may join.
Manage the tailnet with the embedded CLI:

```sh
docker compose exec headscale headscale nodes list
```

## Design notes

- **Configs are rendered, not edited.** `*.tmpl` + `.env` + `setup.sh`
  keep hostnames and secrets in one place; rendered files are gitignored.
- **Boring failure mode.** If this VPS dies, established WireGuard
  connections keep working — only joins, re-auth, and config pushes stop.
  Back up the `headscale-data` and `dex-data` volumes; Caddy re-issues
  certificates by itself.
- **Node expiry is disabled** (`oidc.expiry: "0"`) so unattended machines
  don't silently fall off the tailnet; expire lost devices manually.
- **Headscale's config schema moves between releases.** The rendered
  `headscale/config.yaml` contains only the load-bearing values; merging it
  into the pinned tag's `config-example.yaml` is a required step, not a
  suggestion.
- DERP relaying uses Tailscale's public relays by default. Latency-sensitive
  setups behind hostile NAT can enable Headscale's embedded DERP later.

## Adding application clients

Set `APP_CLIENT_ID` in `.env` to add a public (device-flow) OIDC client for
an application. Convention worth stealing: **make the client id the hostname
the application is reached at** (e.g. `lore.your.domain`). Dex puts the
client id into the token's `aud`, so with this convention a verifying
application can pin its audience to its own hostname — and clients that
refuse to send tokens whose `aud` doesn't cover the destination host (a
sensible anti-leak measure) work unmodified.

The application then verifies JWTs with:
issuer `https://dex.your.domain/dex`, audience = its own hostname, JWKS via
OIDC discovery. Users obtain tokens via the OAuth 2.0 device flow — see
`examples/` in the parent project for a curl-based helper.

## Similar projects

- Headscale-only Docker setups (e.g. `hwdsl2/docker-headscale`)
- Headscale + Headplane (web UI) + Caddy stacks
- `gmiles32/headscale-authelia` — same idea with Authelia + Traefik

This stack chooses Dex for its connector model (federate to GitHub, LDAP,
SAML, ...) and for double-duty as an application IdP.

## License

MIT — see LICENSE (fill in your name).
