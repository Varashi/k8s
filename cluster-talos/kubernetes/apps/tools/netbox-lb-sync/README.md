# netbox-lb-sync — k8s LoadBalancer IPs → NetBox IPAM

One-way mirror of in-cluster `type=LoadBalancer` service IPs into NetBox
IPAM, so allocated LB addresses show up as tracked IPs in the same
prefix as the k8s nodes.

## Architecture

```
kube-apiserver (type=LoadBalancer services)
  │  kubectl get svc -A -o json
  ▼
netbox-lb-sync  (CronJob, */15m, Europe/Brussels)
  │  upsert + prune via NetBox REST
  ▼
ipam.boeye.net → /api/ipam/ip-addresses  (tag: k8s-lb)
```

- Source: live cluster state, read via a ServiceAccount with cluster-wide
  `get,list` on `services`.
- Target: NetBox IPAM; admin token pulled from BW SM
  (`SECRET_NETBOX_ADMIN_TOKEN`) via ExternalSecret.
- NetBox is the *mirror*, not the authority — do not edit `k8s-lb`-tagged
  rows in the UI, they get rebuilt on the next run.

## Mapping

Per distinct LB IP:

| NetBox field   | Source                                                              |
|----------------|---------------------------------------------------------------------|
| `address`      | `status.loadBalancer.ingress[0].ip` + `/24` (SKW-K8S mask)          |
| `status`       | `active`                                                            |
| `dns_name`     | first hostname from `external-dns.alpha.kubernetes.io/hostname` (comma-split); empty when annotation absent |
| `description`  | `<app.kubernetes.io/name \| svc name> load balancer`                |
| `tags`         | `k8s-lb` (auto-created on first run)                                |

Services sharing an IP (e.g. `qbittorrent-bittorrent-tcp` + `-udp` via the
cilium `io.cilium/lb-ipam-sharing-key` annotation) collapse to one NetBox
row. jq sorts rows carrying a hostname annotation first, then
`group_by(.ip) | map(.[0])` — so the DNS-bearing service wins the row.

Rows previously tagged `k8s-lb` that no longer correspond to a current LB
service are deleted, so removing a Service cleans up NetBox the next cycle.

## Components

| Path                        | Purpose                                                       |
|-----------------------------|---------------------------------------------------------------|
| `app/cronjob.yaml`          | CronJob `*/15m` running `bash /etc/netbox-lb-sync/sync.sh`.   |
| `app/configmap.yaml`        | The sync script itself.                                       |
| `app/rbac.yaml`             | ServiceAccount + cluster-wide `services` get/list ClusterRole.|
| `app/externalsecret.yaml`   | `SECRET_NETBOX_ADMIN_TOKEN` → env `NETBOX_API_TOKEN`.         |
| `app/namespace.yaml`, `ks.yaml` | Namespace + Flux KS (dependsOn `netbox`).                |

Image: `alpine/k8s` (bundles kubectl, curl, jq). Renovate pins the tag
via the `# renovate:` annotation on `cronjob.yaml`.

## Notes

- **No `postBuild.substituteFrom`** on the Flux Kustomization. Flux's
  substitution rewrites *every* `${X}` token in the rendered manifest,
  including shell expansions inside the script (`${NETBOX_URL}`,
  `${TAG_SLUG:=k8s-lb}`, …). Tokens without a matching cluster-var collapse
  to empty strings. Symptom of the mistake: `curl: (3) No host part in the URL`
  on the first `curl` call.
- **Manual run**: `kubectl -n netbox-lb-sync create job --from=cronjob/netbox-lb-sync netbox-lb-sync-now`.
- **Mask is `/24`** to match the existing node entries in the `SKW-K8S`
  prefix — pick `/32` only if the rest of the prefix ever switches to
  host-masked rows.
