# sftpgo — Plex library download portal

Public, HTTP-only download interface for the Plex media library at
`https://plexmedia.boeye.net`. Gated by Plex server membership via
Authentik OIDC — only users who share this Plex server can log in, and
they can only browse + download (no writes, no SFTP/FTP).

## Architecture

- sftpgo `ghcr.io/drakkan/sftpgo:v2.6.6-alpine` via bjw-s
  `app-template`.
- **HTTP only** — SFTP/FTP/WebDAV daemons disabled (port 0). Gateway
  terminates TLS; sftpgo listens plaintext on 8080.
- **Data:** SQLite provider on a 1 GiB vSAN RWO PVC (`sftpgo-data`).
  Single replica — SQLite + RWO PVC, no HA.
- **Media mount:** read-only NFS from
  `skw-truenas-nfs.boeye.net:/mnt/DATA/mediapool/media` → `/srv/media`.
  Pod joins supplemental group `568` (`apps` on TrueNAS).
- **Public exposure:** no Cloudflare tunnel (media-TOS 2.8). The
  `sftpgo-public` ExternalName Service CNAMEs `plexmedia.boeye.net` to
  the WAN DDNS via `external-dns ... /public: "true"`. HTTPRoute on
  `cilium/main/https` serves the internal path.

## OIDC login

Authentik application `sftpgo` at `sso.boeye.net`. Flow:

1. User hits `plexmedia.boeye.net` → sftpgo 302s to
   `/web/client/login`.
2. HTTPRoute has an **Exact-match** rule on `/web/client/login` that
   302s to `/web/client/oidclogin` — skips the sftpgo form entirely so
   the user never sees it.
3. sftpgo kicks off the OIDC dance, Authentik runs the Plex source
   (first login = Plex OAuth popup, silent on subsequent visits), user
   lands in `/web/client/`.

Admin login at `/web/admin/login` is **not** rewritten — local password
access still works for the `admin` account.

### Config notes

- `SFTPGO_HTTPD__BINDINGS__0__ENABLED_LOGIN_METHODS=0` — bitmask values
  (1/3/9) crash the HTTP server with "no login method available for
  WebAdmin UI". `0` = all configured methods allowed; OIDC-provisioned
  users have no password so the form is unusable for them anyway.
- `SFTPGO_DATA_PROVIDER__USERNAME_REGEX=^[a-zA-Z0-9][a-zA-Z0-9_.@+&-]*$`
  — default regex rejects `&`, which appears literally in at least one
  Plex username on this account.
- OIDC field `preferred_username` drives the sftpgo username;
  `implicit_roles=false` keeps OIDC users as plain users, not admins.

## User lifecycle — `sftpgo-plex-sync` CronJob

Daily at `03:17 Europe/Brussels` an inline Python script (ConfigMap
`sftpgo-plex-sync`, image `python:3.13-alpine`, stdlib only):

1. Lists Plex shared users via
   `GET https://plex.tv/api/servers/<machine-id>/shared_servers`.
2. Adds the server owner from `GET https://plex.tv/api/v2/user` (the
   owner is not in the `shared_servers` list).
3. Fetches an sftpgo admin bearer (basic-auth → `/api/v2/token`).
4. Creates missing users, enables any disabled users that are back in
   the Plex set.
5. Disables (doesn't delete) sftpgo users no longer in the Plex set.

Provisioned user shape: `home_dir=/srv/media`, UID/GID `1000`,
permissions `list+download`, `filters.web_client` disables uploads,
shares, password change, MFA, etc.

### Known gap

No JIT pre-login hook — a user removed from the Plex share between cron
runs can still log in until the next 03:17 sync (<=24h window).
Acceptable for this use case; if tightened, add a `pre-login-hook`
container that re-queries Plex.

## Secrets

All in Bitwarden SM, pulled via the ExternalSecret in `app/`:

- `SECRET_SFTPGO_ADMIN_PASSWORD` — default admin, used by CREATE_DEFAULT_ADMIN.
- `SECRET_SFTPGO_JWT_KEY` — signing passphrase for sftpgo's own tokens.
- `SECRET_SFTPGO_DATA_KEY` — data provider encryption key.
- `SECRET_SFTPGO_OIDC_CLIENT_SECRET` — matches the Authentik `sftpgo`
  provider client secret.
- `SECRET_PLEX_TOKEN` — Frank's Plex auth token, used by the sync cron
  only. Does **not** need to match Authentik's Plex-source token; the
  cron talks to plex.tv directly.
