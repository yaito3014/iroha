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
