# netbox — IPAM + DNS plugin

NetBox deployed from the upstream chart with a custom image that bakes in
`netbox-plugin-dns` (so NetBox can host DNS objects alongside IPAM).

## Custom image

`image/Dockerfile` extends `docker.io/netboxcommunity/netbox` and installs
`netbox-plugin-dns` into the existing `/opt/netbox/venv` via `uv pip`
(netbox-docker doesn't ship pip inside the venv). Built by
`.github/workflows/build-images.yaml` and pushed to
`ghcr.io/<owner>/netbox-plus`. Pinned versions in the Dockerfile ARGs are
tracked by Renovate (regex manager in `renovate.json`).

The chart's `plugins:` values key enables the plugin; `pluginsConfig:`
passes Django `PLUGINS_CONFIG` entries. See
`app/helmrelease.yaml` for the full config.

## DNS plugin pre-reqs for the OctoDNS mirror

The sibling [`octodns/`](../octodns/) CronJob expects:

- Zone `boeye.net` pre-created (plugin won't auto-create zones).
- Nameservers `skw-dc.boeye.net` + `sta-dc.boeye.net` registered on the
  Zone (apex NS + SOA are managed by the plugin from these fields).
- `pluginsConfig.netbox_dns.tolerate_underscores_in_labels: true` — AD
  hosts like `slzb-06-zigbee_bluetooth-*` otherwise fail validation.

Bootstrap script: `/tmp/netbox_create_dns_zone.py` (uses BW SM
`SECRET_NETBOX_ADMIN_TOKEN`). Rerun after a full NetBox rebuild.

## API tokens (v2 / peppered)

NetBox 4.4+ supports v2 API tokens: stored as an HMAC digest peppered with
`API_TOKEN_PEPPERS` (never plaintext at rest). Enabled here via secret key
`api_token_peppers` (BW SM `SECRET_NETBOX_API_TOKEN_PEPPERS`, JSON
`{"<id>":"<pepper>"}`) wired in `app/externalsecret.yaml`; the chart's
projected `secrets` volume already mounts it and `configuration.py` reads it.
Peppers load at Django startup — restart NetBox after changing them.

`SECRET_NETBOX_ADMIN_TOKEN` (consumed by netbox-lb-sync + octodns) is a **v2**
token: client string `nbt_<key>.<secret>`, sent as `Authorization: Bearer …`
(not the legacy `Token …` scheme). pynetbox ≥7.5 auto-detects the `nbt_`
prefix; the lb-sync curl script switches scheme on the prefix.

Recreate after a full rebuild (peppers must already be configured):
```
POST /api/users/tokens/  {"version":2,"user":<admin id>}   # via Django shell
# capture t.token (=secret) + t.key, store nbt_<key>.<secret> in BW SM
```
The chart-bootstrapped superuser token (`SECRET_NETBOX_SUPERUSER_API_TOKEN`)
remains **v1** (unused break-glass; chart bootstrap doesn't emit v2).

## Other notes

- Backing store: external CNPG (`netbox-pg-rw`); bundled postgres/valkey
  subcharts disabled.
- `updateStrategy: Recreate` — the media/reports/scripts PVCs are RWO on
  vSAN and can't stay bound on two nodes during a rolling update.
- Startup probe extended to ~20 min to cover cold-start migrations on
  vSAN.
- LDAP auth is intentionally disabled for now (deferred).
