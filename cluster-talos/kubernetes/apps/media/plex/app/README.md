# plex (prod)

Plex Media Server with scaleplex distributed transcoding — `plex.boeye.net`,
LB `172.16.4.104`. Worker DaemonSet + orchestrator folded into this one HR
alongside the PMS Deployment (bjw-s `app-template` multi-controller layout).

For the broader operational story (backups, library notification chain, test
bench), see `~/CLAUDE-media.md`.

## Layout

```
plex/app/
├── kustomization.yaml
├── namespace.yaml
├── pvc.yaml
├── helmrelease.yaml                       # plex + orchestrator + worker
├── configmap-html-tv-app-profile.yaml     # client profile override (see below)
├── cronjob-backup-preferences.yaml
├── cronjob-backup-full.yaml
├── cronjob-trigger-db-backup.yaml
└── cronjob-optimize-retry.yaml
```

## HTML TV App profile override

`configmap-html-tv-app-profile.yaml` ships a patched copy of the bundled
`/usr/lib/plexmediaserver/Resources/Profiles/HTML TV App.xml`, mounted into
`/config/Library/Application Support/Plex Media Server/Profiles/HTML TV App.xml`
(the user-config override path PMS reads ahead of the bundled profile).

**Why:** PMS 1.43.1.10611 silently ignores the v2 Plex Web client's
`X-Plex-Client-Profile-Extra` augmentation
`add-transcode-target-audio-codec audioCodec=ac3 replace=true`, so PMS hands
the player AAC for HLS even though the client only accepts AC3. The legacy
v2 webOS Plex shell (older LGs running `app.plex.tv/tv-v2-webos`) refuses
the decision and shows `H4` on screen ~23 s later — without ever fetching
the manifest. Trips on every HLS transcode, independent of subtitles or
source codec, and reproduces on stock PMS with scaleplex bypassed.

**Fix:** put `audioCodec="ac3"` ahead of `aac` in the profile's HLS+mpegts
target. PMS picks the first compatible entry, so the chosen encoder becomes
AC3 — which the v2 client accepts. AAC target stays in the list as
fall-through. Both LG TVs in the household advertise this profile name and
natively decode AC3, so the swap is transparent for the v5 client (newer
LGs) and unblocks the v2 one.

**When to remove:** if a future PMS release honors the augmentation's
`replace=true` flag, the override is no longer needed — drop the ConfigMap
+ mount + kustomization entry and PMS falls back to the shipped profile
automatically.

Bisect history (and how to recognise the same pattern on a different
client) is in memory `reference_pms_html_tv_app_ac3_override.md`.

## Other notes

- **DOCKER_MODS** — `ghcr.io/varashi/scaleplex_pms_dockermod:<tag>` replaces
  `/usr/lib/plexmediaserver/Plex Transcoder` with `scaleplex-shim` and runs
  `scaleplex-relay` as an s6 longrun. Removing the env reverts PMS to
  fully-local transcoding on its single GPU.
- **PMS GPU** — `gpu.intel.com/i915: 1` limit; node-selected onto
  `intel.feature.node.kubernetes.io/gpu=true`. Only matters now for
  subtitle rasterising / metadata / photo transcodes — actual video work
  goes to the scaleplex worker fleet.
- **Profile overrides require a PMS restart** to take effect (read once at
  server start). For a quick in-pod test:
  `kubectl -n plex exec <pod> -- s6-svc -r /run/service/svc-plex`.
- **Verbose request-header logging** when bisecting client weirdness:
  `PUT /:/prefs?LogVerbose=1` (persists in `Preferences.xml` on the PVC).
  Expect log files to rotate every couple of minutes; turn it off
  (`LogVerbose=0`) once you've caught what you need.
