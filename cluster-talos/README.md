# k8s-talos Cluster

Single Talos Linux cluster replacing k8s-adm (Talos+Rancher) and k8s (RKE2).
See **[BOOTSTRAP.md](BOOTSTRAP.md)** for the full lifecycle guide (provision, bootstrap, teardown).

## Stack

| Layer | Tool | Purpose |
|---|---|---|
| Talos config (day-0 + day-2) | [talm](https://github.com/cozystack/talm) | Helm-like templating — renders full machine configs with PKI + applies them to maintenance-mode or running nodes |
| VM provisioning | OpenTofu + vSphere provider | Creates VMs on vSphere from Talos OVA; injects minimal bootstrap config via guestinfo |
| CNI + BGP + Ingress | Cilium 1.19.x | Pod networking, BGP LB IP advertisement, Gateway API |
| GitOps | FluxCD | Reconciles Kubernetes manifests from git |
| CI | GitHub Actions: `flux-diff` + `renovate-validate` | PR-gated rendered-diff comment + Renovate config lint |
| App secrets | ESO + Bitwarden Secrets Manager EU | Pulls secrets from Bitwarden into Kubernetes |
| Internal PKI | cert-manager (self-signed CA) | TLS certs for internal cluster services |
| Public TLS | cert-manager + Let's Encrypt (Cloudflare DNS-01) | TLS certs for internet-facing services |
| Public DNS | external-dns (Cloudflare + RFC2136/AD) | HTTPRoute hostnames → DNS records |
| Storage (RWO) | vSphere CSI + Longhorn (GPU workers) | `vsan` SC on all workers, `longhorn` SC on gpu-worker-* for Kasten |
| Backup | Kasten K10 + `talos-backup` CronJob | App-level restore; daily age-encrypted etcd snapshot to B2 |
| Observability | Prometheus (lean) + metrics-server | Freelens Helm provider; VCF Ops 9 external collector via HTTPRoute + basic auth |
| Log forwarding | Fluent-Bit DaemonSet | Ships `/var/log/containers/*.log` as syslog rfc5424 → VCF Operations for Logs (Tanzu Kubernetes content pack on the ingest side) |
| Config reload | Stakater Reloader | Restarts Deployments/DaemonSets when annotated ConfigMaps/Secrets change |
| Image mirror | Spegel DaemonSet | P2P pull-through cache across node containerds (wildcard mirror via `/etc/cri/conf.d/hosts/_default/hosts.toml`) |
| GPU workloads | Intel GPU device plugin (+ NFD) | `gpu.intel.com/i915` on gpu-worker-*; ClusterPlex HW transcode |

## Cluster Spec

| Parameter | Value |
|---|---|
| Cluster name | k8s-talos |
| Talos version | v1.12.6 |
| Kubernetes version | v1.35.0 |
| VLAN | 104 — `dv-SKW-K8s` portgroup |
| Subnet | 172.16.4.0/24 |
| Gateway / BGP peer | 172.16.4.254 (OPNsense, AS 64512) |
| API VIP | 172.16.4.1 |
| Control planes | 172.16.4.10 / .11 / .12 (4 vCPU, 8 GB, 50 GB) |
| Workers | 172.16.4.20 / .21 / .22 (4 vCPU, 16 GB, 100 GB) — NIC1 k8s + NIC2 storage (10.5.1.20–22/24). Active count = `var.worker_count` (default 2); entries in `worker_nodes` beyond N are declared but not provisioned. |
| GPU workers | 172.16.4.30 / .31 / .32 (2 vCPU, 8 GB, 100 GB + 100 GB Longhorn VMDK) — NIC1 k8s + NIC2 storage (10.5.1.30–32/24) + Intel ARC A380 passthrough. Active count = `var.gpu_worker_count` (default 1). |
| LB IP pool | 172.16.4.100–.200 (BGP via Cilium, AS 64513) |
| Talos schematic | `903b2da78f99adef03cbbd4df6714563823f63218508800751560d3bc3557e40` |

Schematic includes `siderolabs/vmtoolsd-guest-agent` (VMware Tools) only.

---

## Directory Structure

```
cluster-talos/
├── BOOTSTRAP.md              # Full lifecycle guide — read this first
├── tofu/                     # OpenTofu — vSphere VM provisioning
│   ├── main.tf               # Provider, portgroup, content library, OVA
│   ├── nodes.tf              # VM resources (CP + worker); local.active_worker_nodes filters by worker_count
│   ├── variables.tf          # Cluster spec, node maps, worker_count, vSphere settings
│   ├── outputs.tf            # Node IPs
│   ├── terraform.tfvars      # (gitignored) vSphere credentials
│   └── terraform.tfvars.example
├── talos/                    # talm — Talos machine config management (day-0 + day-2)
│   ├── Chart.yaml            # talm chart root
│   ├── values.yaml           # Cluster-wide defaults
│   ├── secrets.yaml          # Cluster PKI (gitignored — back up to Bitwarden)
│   ├── secrets.encrypted.yaml# AGE-encrypted secrets (committed)
│   ├── templates/            # controlplane + worker Go templates + _helpers.tpl
│   ├── scripts/
│   │   └── validate-rendered.py   # Fails CI if rendered configs leak talm --full default subnets
│   ├── nodes/
│   │   ├── bootstrap/        # Full offline-rendered configs (gitignored, consumed by tofu guestinfo)
│   │   ├── values/           # Per-node value overrides (IP, VIP, storageIP) — source of truth
│   │   ├── patches/          # talm modelines + day-2 patches
│   │   └── *.yaml            # Full rendered configs (gitignored, produced by `make render` or `make bootstrap-template`)
│   └── clusterconfig/        # Generated (gitignored): talosconfig + kubeconfig
└── kubernetes/               # FluxCD GitOps manifests
    ├── bootstrap/
    │   └── flux-system/      # Flux install + 5 top-level Kustomization CRs
    │       ├── gotk-components.yaml     # (gitignored) Flux controllers
    │       ├── gotk-sync.yaml           # (gitignored) Root GitRepository + Kustomization
    │       └── cluster-kustomizations.yaml  # All Kustomizations — self-managed after bootstrap
    ├── infrastructure/
    │   ├── flux-system/      # Flux Operator + FluxInstance (self-managed)
    │   │   └── flux-repositories/   # HelmRepository / OCIRepository / GitRepository sources
    │   ├── core/             # Tier reconciled first — CNI, CRDs, storage drivers, PKI, ESO
    │   │   ├── gateway-api/             # Gateway API CRDs
    │   │   ├── cilium/                  # CNI + Gateway (HelmRelease post-bootstrap)
    │   │   ├── cert-manager/
    │   │   │   ├── cert-manager/        # Operator
    │   │   │   └── certs/               # Internal CA + bitwarden-sdk-server TLS cert
    │   │   ├── external-secrets/        # ESO operator
    │   │   ├── kubelet-csr-approver/
    │   │   ├── snapshot-controller/     # CSI volume snapshots (Kasten)
    │   │   ├── vsphere-cpi/             # vSphere cloud-controller
    │   │   ├── vsphere-csi/             # vSAN PVCs
    │   │   ├── intel-gpu/               # NFD + intel-device-plugins-operator
    │   │   ├── intel-gpu-config/        # GpuDevicePlugin CR (sharedDevNum=32)
    │   │   ├── gpu-node-vsphere-maintenance/ # Controller: drain + power-off GPU workers for ESXi maint
    │   │   └── etcd-backup/             # talos-backup CronJob → B2 (age-encrypted)
    │   └── platform/         # Tier reconciled after core — higher-level platform components
    │       ├── metrics-server/          # kubectl top + HPA
    │       ├── reloader/                # Stakater Reloader
    │       ├── descheduler/             # RemoveDuplicates, LowNodeUtilization, etc.
    │       ├── cnpg-system/             # CloudNativePG operator
    │       ├── monitoring/              # Lean Prom + KSM + node-exporter + HTTPRoute (basic-auth via ESO)
    │       ├── configs/                 # BGP, LB pool, GatewayClass, ClusterIssuers, ClusterSecretStore, substvar Secrets
    │       ├── external-dns/
    │       │   ├── external-dns/        # RFC2136 / GSS-TSIG → AD DNS (internal)
    │       │   └── external-dns-cloudflare/ # Cloudflare public records
    │       ├── cloudflare-operator-system/
    │       │   ├── cloudflare-operator/ # ClusterTunnel CRD + operator
    │       │   └── cloudflare-tunnel/   # ClusterTunnel instance + default TunnelBinding
    │       ├── longhorn-system/         # RWO storage on GPU workers (Kasten backup source)
    │       ├── tanzu-system-logging/    # Fluent-Bit DaemonSet → VCF Operations for Logs (syslog rfc5424)
    │       ├── kasten-io/               # Kasten K10 (LDAPS to AD)
    │       ├── spegel/                  # P2P containerd image cache
    │       └── renovate/                # Dependency update bot
    ├── apps/                 # Workloads, nested by category; each app ships its own Flux KS
    │   ├── arr/             # sonarr, radarr, bazarr, prowlarr, autobrr, recyclarr, neutarr, sonarr-nl
    │   ├── downloaders/     # qbittorrent, sabnzbd
    │   ├── media/           # plex, clusterplex, tdarr, tautulli, tracearr, ombi
    │   └── tools/           # netbox, ocis, rustdesk, guacamole, postfix-relay
    │       └── <app>/
    │           ├── ks.yaml  # Flux Kustomization (prune=false, per-app postBuild + dependsOn)
    │           └── app/     # HelmRelease + PVC + ExternalSecret + namespace
    └── forwarders/           # Routing-only shims: external Service + HTTPRoute (+ TunnelBinding)
        ├── home-assistant/
        └── nzbget/
```

---

## Flux Dependency Chain

Six top-level Kustomizations in `bootstrap/flux-system/cluster-kustomizations.yaml`:

```
infrastructure-flux-system  (Flux Operator + FluxInstance; self-manages flux-system controllers)
flux-repositories           (HelmRepository / OCI / GitRepository sources — no dependencies)
infrastructure-core         (aggregator → ~13 child Kustomizations under infrastructure/core/)
    └── infrastructure-platform (aggregator → ~15 children under infrastructure/platform/)
            ├── apps          (aggregator → 21 per-app children under apps/<category>/<app>/)
            └── forwarders    (aggregator → per-app children under forwarders/<app>/)
```

Aggregator parents (`infrastructure-core`, `infrastructure-platform`, `apps`, `forwarders`)
are all `wait: false` on purpose — children carry their own `dependsOn` edges across tiers
(e.g. `core/etcd-backup` → `platform/configs`, `platform/configs` → `core/cert-manager`,
`apps/media/tracearr` → `platform/cnpg`), and `wait: true` on a parent would deadlock the
cross-tier chain.

Child Kustomizations carry their own `postBuild.substituteFrom` where needed
(`cluster-vars` for `${SECRET_DOMAIN}`; `apps-substvars` for `${SECRET_NFS_HOST}`) — parent
substitutions do not inherit to children. Per-app Flux KSs set `prune: false` as a
HelmRelease safety guardrail; aggregator parents use `prune: true`.

All Kustomizations are self-managed by Flux from the moment `flux bootstrap` completes.
No manual `kubectl apply` steps are needed after bootstrap.

---

## Bootstrap Secrets (manual, one-time)

Two secrets must be created manually after `infrastructure-core` reconciles (when
the namespaces first exist). Everything else is pulled from Bitwarden automatically via ESO.

```bash
# Bitwarden SM machine account token
kubectl create secret generic bitwarden-credentials \
  --namespace external-secrets \
  --from-literal=token=<machine-account-token>

# Cloudflare API token (DNS-01 for Let's Encrypt)
kubectl create secret generic cloudflare-api-token \
  --namespace cert-manager \
  --from-literal=api-token=<cloudflare-token>
```

See BOOTSTRAP.md Step 6 for full context and timing.

---

## Key Design Decisions

**Cilium native routing** — `ipv4NativeRoutingCIDR: 10.244.0.0/16` covers the full pod CIDR.
Must match the pod network, not the node subnet — masquerading pod-to-pod traffic breaks
cross-node connectivity in native routing mode.

**CoreDNS upstream DNS** — With Cilium native routing, the Talos hostDNS address
(`169.254.20.10`) is unreachable from pods. `forwardKubeDNSToHost: false` in the machine
config leaves CoreDNS using `/etc/resolv.conf`, which Talos populates from
`machine.network.nameservers`. External DNS works without any CoreDNS patching.

**Gateway API CRDs** — Committed to `infrastructure/core/gateway-api/` and installed by
its own child Kustomization with `wait: true`, ensuring the CRDs exist before anything
that references them (Cilium Gateway resources in `platform/configs`, etc.) reconciles.

**Cilium pre-Flux** — Cilium must be installed before Flux because Flux pods need pod
networking. Installed via `make bootstrap-cilium` (helm upgrade --install with pinned flags).
Flux's HelmRelease then reconciles against the already-installed release.

**Longhorn extraMounts scoped to GPU workers** — `machine.kubelet.extraMounts` (self-binds
on `/var/lib/kubelet/plugins{,_registry}` + `/pods` with `rshared`) is gated on
`.Values.longhorn.enabled` in `templates/worker.yaml`, and only `nodes/values/gpu-worker-*.yaml`
set it. Non-GPU workers omit the binds entirely. Reason: `rshared` places the bind in `/var`'s
shared peer group, so every vSphere CSI globalmount under `/var/lib/kubelet/plugins`
propagates to both mount points and appears twice in `/proc/mounts`. vSphere CSI's
`isBlockVolumeMounted` rejects "mounted in multiple places", NodeUnstage fails, and
VolumeAttachments orphan permanently on the vSAN StorageClass. GPU workers tolerate the
extraMounts because they run Longhorn-only workloads (no vSphere CSI PVCs).

**GPU worker taint = soft + per-app anti-affinity** — `templates/worker-gpu.yaml` taints
gpu-workers `intel.feature.node.kubernetes.io/gpu=true:PreferNoSchedule` (soft, not the
original NoSchedule). Reason: Kasten's block-mode-upload pods carry no tolerations, so a
hard taint stranded every Longhorn-PVC backup. Soft taint lets them land. To prevent
vSphere CSI workloads spilling onto gpu-workers under regular-worker pressure (which would
trigger the rshared VA deadlock above), every vsan-backed app's HelmRelease pins
`defaultPodOptions.affinity.nodeAffinity` with `intel.feature.node.kubernetes.io/gpu
DoesNotExist` as a *required* rule. Update path: `talm apply` strips `nodeTaints` (see
memory `feedback_talm_apply_strips_fields.md`); push via `talosctl apply-config -f
nodes/bootstrap/gpu-worker-X.yaml --mode auto`.

**Two-stage Talos config** — tofu injects a minimal bootstrap machine config
(`talos/nodes/bootstrap/*.yaml`) via guestinfo: hostname + primary NIC static IP + install image,
no cluster/PKI. VMs come up on their final IPs, then `talm template -i` + `talm apply -i`
push the full cluster config with PKI. Same `templates/` + `nodes/values/` used day-0 and
day-2, so day-2 applies don't drift from the bootstrap state. See BOOTSTRAP.md Steps 1–3.

**bitwarden-sdk-server TLS** — ESO's Bitwarden provider requires TLS for SDK server
communication. Certs are issued by the internal cluster CA (cert-manager). The
`cert-manager/certs` child Kustomization (in `core/`) creates the CA and cert before the
`configs` Kustomization (in `platform/`) deploys the SDK server HelmRelease; its
`dependsOn` lists both `cert-manager` and `external-secrets`.

**Control plane metrics exposure** — `controllerManager.extraArgs.bind-address=0.0.0.0` +
`scheduler.extraArgs.bind-address=0.0.0.0` in `talos/templates/controlplane.yaml` let
Prometheus scrape kube-scheduler + kube-controller-manager. Default Talos binds them to
localhost only. Also an apiserver `certSAN` for a stable external DNS name so external
collectors can hit the API with a non-rotating hostname.

**Prometheus external exposure** — Prometheus is exposed on the shared
Cilium Gateway, fronted by its own `--web.config.file` basic auth (no sidecar proxy). The
web config, plus a `server.probeHeaders` snippet that lets Prom scrape itself through the
auth layer, are rendered by an ExternalSecret pulling from BW SM and merged into the
HelmRelease via `valuesFrom`. This is how the external VCF Ops 9 VM scrapes the cluster.

**VCF Ops 9 K8s adapter** — separate read-only path: `vcfops-collector` SA in its own
namespace, bound to a custom ClusterRole granting `get/list/watch` on `*/*` plus node
subresources (`metrics`/`stats`/`proxy`/`spec`) and all `nonResourceURLs`. No write verbs.
Token lives in a `kubernetes.io/service-account-token` Secret; the external VM consumes it
alongside the Prom basic-auth creds.

**Site-identifying info kept out of git** — public DNS zone, account email, API endpoint
hostnames, vCenter FQDN and API cert SANs are all parameterized so the committed tree is
site-neutral. Three mechanisms, one per tool:

| Layer | Value source | Reference variable in committed YAML |
|---|---|---|
| Flux-reconciled manifests (apps + infra) | `cluster-vars` Secret in `flux-system` | `${SECRET_DOMAIN}` etc., via `postBuild.substituteFrom` on the parent Kustomization |
| Flux manifests that must not have the value even in `cluster-vars` (e.g. CF account email) | Bitwarden SM → ESO → `cloudflare-substvars` Secret in `flux-system` | `${SECRET_CLOUDFLARE_ACCOUNT_EMAIL}`, `${SECRET_CLOUDFLARE_ACCOUNT_ID}`, … |
| OpenTofu vSphere provider | Gitignored `tofu/terraform.tfvars` | `var.vsphere_server` (no default — required) |
| Talm machine configs (not flux-reconciled) | Gitignored `talos/values.local.yaml` | `.Values.cluster.apiCertSANs` |

Rules of thumb when adding new identifying info:
- Prefer `${SECRET_DOMAIN}` (cluster-vars) — already substituted on most parent Kustomizations.
- If the parent ks lacks `postBuild.substituteFrom`, add it in `bootstrap/flux-system/cluster-kustomizations.yaml`.
- For values that shouldn't even be in `cluster-vars` (e.g. account identity), add a new key to the matching ExternalSecret (prefix `SECRET_`) and list the backing secret as a second `substituteFrom`. Commit the ES change first and wait for ESO sync before swapping consumers.
- For talm templates, loop over a value in `values.yaml` (default `[]`) and keep the real list in `talos/values.local.yaml`. `make render` chains `--values values.local.yaml` before per-node values.
- An example of each gitignored file is committed next to it (`*.example`).
