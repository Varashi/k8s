# plex-test

scaleplex test bench — `plex-test.boeye.net`, ns `plex-test`, LB
`172.16.4.106`. Own worker DS + orchestrator + PMS. Not user-facing;
rebuilt fresh, seeded from prod's library DB for representative content.

For the rebuild/seed procedure, public-IP collision risk with prod, and
the prefs that must be set after seeding, see `~/CLAUDE-media.md`
(Plex Test Bench section).

## Layout

```
plex-test/app/
├── kustomization.yaml
├── namespace.yaml
├── pvc.yaml
├── helmrelease.yaml                  # pms + orchestrator + worker
├── configmap-plex-log-tail.yaml      # PMS file log → stdout (see below)
└── …
```

## PMS file log → stdout → vcflogs

Mirrors the same mechanism the prod `plex` HR uses (see
`cluster-talos/kubernetes/apps/media/plex/app/README.md`):
`configmap-plex-log-tail.yaml` defines an s6-overlay longrun
(`plex-log-tail`) that runs `tail -n0 -F` on
`/config/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log`
as uid 1000 inside the `pms` container. Output flows to container
stdout → Logging Operator Fluent Bit DS (`logging` ns) → Fluentd
aggregator → `skw-vcflogs.boeye.net:9543` CFAPI HTTPS.

Only delta vs prod: the persistence entry hangs off controller key `pms`
(plex-test calls the PMS container that, not `app` like prod does) and
the namespace is `plex-test`. Otherwise identical — same ConfigMap
shape, same three subPath mounts under `/etc/s6-overlay/s6-rc.d/`.

## Other notes

- This HR carries scaleplex's bleeding-edge tags (`sha-...` on
  `scaleplex_pms_dockermod`, `scaleplex_worker`, `scaleplex_orchestrator`).
  Prod's tags lag by design — promote here first, then bump prod.
- `ManualPortMappingMode` MUST stay `0` on this server (set via
  `PUT /:/prefs`). Setting it to `1` makes Plex publish a WAN remote-
  access connection on the shared public IP and Plex client dashboards
  dedupe one of the two servers (prod or test) out of view. See the
  CLAUDE-media.md Plex Test Bench section for the full story.
