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

## PMS file log → stdout → vcflogs

`configmap-plex-log-tail.yaml` registers an s6-overlay v3 longrun named
`plex-log-tail` inside the plex container. It tails the file Plex writes
to (`/config/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log`)
into the container's stdout. The Logging Operator's Fluent Bit
DaemonSet (`logging` ns) picks it up along with the rest of
`/var/log/containers/*.log` and forwards to the Fluentd aggregator,
which posts to `skw-vcflogs.boeye.net:9543` via CFAPI HTTPS (the
old syslog/RFC5424 path was replaced 2026-05-27 to escape its
2048-byte per-message cap that clipped long Plex Web Request
lines).

The longrun layout mirrors how `scaleplex_pms_dockermod` already wires
`scaleplex-relay` — three files mounted under `/etc/s6-overlay/s6-rc.d/`:
`plex-log-tail/type`, `plex-log-tail/run`, and
`user/contents.d/plex-log-tail` (marker). `tail -F` follows by filename,
so Plex's 10 MB internal rotation is transparent.

Useful to know:
- vcflogs lumps these under the same `PROCID=app` as the linuxserver
  init banner and the `relay forward done POST ...` chatter — filter by
  message content, not PROCID. (Dedicated sidecar was the alternative
  considered; rejected to stay single-container.)
- `Plex Media Server.log` on the config PVC is unchanged. In-pod
  `kubectl exec` + grep on the 60 MB rolling buffer is still the
  fastest path for live bisects.
- `PUT /:/prefs?LogVerbose=1` makes the tail very chatty; the in-pod
  buffer narrows to ~10-30 min while vcflogs retention picks up the
  slack. Remember to flip it back off after a debugging window.

The plex-test HR ships the same ConfigMap + mount in its `plex-test/app/`
directory.

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
