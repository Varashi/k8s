# be-stream-downloader

VRT MAX downloader, internal-only. UI at `https://bedl.${SECRET_DOMAIN}`.

Source: <https://github.com/Varashi/be-stream-downloader>

## Layout

```
ks.yaml                  Flux Kustomization
app/
  namespace.yaml
  externalsecret.yaml    VRT creds, Plex token, GHCR pull secret, .wvd Secret
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
| `/data` | PVC `be-stream-downloader-data` (vsan, RWO, 10Gi). Holds seeded app code + `state.json` |
| `/wvd/cdm.wvd` | Secret `be-stream-downloader-wvd` projected from BW SM `SECRET_BEDL_WVD` (base64 of the `.wvd` file) |
| `/media` | NFS `${SECRET_NFS_HOST}:/mnt/DATA/mediapool/media` — mounted at the *parent* of all libraries so the per-show `library` override can target sibling subtrees (`Series`, `Movies`, `MoviesNL`, …) |

## Secrets (BW SM via ESO)

| BW SM key | Used as | Purpose |
|---|---|---|
| `SECRET_VRT_EMAIL` | env `VRT_EMAIL` | VRT MAX login |
| `SECRET_VRT_PASSWORD` | env `VRT_PASSWORD` | VRT MAX login |
| `SECRET_PLEX_TOKEN` | env `PLEX_TOKEN` | Plex API auth |
| `SECRET_BEDL_WVD` | Secret data `cdm.wvd` | Widevine CDM device file (base64 of binary) |
| `SECRET_GHCR_PULL_TOKEN` | dockerconfigjson `ghcr-pull-secret` | private GHCR pull |

`PLEX_URL` and `MEDIA_LIBRARY_DEFAULT` are set as plain env in the HelmRelease, not secrets.

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
