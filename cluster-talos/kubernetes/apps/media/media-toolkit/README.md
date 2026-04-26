# media-toolkit

Workbench pod on `k8s-talos` for ad-hoc media editing — replaces logging into
`skw-adm-l` and `brew install`-ing tools every time. Built once, exec into it.

## Layout

```
media-toolkit/
├── ks.yaml                 # Flux Kustomization
├── app/
│   ├── namespace.yaml
│   ├── kustomization.yaml
│   ├── externalsecret.yaml # ESO → media-toolkit-secrets (Sonarr/Radarr/TrueNAS API keys)
│   └── helmrelease.yaml    # bjw-s app-template Deployment
└── image/
    └── Dockerfile          # ghcr.io/varashi/media-toolkit
```

## What's in the image

Debian trixie + apt-installed:
- `ffmpeg` (VA-API + QSV hwaccels), `mkvtoolnix` (mkvmerge, mkvextract), `mediainfo`
- `intel-media-va-driver-non-free`, `vainfo`, `libigfxcmrt7` — Intel Arc GPU stack
- `sshpass`, `rsync`, `openssh-client`, `curl`, `jq`
- `python3` + pip: `faster-whisper`, `pysubs2`, `requests`

## Pod shape

- 1 replica Deployment, `sleep infinity` (workbench, not a service)
- Runs as `uid 1000:1000`, supplemental group `568` (TrueNAS `apps`)
- nodeSelector `intel.feature.node.kubernetes.io/gpu=true` + matching toleration
- Resource limit `gpu.intel.com/i915: 1`

## Mounts

| Path | Source | Notes |
|---|---|---|
| `/mnt/media/media` | NFS `${SECRET_NFS_HOST}:/mnt/DATA/mediapool/media` | RW, root_squash → uid 1000 |
| `/scratch` | emptyDir 50Gi | Local scratch for concat lists, srt staging, whisper models |
| `/tmp` | emptyDir | Standard tmp |

## Env (from ESO `media-toolkit-secrets`)

| Var | BW SM key |
|---|---|
| `SONARR_NL_API_KEY` | `SECRET_SONARR_NL_API_KEY` |
| `RADARR_NL_API_KEY` | `SECRET_RADARR_NL_API_KEY` |
| `TRUENAS_API_KEY` | `SECRET_TRUENAS_API_KEY` |

> SSH password (`INFRA_SSH_PASSWORD`) to push from `skw-d-test` is **not** in
> BWS. Either set it ad-hoc:
> `kubectl -n media-toolkit exec -it deploy/media-toolkit -- env SSHPASS=... bash`
> or add `SECRET_INFRA_SSH_PASSWORD` to BWS and another `data:` entry in
> `externalsecret.yaml`.

## Usage

Drop into the pod:
```bash
kubectl -n media-toolkit exec -it deploy/media-toolkit -- bash
```

Verify GPU + tooling (one-shot):
```bash
kubectl -n media-toolkit exec deploy/media-toolkit -- bash -c '
  vainfo 2>&1 | head -10
  ffmpeg -hide_banner -hwaccels
  mkvmerge --version
  python3 -c "import faster_whisper; print(faster_whisper.__version__)"
'
```

Trigger Sonarr-NL rescan from inside the pod (in-cluster URL — no SSL):
```bash
curl -s -X POST -H "X-Api-Key: $SONARR_NL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"RescanSeries","seriesId":18}' \
  http://sonarr-nl.sonarr-nl.svc:8989/api/v3/command
```

Push from `skw-d-test` straight to TrueNAS NFS:
```bash
SSHPASS='<infra-pw>' sshpass -e rsync \
  -e 'ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no' \
  -rlt --info=progress2 --partial \
  --include='*.mkv' --include='*.mp4' --include='*.srt' --exclude='*' \
  'root@skw-d-test:/mnt/sdb1/Series NL/<Show>/S01/' \
  '/mnt/media/media/SeriesNL/<Show>/_staging/Belgian-S01/'
```

ffmpeg concat-demuxer (matching codecs, lossless mkv merge):
```bash
printf "file '%s'\nfile '%s'\n" deel1.mkv deel2.mkv > /scratch/concat.txt
ffmpeg -y -f concat -safe 0 -i /scratch/concat.txt -c copy merged.mkv
```

Hardware-accelerated VA-API encode (Intel iGPU/Arc):
```bash
ffmpeg -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 \
  -hwaccel_output_format vaapi -i in.mkv \
  -c:v hevc_vaapi -b:v 4M -c:a copy out.mkv
```

faster-whisper language detection on first 30s:
```bash
python3 - <<'EOF'
from faster_whisper import WhisperModel
m = WhisperModel("tiny", compute_type="int8")
seg, info = m.transcribe("sample.mkv", language=None, task="transcribe", vad_filter=True)
print(info.language, info.language_probability)
EOF
```

## Bumping the image

1. Edit `image/Dockerfile`, commit, push.
2. `.github/workflows/build-images.yaml` builds + pushes new tag `sha-<commit>`
   to `ghcr.io/varashi/media-toolkit`.
3. Get the digest:
   ```bash
   TOKEN=$(curl -s 'https://ghcr.io/token?service=ghcr.io&scope=repository:varashi/media-toolkit:pull' | jq -r .token)
   curl -sLI "https://ghcr.io/v2/varashi/media-toolkit/manifests/sha-<short>" \
     -H "Accept: application/vnd.oci.image.index.v1+json" \
     -H "Authorization: Bearer $TOKEN" | grep -i docker-content-digest
   ```
4. Update `app/helmrelease.yaml` `tag: sha-<short>@sha256:<digest>`. Commit + push.
   (Spegel mirror requires digest pin — `:latest` alone gets cached stale.)

## Reference

- Procedure that drove this: `~/.claude/projects/.../memory/reference_belgian_dub_migration_procedure.md`
- Why the pod exists: `~/.claude/projects/.../memory/feedback_kill_abandoned_background_jobs.md`
  (Linuxbrew install on `skw-adm-l` thrashed CPU for 24 min — pod is the fix)
