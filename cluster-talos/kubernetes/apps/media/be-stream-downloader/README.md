# be-stream-downloader

VRT MAX + VTM GO + GoPlay + Streamz downloader, internal-only. UI at `https://bedl.${SECRET_DOMAIN}`.
Per-provider routing in `app/web/main.py:_provider_for` — host suffix decides which `*-DL.py` script runs.

Downloads run **serially** (`BEDL_MAX_CONCURRENT_DOWNLOADS=1` default; one n-m3u8dl-re saturates the household uplink alone).

Auto-DL scheduler (FastAPI lifespan task) ticks every `BEDL_AUTO_DL_INTERVAL_SECONDS` (default `3600`). Per-show toggle in the UI; cold-start is baseline-from-current (only future-published episodes get queued). Quality-upgrade pass re-pulls any Plex-present episode whose video height is below `BEDL_TARGET_HEIGHT` (default `1080`). Multi-show ticks queue serially through the same semaphore as user-clicked downloads.

Source: <https://github.com/Varashi/be-stream-downloader>

## Layout

```
ks.yaml                  Flux Kustomization
app/
  namespace.yaml
  externalsecret.yaml    VRT + Streamz + GoPlay creds, VTM GO cookies, Plex token, GHCR pull secret, .wvd Secret
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
| `/wvd/cdm.wvd` | Secret `be-stream-downloader-wvd` projected from BW SM `SECRET_BEDL_WVD` (base64 of the `.wvd` file). Same CDM serves all four providers |
| `/media` | NFS `${SECRET_NFS_HOST}:/mnt/DATA/mediapool/media` — mounted at the *parent* of all libraries so the per-show `library` override can target sibling subtrees (`Series`, `Movies`, `MoviesNL`, …) |

## Secrets (BW SM via ESO)

| BW SM key | Used as | Purpose |
|---|---|---|
| `SECRET_VRT_EMAIL` | env `VRT_EMAIL` | VRT MAX login |
| `SECRET_VRT_PASSWORD` | env `VRT_PASSWORD` | VRT MAX login |
| `SECRET_STREAMZ_EMAIL` | env `STREAMZ_EMAIL` | Streamz Telenet-portal login (Okta IDP) |
| `SECRET_STREAMZ_PASSWORD` | env `STREAMZ_PASSWORD` | Streamz Telenet-portal login (Okta IDP) |
| `SECRET_VTMGO_COOKIES` | env `VTMGO_COOKIES` | Raw `Cookie:` header from logged-in Chrome (DPG Media OIDC). Refreshed manually when 401s appear. |
| `SECRET_GOPLAY_EMAIL` | env `GOPLAY_EMAIL` | GoPlay (play.tv) login — AWS Cognito user pool. Currently mirrors VRT credentials but kept as a separate BW SM key so they can diverge. |
| `SECRET_GOPLAY_PASSWORD` | env `GOPLAY_PASSWORD` | GoPlay password |
| `SECRET_PLEX_TOKEN` | env `PLEX_TOKEN` | Plex API auth |
| `SECRET_BEDL_WVD` | Secret data `cdm.wvd` | Widevine CDM device file (base64 of binary) |
| `SECRET_GHCR_PULL_TOKEN` | dockerconfigjson `ghcr-pull-secret` | private GHCR pull |

`PLEX_URL` and `MEDIA_LIBRARY_DEFAULT` are set as plain env in the HelmRelease, not secrets.

VRT re-logs every subprocess call. Streamz logs in once and persists the LFVP cookie envelope to `/data/.config/streamz/tokens.json`; Streamz Next.js silently rotates the inner access_token via popcorn-sdk's confidential client_secret, so the cookie is good for ~365 days. `streamz_auth.invalidate()` drops the cache on a real 401 to force re-login.

Streamz Phase 2 shipped 2026-04-28: the Quick Download flow now goes end-to-end (PSSH from MPD → DRMtoday Widevine license at `lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true` → `n-m3u8dl-re` decrypt + MKV mux → Dutch VTT subtitle → SRT mux). `STREAMZ-DL.py` ships two CDM classes: `Local_CDM` (default, uses `BEDL_WVD`) and `GetWVKeys_CDM` (cold backup via getwvkeys.cc, gated on env `WV_TOKEN` / optional `WV_BUILDINFO`/`WV_URL`). Streamz library scraper (`app/scrapers/streamz.py`) shipped 2026-04-28 too — Add Show accepts `streamz.be/streamz/<slug>~<uuid>` URLs.

Auto-DL scheduler shipped 2026-04-28 with two env knobs: `BEDL_AUTO_DL_INTERVAL_SECONDS` (default 3600, min 60) sets the tick cadence; `BEDL_TARGET_HEIGHT` (default 1080) caps the quality-upgrade target. Both default values are sane; the only reason to bump them is if Belgian streamers ever start serving above 1080p.

UI hardening shipped 2026-04-28 alongside the scheduler: per-job log buffer is bounded by `BEDL_LOG_BUFFER_LINES` (default 4000) so a long-running download doesn't grow an unbounded list; n-m3u8dl-re progress lines are coalesced and benign h264 SEI/PPS warnings are filtered to keep the event loop snappy; the `/jobs/{id}/stream` SSE generator emits a 15 s `: keepalive` comment line so queued jobs don't show "stream lost" while waiting at the serial-execution semaphore. Show-page UI now ships collapsible seasons (native `<details>`/`<summary>`) plus cross-season `all/missing/none` selection shortcuts.

rc-check + orphan sweep deployed 2026-04-28 across STREAMZ-DL / VTMGO-DL / GOPLAY-DL — `n-m3u8dl-re`'s exit code is now respected and any half-muxed `<save>.mp4`/`.m4a`/`.srt`/`.ts` stems on a crashed run get cleaned up before the wrapper raises. No more orphans masquerading as legitimate Plex content.

GoPlay logs in via AWS Cognito USER_SRP_AUTH (public user pool `eu-west-1_dViSsKM5Y`, browser SPA client ID, no client secret) using `pycognito` — username + password is enough; no browser, no cookies. The IdToken cache lives at `/data/.config/goplay/tokens.json` and is proactively refreshed before its 1 h expiry. On a real 401 from `api.play.tv` or `drm.goplay.be`, `goplay_auth.invalidate()` drops the cache and a fresh SRP login fires.

VTM GO is the odd one out — Akamai BotManager Premier in front of `login.vtmgo.be` requires JS sensor-data validation that no Python TLS-impersonation client can solve. Workaround: cookie-replay from a logged-in Chrome session. Per-asset 5-min Bearer JWTs are minted server-side from the `/vtmgo/afspelen/<uuid>` page HTML on each download. Refresh the BW SM entry when scraping/DL starts returning 401/403 (typically every few days).

### VTM GO cookie refresh recipe

When VTM GO scraping or DLs start returning 401/403:

1. Log into `https://www.vtmgo.be/` in Chrome (any device, any network — same WAN IP as the pod is fine).
2. F12 → **Network** tab → click any `/vtmgo/...` document request (e.g. the homepage) → right-click → **Copy** → **Copy as cURL (bash)**.
3. From the curl block, extract the `-b '...'` value (the cookie blob, ~6-7 KB).
4. Push to BW SM:
   ```sh
   bws secret edit e55cf33a-3dd7-4499-b5c9-b439013e751d --value "<the cookie blob>"
   ```
   (BW SM secret ID for `SECRET_VTMGO_COOKIES`.)
5. Force pod refresh — ESO polls every 1h, so for immediate effect:
   ```sh
   kubectl rollout restart deploy/be-stream-downloader -n be-stream-downloader
   ```

Cookies typically last a few days. Watch for `[VTM GO] HTTP 4xx` or `cookies expired?` lines in the pod log as the trigger.

## Networking

HTTPRoute on the shared `cilium/main` gateway, hostname `bedl.${SECRET_DOMAIN}`. No `external-dns.alpha.kubernetes.io/public: "true"` annotation → internal AD DNS only, not published to Cloudflare.

Homepage tile under group `Media`.

## Resources

`limits: 2 CPU / 2 GiB` (sized for serial downloads — one n-m3u8dl-re peaks ~1.5 cores + ~600 MiB during a 4 Mbps DASH decode, plus headroom for a concurrent show refresh and the FastAPI event loop). `requests: 100m / 256Mi`. If you ever raise `BEDL_MAX_CONCURRENT_DOWNLOADS` above 1, bump these accordingly — earlier 4c/4Gi was needed for parallel batches of 7-9 concurrent encoders.

## Pod placement

No `nodeSelector` — vsan PVC binds wherever the pod schedules (WaitForFirstConsumer). Earlier deploys pinned to `gpu-workers` because the PVC was longhorn-backed; that's been replaced.

## Operator notes

- **Reseed `/data/app/` on image upgrade** is automatic since PR #5 (entrypoint uses `cp -rf`); no `kubectl exec rm` dance needed anymore.
- **Plex hooks** are best-effort. `/plex/status` reports connection health.
- **State migration**: state.json is auto-migrated on read (legacy entries get keys + override fields backfilled). No data loss across image upgrades.
- **GHCR pull token rotation**: the BW SM entry was revoked once via GitHub secret scanning. `SECRET_GHCR_PUSH_TOKEN` worked as a fallback for both push and pull while the read-only token was being regenerated. Generate a fine-grained PAT scoped to the `be-stream-downloader` repo with `Packages: Read` only.
