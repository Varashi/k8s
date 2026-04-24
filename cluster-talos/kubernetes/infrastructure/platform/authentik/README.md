# authentik — IdP / SSO

Authentik deployed from the upstream `goauthentik` chart, fronts the
cluster's SSO integrations. First consumer is
[sftpgo](../../../apps/tools/sftpgo/) (Plex-gated downloads at
`plexmedia.boeye.net`); more media-app integrations planned — see
`project_media_sso.md` in auto-memory.

Public URL: `https://sso.boeye.net`. Internal HTTPRoute on
`cilium/main/https` (gateway `172.16.4.100`); public DNS CNAMEs
`sso.boeye.net` to the WAN DDNS via the `authentik-public` ExternalName
Service (`external-dns ... /public: "true"`).

## Backing store

Bundled chart subcharts disabled:

- **Postgres:** external CNPG `Cluster/authentik-db` (see
  `app/cluster.yaml`). CNPG handles backups via the infra-level
  barmanObjectStore.
- **Redis:** bjw-s app-template `authentik-redis` StatefulSet with a
  dedicated PVC (see `app/redis.yaml`).

The chart's `authentik.postgresql` + `authentik.redis` blocks point at
the in-namespace services.

## Secrets

All in Bitwarden SM, pulled via ExternalSecrets:

- `SECRET_AUTHENTIK_SECRET_KEY` — Django secret key.
- `SECRET_AUTHENTIK_POSTGRES_PASSWORD` — CNPG initdb + `authentik` user
  (used twice: CNPG bootstrap + app env).
- `SECRET_AUTHENTIK_BOOTSTRAP_PASSWORD` + `SECRET_AUTHENTIK_BOOTSTRAP_TOKEN`
  — first-run `akadmin` creds. Keep these even after first boot; the
  bootstrap env is idempotent and required for some CLI/API paths.

App bootstrap email: `frank@boeye.net`.

## Config drift — what lives only in Authentik's DB

GitOps ships the install; **all IdP objects are configured in the
Authentik UI/API** and persist only in Postgres. These are NOT in this
repo:

- **Sources** — the Plex source (`type=plex`,
  `user_matching_mode=identifier`). Rebuilding from scratch: Admin UI →
  Directory → Federation & Social login → Create → Plex; capture a Plex
  auth token during the OAuth dance.
- **Applications / Providers** — per-consumer OIDC providers. For
  sftpgo: provider `sftpgo` (OAuth2/OpenID, redirect URI
  `https://plexmedia.boeye.net/web/client/oidcredirect`, signing key =
  default `authentik Self-signed Certificate`), application `sftpgo`
  bound to that provider.
- **Flows** — default `default-source-enrollment` / `default-authentication-flow`
  are fine; just ensure the identification stage has the Plex source
  listed under `sources` so the "Sign in with Plex" button appears.
- **Users** — mirrored at login time from the Plex source
  (`preferred_username` = Plex username, preserving literal `&amp;` —
  see `feedback_authentik_plex_html_entities.md`).

### Recovery

1. Kasten restores the `authentik` namespace PVCs → CNPG + Redis resume
   with prior state, IdP objects intact. This is the preferred path.
2. If CNPG is lost: `helm rollback` / re-install, then rebuild the
   objects above by hand. Shouldn't take more than ~15 min; the list is
   short.

## Notes

- Anti-affinity keeps Authentik off GPU workers (avoids pod-level vGPU
  contention for an app that doesn't need GPU).
- Gatus + Homepage annotations on the Route for status/dashboard
  integration.
