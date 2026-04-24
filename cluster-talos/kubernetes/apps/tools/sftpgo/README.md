# sftpgo — Plex library download portal

Public, HTTP-only download interface for the Plex media library.
Gated by Plex server membership via Authentik OIDC — only users who
share this Plex server can log in, and they can only browse + download
(no writes, no SFTP/FTP).

## Architecture

- sftpgo `ghcr.io/drakkan/sftpgo:v2.6.6-alpine` via bjw-s
  `app-template`.
- **HTTP only** — SFTP/FTP/WebDAV daemons disabled (port 0). Gateway
  terminates TLS; sftpgo listens plaintext on 8080.
- **Data:** SQLite provider on a 1 GiB vSAN RWO PVC (`sftpgo-data`).
  Single replica — SQLite + RWO PVC, no HA.
- **Media mount:** read-only NFS to the TrueNAS media dataset →
  `/srv/media`. Pod joins supplemental group `568` (`apps` on TrueNAS).
- **Public exposure:** no Cloudflare tunnel (media-TOS 2.8). The
  `sftpgo-public` ExternalName Service CNAMEs the public hostname to
  the WAN DDNS via `external-dns ... /public: "true"`. HTTPRoute on
  `cilium/main/https` serves the internal path.

## OIDC login

Authentik application `sftpgo` on the SSO host. Flow:

1. User hits the public hostname `/` → HTTPRoute has an **Exact-match**
   rule on `/` that 302s to `/web/client/oidclogin`, skipping the
   sftpgo form entirely on first visit.
2. sftpgo kicks off the OIDC dance, Authentik runs the Plex source
   (first login = Plex OAuth popup, silent on subsequent visits), user
   lands in `/web/client/`.

Admin login at `/web/admin/login` is **not** rewritten — local password
access still works for the `admin` account.

### Stuck as `akadmin` → "Failed to get user associated with OpenID token"

If you've logged into Authentik as `akadmin` (e.g. via the SSO admin
console), that session bleeds into the sftpgo OIDC flow: Authentik
silently grants with the `akadmin` identity, sftpgo gets
`preferred_username=akadmin`, no matching user in its DB, rejection.

Kill the Authentik session via the invalidation flow:
`/if/flow/default-invalidation-flow/` on the SSO host.

Then retry the sftpgo URL → Plex OAuth → lands as the Plex username.
Alternative: use a private window for sftpgo so the admin session
stays intact for other work.

### Why the redirect is scoped to `/`, not `/web/client/login`

An earlier iteration redirected `/web/client/login` → `/web/client/oidclogin`.
That traps users in an `ERR_TOO_MANY_REDIRECTS` loop: sftpgo's OIDC
callback handler calls `doRedirect()` → `/web/client/login` on any
validation failure (invalid token, non-matching user, wrong role).
The filter then bounces that to `/web/client/oidclogin`, which
restarts the OIDC flow; Authentik silently re-grants because its
session is still live; the callback fails the same way; repeat.

Scoping the redirect to **only the root path** keeps the seamless
entry for typed-URL visitors while letting sftpgo's internal 302s to
`/web/client/login` pass through untouched — users see the native
form with the flash error message and can retry manually.

## Branding

Web-client pages (login + files/shares/etc.) are re-skinned to match
the Homepage status-page look: Unsplash starfield background, dark
translucent cards with backdrop-blur, cyan primary buttons, cyan→violet
gradient headings. Admin UI (`/web/admin/*`) stays default.

- Assets ship via ConfigMap `sftpgo-branding-assets` (2 SVGs + 1 CSS),
  mounted at `/usr/share/sftpgo/static/branding/` → served at
  `/static/branding/*` by sftpgo's static-files handler.
- Wired through `httpd.bindings[0].branding.web_client.*`
  (`LOGO_PATH`, `FAVICON_PATH`, `EXTRA_CSS`, `NAME`, `SHORT_NAME`).
  Templates in the sftpgo binary render these via
  `{{.StaticURL}}{{.Branding.LogoPath}}` etc., where `.StaticURL = /static`.

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
- `SECRET_PLEX_TOKEN` — owner's Plex auth token, used by the sync
  cron only. Does **not** need to match Authentik's Plex-source token;
  the cron talks to plex.tv directly.
