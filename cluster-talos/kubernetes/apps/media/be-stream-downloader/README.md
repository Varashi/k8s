# be-stream-downloader

VRT MAX + VTM GO + Streamz downloader, internal-only. UI at `https://bedl.${SECRET_DOMAIN}`.
Per-provider routing in `app/web/main.py:_provider_for` — host suffix decides which `*-DL.py` script runs.

Source: <https://github.com/Varashi/be-stream-downloader>

## Layout

```
ks.yaml                  Flux Kustomization
app/
  namespace.yaml
  externalsecret.yaml    VRT + Streamz creds, Plex token, GHCR pull secret, .wvd Secret
  pvc.yaml               vsan PVC be-stream-downloader-data (10 Gi)
  helmrelease.yaml       bjw-s app-template, digest-pinned image
  kustomization.yaml
```

## Image

Pinned by digest in `helmrelease.yaml` — `ghcr.io/varashi/be-stream-downloader:<git-sha>@sha256:<manifest-digest>`.

To bump: pull `:latest` after a CI build, copy the digest from `podman image inspect ... --format '{{.Digest}}'` plus `git rev-parse main` from the source repo, edit the `tag:` field, push.

## Volumes

| Mount | Source |
|---|---|
| `/data` | PVC `be-stream-downloader-data` (vsan, RWO, 10Gi). Holds seeded app code + `state.json` + Streamz token cache at `/data/.config/streamz/tokens.json` |
| `/wvd/cdm.wvd` | Secret `be-stream-downloader-wvd` projected from BW SM `SECRET_BEDL_WVD` (base64 of the `.wvd` file). Same CDM serves both VRT and Streamz |
| `/media` | NFS `${SECRET_NFS_HOST}:/mnt/DATA/mediapool/media` — mounted at the *parent* of all libraries so the per-show `library` override can target sibling subtrees (`Series`, `Movies`, `MoviesNL`, …) |

## Secrets (BW SM via ESO)

| BW SM key | Used as | Purpose |
|---|---|---|
| `SECRET_VRT_EMAIL` | env `VRT_EMAIL` | VRT MAX login |
| `SECRET_VRT_PASSWORD` | env `VRT_PASSWORD` | VRT MAX login |
| `SECRET_STREAMZ_EMAIL` | env `STREAMZ_EMAIL` | Streamz Telenet-portal login (Okta IDP) |
| `SECRET_STREAMZ_PASSWORD` | env `STREAMZ_PASSWORD` | Streamz Telenet-portal login (Okta IDP) |
| `SECRET_VTMGO_COOKIES` | env `VTMGO_COOKIES` | Raw `Cookie:` header from logged-in Chrome (DPG Media OIDC). Refreshed manually when 401s appear. |
| `SECRET_PLEX_TOKEN` | env `PLEX_TOKEN` | Plex API auth |
| `SECRET_BEDL_WVD` | Secret data `cdm.wvd` | Widevine CDM device file (base64 of binary) |
| `SECRET_GHCR_PULL_TOKEN` | dockerconfigjson `ghcr-pull-secret` | private GHCR pull |

`PLEX_URL` and `MEDIA_LIBRARY_DEFAULT` are set as plain env in the HelmRelease, not secrets.

VRT re-logs every subprocess call. Streamz logs in once and persists the LFVP cookie envelope to `/data/.config/streamz/tokens.json`; Streamz Next.js silently rotates the inner access_token via popcorn-sdk's confidential client_secret, so the cookie is good for ~365 days. `streamz_auth.invalidate()` drops the cache on a real 401 to force re-login.

VTM GO is the odd one out — Akamai BotManager Premier in front of `login.vtmgo.be` requires JS sensor-data validation that no Python TLS-impersonation client can solve. Workaround: log into vtmgo.be in Chrome, F12 → Network → any document request → Copy as cURL, take the `-b '...'` cookie blob, paste into BW SM `SECRET_VTMGO_COOKIES`. ESO refreshes pod env on the next 1h cycle; for an immediate refresh, kubectl rollout the deployment. Per-asset 5-min Bearer JWTs are minted server-side from the `/vtmgo/afspelen/<uuid>` page HTML on each download. Refresh the BW SM entry when scraping/DL starts returning 401/403 (typically every few days).

## Networking

HTTPRoute on the shared `cilium/main` gateway, hostname `bedl.${SECRET_DOMAIN}`. No `external-dns.alpha.kubernetes.io/public: "true"` annotation → internal AD DNS only, not published to Cloudflare.

Homepage tile under group `Media`.

## Pod placement

No `nodeSelector` — vsan PVC binds wherever the pod schedules (WaitForFirstConsumer). Earlier deploys pinned to `gpu-workers` because the PVC was longhorn-backed; that's been replaced.

## Operator notes

- **Reseed `/data/app/` on image upgrade** is automatic since PR #5 (entrypoint uses `cp -rf`); no `kubectl exec rm` dance needed anymore.
- **Plex hooks** are best-effort. `/plex/status` reports connection health.
- **State migration**: state.json is auto-migrated on read (legacy entries get keys + override fields backfilled). No data loss across image upgrades.
- **GHCR pull token rotation**: the BW SM entry was revoked once via GitHub secret scanning. `SECRET_GHCR_PUSH_TOKEN` worked as a fallback for both push and pull while the read-only token was being regenerated. Generate a fine-grained PAT scoped to the `be-stream-downloader` repo with `Packages: Read` only.
