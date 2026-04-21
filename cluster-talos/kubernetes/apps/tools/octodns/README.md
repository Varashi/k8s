# octodns — AD DNS → NetBox DNS plugin mirror

One-way read-only mirror of the `boeye.net` zone from Active Directory DNS
into NetBox's `netbox-plugin-dns`, so NetBox IPAM reflects live DNS state
without becoming authoritative.

## Architecture

```
skw-dc.boeye.net (AD DNS, authoritative)
  │  AXFR zone transfer (port 53 TCP)
  ▼
octodns-sync-filtered  (CronJob, */15m, Europe/Brussels)
  │  octodns diff → NetBox REST
  ▼
ipam.boeye.net → /api/plugins/netbox-dns
```

- Source provider: `octodns_bind.AxfrSource` (pulls the full zone each run).
- Target provider: `octodns_netbox_dns.NetBoxDNSProvider` — `replace_duplicates: true`
  so the target is rebuilt every run. `disable_ptr: true` (no reverse zones).
- AD is authoritative — **do not edit DNS records in the NetBox UI**, they
  will be overwritten within 15 minutes.

## Components

| Path                                 | Purpose                                                    |
|--------------------------------------|------------------------------------------------------------|
| `app/cronjob.yaml`                   | CronJob running `octodns-sync-filtered --doit --force`.    |
| `app/configmap.yaml`                 | octodns `config.yaml` — providers + zones.                 |
| `app/externalsecret.yaml`            | Maps BW SM `SECRET_NETBOX_ADMIN_TOKEN` → `NETBOX_API_TOKEN`. |
| `app/namespace.yaml`, `ks.yaml`      | Namespace + Flux Kustomization (dependsOn netbox).         |
| `image/Dockerfile`                   | Custom octodns image (python 3.13 + octodns + plugins).    |
| `image/octodns-sync-filtered`        | Wrapper that patches octodns-bind before running sync.     |

The image is built by `.github/workflows/build-images.yaml` and pushed to
`ghcr.io/<owner>/octodns:latest` (+ `sha-<short>` tag). The CronJob pins
the image by digest — Renovate auto-bumps the digest via the
`# renovate:` annotation in `cronjob.yaml`.

## Why the wrapper (`octodns-sync-filtered`)

`octodns-bind`'s default AXFR behaviour breaks on our zone in two places,
both before any octodns processor can intervene. The wrapper monkey-patches
`AxfrPopulate.zone_records` to fix both at the source:

1. **Out-of-bailiwick glue.** AD caches the Cloudflare NS A-records that
   back the `wan.boeye.net` delegation (`chase.ns.cloudflare.com`,
   `dee.ns.cloudflare.com`). These appear in the AXFR stream with absolute
   names outside `boeye.net.`, and octodns rejects them with a "double-dot"
   `ValidationError` inside `populate()`. The wrapper skips records where
   `name.is_subdomain(zone.origin)` is false.

2. **Apex NS + SOA.** NetBox DNS plugin manages apex NS and SOA from the
   Zone object's `nameservers` / `soa_*` fields, and refuses external
   writes with `boeye.net [NS] is managed, refusing update`. The wrapper
   drops apex NS + SOA so octodns never plans them.

The wrapper then hands off to `octodns.cmds.sync:main` unchanged.

## NetBox DNS plugin prerequisites

Pre-create the Zone + Nameservers before the first sync (octodns will not
create Zones, only Records). This repo doesn't carry a Kustomization for
that — it was bootstrapped with `/tmp/netbox_create_dns_zone.py` (Python
+ BW SM token). If the cluster is rebuilt, rerun that script against the
new NetBox before Flux enables the `octodns` KS.

Plugin settings that must be on (set via the chart's `pluginsConfig`):

- `tolerate_underscores_in_labels: true` — AD allows hosts like
  `slzb-06-zigbee_bluetooth-6be208`; default-strict RFC1035 validation
  rejects them as invalid.

## Operational notes

- **`--force`** bypasses `raise_if_unsafe` (RootNsChange, bulk deletes).
  Safe here because NetBox is a rebuild-each-run mirror; not safe if you
  ever flip the direction.
- **TTL-0 idempotency leak.** Some AD records carry TTL 0 over AXFR. The
  plugin stores them but returns `zone_default_ttl` on read, so octodns
  replans the same ~68 TTL updates every run. It's idempotent in terms of
  applied state, just noisy in the logs.
- **AXFR allow-list.** `skw-dc.boeye.net` must allow zone transfers from
  the pod CIDR (`172.16.4.0/24`). If syncs start failing with `REFUSED`,
  check the AD DNS `Zone Transfers` tab.
- **Digest pin, not `:latest`.** Spegel can cache stale tag→digest
  mappings and serve old layers even with `imagePullPolicy: Always`. The
  CronJob references `ghcr.io/<owner>/octodns:latest@sha256:<digest>` so
  kubelet pulls by content address regardless of Spegel's tag state.
