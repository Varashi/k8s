# k8s-talos Bootstrap Guide

This document covers the full lifecycle of the k8s-talos cluster: initial provisioning,
bootstrapping, and teardown. It is designed to be followed repeatedly during testing.

## Cluster Spec

| Parameter         | Value                                        |
|-------------------|----------------------------------------------|
| Cluster name      | k8s-talos                                    |
| Talos version     | v1.12.6                                      |
| Kubernetes version| v1.35.0                                      |
| VLAN              | 104 — `dv-SKW-K8s` portgroup                 |
| Subnet            | 172.16.4.0/24                                |
| Gateway / BGP peer| 172.16.4.254 (OPNsense, AS 64512)            |
| API VIP           | 172.16.4.1                                   |
| Control planes    | 172.16.4.10 / .11 / .12 (4 vCPU, 8 GB, 50 GB)|
| Workers           | 172.16.4.20 / .21 / .22 (4 vCPU, 16 GB, 100 GB)|
| GPU workers       | 172.16.4.30 / .31 / .32 (2 vCPU, 8 GB, 100 GB + Longhorn 100 GB)|
| LB IP pool        | 172.16.4.100–.200 (BGP via Cilium, AS 64513) |
| Talos schematic   | `903b2da78f99adef03cbbd4df6714563823f63218508800751560d3bc3557e40` |

The schematic includes only the `siderolabs/vmtoolsd-guest-agent` extension (VMware Tools).
To regenerate: `POST https://factory.talos.dev/schematics` with body
`{"customization":{"systemExtensions":{"officialExtensions":["siderolabs/vmtoolsd-guest-agent"]}}}`.
See [factory.talos.dev](https://factory.talos.dev) if extensions change.

---

## Prerequisites (one-time, already done)

These are configured once and survive cluster rebuilds:

- **OPNsense**: VLAN 104 interface (`ix1_vlan104`, `172.16.4.254/24`), firewall allow rule,
  FRR BGP (AS 64512) with static neighbors (AS 64513): control planes `.10–.12`,
  workers `.20–.22`, GPU workers `.30–.32`. **Important:** OPNsense only accepts BGP
  sessions from explicitly-configured neighbors. Provisioning a node at a new IP without
  first adding the OPNsense neighbor entry leaves that node's `cilium bgp peers` in
  `idle`, so any LB IP whose only endpoint lands on it is silently black-holed.
- **MikroTik switch10g1 + switch10g2**: VLAN 104 tagged on all ESXi uplinks and the
  LACP bonding inter-switch link.
- **vSphere**: The `dv-SKW-K8s` distributed portgroup (VLAN 104) is created by OpenTofu
  on first apply and persists across cluster rebuilds.
- **Bitwarden Secrets Manager**: Organisation and project created; machine account token
  generated.

---

## Tooling

| Tool          | Purpose                                      | Install                         |
|---------------|----------------------------------------------|---------------------------------|
| `talm`        | Talos config management — day-0 render + day-2 apply/upgrade | `curl -sSL https://github.com/cozystack/talm/raw/refs/heads/main/hack/install.sh \| sh -s` |
| `talosctl`    | Direct Talos API client (bootstrap etcd, kubeconfig) | `brew install siderolabs/tap/talosctl` |
| `tofu`        | Provision vSphere VMs                        | `brew install opentofu`         |
| `helm`        | Install Cilium before Flux                   | `brew install helm`             |
| `flux`        | Bootstrap GitOps                             | `brew install fluxcd/tap/flux`  |
| `kubectl`     | Kubernetes management                        | `brew install kubectl`          |

---

## Directory Structure

```
cluster-talos/
├── BOOTSTRAP.md              # This file
├── .gitignore
├── tofu/                     # OpenTofu — vSphere VM provisioning
│   ├── main.tf               # Provider, portgroup, content library, OVA
│   ├── nodes.tf              # VM resources (CP + worker)
│   ├── variables.tf          # Cluster spec, node maps, vSphere settings
│   ├── outputs.tf            # Node IPs
│   ├── terraform.tfvars      # (gitignored) vSphere credentials — copy from .example
│   └── terraform.tfvars.example
├── talos/                    # Talos machine config management
│   ├── Chart.yaml            # talm chart root (talosconfig path, apply options)
│   ├── values.yaml           # Cluster-wide defaults (installer image, nameservers, etc.)
│   ├── secrets.yaml          # Cluster PKI + tokens (gitignored — backed up to Bitwarden)
│   ├── secrets.encrypted.yaml# AGE-encrypted secrets (committed)
│   ├── talm.key              # AGE private key (gitignored — backed up to Bitwarden)
│   ├── templates/            # Go templates per node type
│   │   ├── _helpers.tpl      # installImage helper
│   │   ├── controlplane.yaml # CP template (full config — used day-0 + day-2)
│   │   └── worker.yaml       # Worker template (includes storage NIC (NIC2))
│   ├── nodes/                # Per-node files
│   │   ├── bootstrap/        # Minimal guestinfo configs (committed, consumed by tofu)
│   │   │   └── {cp,worker}-{1,2,3}.yaml
│   │   ├── values/           # Per-node value overrides (IP, VIP, storageIP)
│   │   ├── patches/          # Per-node patches with talm modelines (day-2 apply target)
│   │   ├── cp-{1,2,3}.yaml   # Full configs rendered by `make bootstrap-template` (gitignored)
│   │   └── worker-{1,2,3}.yaml
│   ├── charts/talm/          # talm library chart (talm.discovered.* functions)
│   └── clusterconfig/        # Generated files (gitignored)
│       ├── talosconfig       # talosctl client config
│       └── kubeconfig        # Kubernetes admin config
└── kubernetes/               # FluxCD GitOps manifests
    ├── bootstrap/            # One-time flux bootstrap output
    ├── flux/                 # HelmRepositories + top-level Kustomizations
    ├── infrastructure/
    │   ├── controllers/      # Gateway API CRDs, Cilium, cert-manager, ESO HelmReleases
    │   └── configs/          # BGP, ClusterIssuer, Bitwarden store
    └── apps/                 # Workloads (populated during migration)
```

---

## Step 1 — Prepare machine configs

**Why:** Talos nodes are configured in two stages:

1. **Minimal bootstrap config** (committed in `talos/nodes/bootstrap/*.yaml`) is injected
   via VMware guestinfo at VM creation. It sets hostname + primary NIC static IP + install
   image — just enough for the node to come up on its final IP and listen on
   maintenance-mode API (:50000) so `talm` can reach it.
2. **Full cluster config** (rendered by `talm template`, written to `talos/nodes/*.yaml`)
   contains PKI, etcd, kubelet, VIP, and cluster bootstrap info. Pushed via
   `talm apply -i` after VMs are up.

Same tool (talm) for day-0 render and day-2 apply. `templates/controlplane.yaml` and
`templates/worker.yaml` are the source of truth for both.

```bash
cd ~/git/k8s/cluster-talos/talos/
```

### 1a — Generate cluster PKI (first time only, or after full teardown)

```bash
talosctl gen secrets -o secrets.yaml
```

Writes `secrets.yaml` with cluster CA, etcd CA, service account key, bootstrap tokens.
**Back it up to Bitwarden SM.** It is gitignored — losing it means no new Talos certs
can be issued for this cluster identity.

For a fresh project from scratch, you can alternately use `talm init --preset cozystack
--name k8s-talos --encrypt` which scaffolds `Chart.yaml`, `values.yaml`, `templates/`,
and `secrets.yaml` together. This repo is already scaffolded — skip.

If rebuilding while keeping PKI (existing kubeconfigs stay valid), reuse the existing
`secrets.yaml`.

### 1b — Encrypt secrets (committed copy)

```bash
talm init --encrypt     # first time only; generates talm.key + secrets.encrypted.yaml
# OR on secrets rotation:
sops --encrypt --age $(cat talm.key | grep public) secrets.yaml > secrets.encrypted.yaml
```

`secrets.encrypted.yaml` is committed. `talm.key` and `secrets.yaml` are gitignored.

### 1c — Verify per-node values

Edit `nodes/values/<node>.yaml` if IPs or hostnames change. Each file:

```yaml
node:
  ip: 172.16.4.10/24       # primary NIC static IP
  gateway: 172.16.4.254
  vip: 172.16.4.1          # CPs only
  storageIP: 10.5.1.20/24  # workers only (storage NIC)
hostname: k8s-talos-cp-1
```

Edit `nodes/bootstrap/<node>.yaml` if adding nodes or changing IPs — these are the
minimal configs tofu injects. Values there must match `nodes/values/` so the full
apply in Step 3 doesn't flip IPs.

---

## Step 2 — Provision VMs with OpenTofu

**Why:** OpenTofu creates the vSphere infrastructure declaratively: the distributed
portgroup for VLAN 104, a content library, the Talos OVA downloaded from the Image
Factory, and 6 VMs cloned from that OVA. The **minimal** bootstrap config
(`talos/nodes/bootstrap/<node>.yaml`) is injected into each VM via the VMware `guestinfo`
mechanism at creation time — Talos reads it on first boot to come up on its final
static IP + hostname. Full cluster config is pushed in Step 3.

```bash
cd ~/git/k8s/cluster-talos/tofu/
```

### 2a — Set up credentials (first time only)

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars and fill in:
#   vsphere_user, vsphere_password, vsphere_server
```

### 2b — Initialise (first time only, or after provider version changes)

```bash
tofu init
```

Downloads the vSphere provider plugin.

### 2c — Review the plan

```bash
tofu plan
```

On first run this shows: 1 portgroup, 1 content library, 1 OVA item, 6 VMs.
On subsequent runs (after teardown) the portgroup and content library already exist,
so only the 6 VMs are recreated.

### 2d — Apply

```bash
cd tofu/
make apply
```

The Makefile runs `tofu apply -parallelism=1 -auto-approve`. The `-parallelism=1` flag
is critical — applying changes to multiple VMs in parallel risks simultaneous reboots
that would take down the entire cluster.

The OVA download from `factory.talos.dev` takes a few minutes on first run.
VMs boot automatically once created. Talos applies the minimal guestinfo config,
brings up the primary NIC on the static IP, and stays reachable on the maintenance-mode API
(:50000, unauthenticated) awaiting a full cluster config.

> **Caveat:** Talos may reject a partial machine config on schema validation (no
> `cluster.*` block). If nodes fail to boot or don't come up on their static IPs,
> the fallback is to render full configs offline and inject them via guestinfo
> instead — see "Fallback: full guestinfo" at the end of Step 3.

> **Note:** `nodes.tf` sets `wait_for_guest_net_timeout = 0` on all VMs. The vSphere
> provider normally waits for VMware Tools to report an IP before considering a VM
> created. Since all nodes have static IPs there is no need to wait, and the default
> 5-minute timeout is not long enough for Talos to start `vmtoolsd-guest-agent` on
> first boot. The VMs are fully created even if the provider would otherwise time out.

> **Worker NICs — two-step process:** Worker VMs need two NICs but the vSphere
> content library OVA deployment API cannot map more networks than the OVA has NIC
> templates (the Talos OVA has 1 NIC). Use the Makefile targets — no file editing needed:
>
> ```bash
> make apply-bootstrap   # step 1: creates VMs with primary NIC only (worker_storage_nic=false)
> # ... bootstrap etcd, kubeconfig, CSRs, Cilium, Flux, secrets (see below) ...
> make apply             # step 2: adds storage NIC to workers via VM reconfigure (not clone)
> ```
>
> The `worker_storage_nic` variable (default `true`) controls the dynamic storage-NIC block.
> The second `make apply` is safe to run at any point after VMs exist.
>
> The second NIC (`dv-SKW-Storage`, VLAN 101, 10.5.1.0/24) gives workers direct
> TrueNAS NFS access without traffic passing through OPNsense. CPs have only the primary NIC.

---

## Step 3 — Render + apply full config via talm

**Why:** The minimal guestinfo config from Step 2 got each VM onto its static IP and
maintenance-mode API. Now talm queries each maintenance node, discovers disks/NICs,
renders the full config from `templates/` + `nodes/values/` + `secrets.yaml` (with
PKI embedded), and pushes it. Node reboots into a real CP/worker on the same IP.

```bash
cd ~/git/k8s/cluster-talos/talos/
```

### 3a — Generate talosconfig (first time only)

```bash
make talosconfig
export TALOSCONFIG=$PWD/clusterconfig/talosconfig
```

### 3b — Render full configs

```bash
make bootstrap-template
```

For each node, runs `talm template -n <ip> -e <ip> -t templates/<type>.yaml --values
nodes/values/<node>.yaml --full --with-secrets secrets.yaml -i > nodes/<node>.yaml`.
The `-i` flag means insecure maintenance-mode query (no client cert yet). Output
contains PKI + discovered hardware comments + the talm modeline that day-2 apply needs.

Day-2 alternative — `make render` renders all 6 nodes **offline** (no live queries)
from `templates/` + `nodes/values/` into both `nodes/*.yaml` (for day-2 apply) and
`nodes/bootstrap/*.yaml` (for tofu guestinfo). Use it after any template or values
change on a running cluster to keep both sets in sync. It runs `validate-rendered`
automatically at the end.

### 3c — Apply full configs

```bash
make bootstrap-apply
```

Pushes each rendered config via `talm apply -f nodes/<node>.yaml -i`. Each node installs
the system image, writes config to disk, and reboots. After reboot the node runs the
full cluster config on the same static IP and its API transitions to authenticated
(mTLS via `talosconfig`).

`bootstrap-apply` runs `make validate-rendered` first; it refuses to push configs that
have non-empty `cluster.network.{podSubnets,serviceSubnets}` (see
[cluster.network podSubnets / serviceSubnets](#clusternetwork-podsubnets--servicesubnets--must-be-empty-arrays)).

### 3d — Bootstrap etcd

**Why:** etcd does not start automatically — exactly one node must be told to initialise
a new etcd cluster. After that, the other control planes join automatically.

```bash
make bootstrap-etcd
```

Runs `talm bootstrap -f nodes/cp-1.yaml`. Equivalent to:

```bash
talosctl -e 172.16.4.10 -n 172.16.4.10 bootstrap
```

This tells cp-1 to form a new single-node etcd cluster. The other control planes
detect the cluster via the VIP and join automatically within ~30 seconds. All three
CPs will transition to `stage: running` once etcd is healthy.

### 3e — Fetch kubeconfig

```bash
talosctl -n 172.16.4.10 -e 172.16.4.10 kubeconfig /tmp/k8s-talos-fresh.yaml --force
```

Merge into `~/.kube/config` and rename context to `k8s-talos`:

```bash
# Remove stale k8s-talos entries from main config
kubectl config delete-context k8s-talos 2>/dev/null || true
kubectl config delete-cluster k8s-talos  2>/dev/null || true
kubectl config delete-user admin@k8s-talos 2>/dev/null || true

# Merge fresh kubeconfig into main config
KUBECONFIG=/tmp/k8s-talos-fresh.yaml:~/.kube/config kubectl config view --flatten > /tmp/merged.yaml
cp ~/.kube/config ~/.kube/config.bak
cp /tmp/merged.yaml ~/.kube/config

# talosctl names the context admin@k8s-talos — rename to k8s-talos
kubectl config rename-context admin@k8s-talos k8s-talos
kubectl config use-context k8s-talos
```

Also copy to `clusterconfig/kubeconfig` for tools that reference it directly:

```bash
cp /tmp/k8s-talos-fresh.yaml clusterconfig/kubeconfig
export KUBECONFIG=~/git/k8s/cluster-talos/talos/clusterconfig/kubeconfig
```

Nodes will show `NotReady` until Cilium is installed (no CNI yet).

### 3f — Approve kubelet serving CSRs

**Why:** Talos is configured with `rotate-server-certificates: true`, which makes each
kubelet request a signed TLS certificate for its serving endpoint. Kubernetes does not
auto-approve these `kubernetes.io/kubelet-serving` CSRs by default. Until they are
approved, `kubectl logs` and `kubectl exec` fail with TLS errors, and CoreDNS logs are
inaccessible.

```bash
kubectl get csr --no-headers | awk '/Pending/ {print $1}' | xargs kubectl certificate approve
```

Run this once after bootstrap. From this point on, `kubelet-csr-approver` (deployed
by Flux as part of `infrastructure-controllers`) handles approval automatically.

### Fallback: full guestinfo (if minimal config fails)

If Step 2 VMs don't boot (Talos rejects partial config on schema validation, nodes
don't reach :50000 on their static IPs), switch to injecting the full rendered config
via guestinfo instead:

```bash
cd ~/git/k8s/cluster-talos/talos/
# Render full configs offline (no maintenance-mode query needed)
for n in cp-1 cp-2 cp-3; do
  talm template -t templates/controlplane.yaml \
    --values nodes/values/$n.yaml --full --with-secrets secrets.yaml --offline \
    > nodes/bootstrap/$n.yaml
done
for n in worker-1 worker-2 worker-3; do
  talm template -t templates/worker.yaml \
    --values nodes/values/$n.yaml --full --with-secrets secrets.yaml --offline \
    > nodes/bootstrap/$n.yaml
done
# Re-apply tofu so guestinfo gets the new (full) configs
cd ../tofu && tofu apply -replace='vsphere_virtual_machine.cp["cp-1"]' ...
```

With full guestinfo, VMs boot fully configured — skip Step 3b/3c (bootstrap-template
and bootstrap-apply). Go straight to Step 3d (bootstrap etcd).

---

## Step 4 — Install Cilium (pre-Flux)

**Why:** Flux pods need working pod networking to start. Cilium must be installed
before Flux can bootstrap. After Flux takes over, its Cilium HelmRelease reconciles
against this already-installed release.

The Makefile target creates the namespace with PodSecurity `privileged` labels
(Cilium's DaemonSet needs them) and runs `helm upgrade --install` with the BGP +
Gateway API flags matching the Flux HelmRelease.

```bash
cd ~/git/k8s/cluster-talos/talos/
make bootstrap-cilium
```

Wait for all 6 nodes to reach `Ready` (~2 minutes):

```bash
kubectl get nodes -w
```

Verify Cilium pods healthy:

```bash
kubectl get pods -n cilium
```

All `cilium-*`, `cilium-envoy-*`, `cilium-operator-*`, `hubble-relay-*`, `hubble-ui-*`
should be `Running`.

> **Version note:** The pinned `--version 1.19.2` in the Makefile must match
> `spec.chart.spec.version` in `kubernetes/infrastructure/controllers/cilium/helmrelease.yaml`.
> Bump both together.

---

## Step 5 — Bootstrap Flux

**Why:** Flux is the GitOps controller. Once bootstrapped, it reads this repository and
reconciles all infrastructure and workloads automatically. The bootstrap command installs
Flux into the cluster and creates a `GitRepository` + `Kustomization` pointing at this repo.

The GitHub PAT is stored in `~/.config/gh/hosts.yml` (the `gh` CLI config). Pass it
via the `GITHUB_TOKEN` env var. The `--token-auth` flag is required because the PAT is
a fine-grained token that cannot manage repository deploy keys.

```bash
GITHUB_TOKEN=$(grep oauth_token ~/.config/gh/hosts.yml | head -1 | awk '{print $2}') \
flux bootstrap github \
  --owner=Varashi \
  --repository=k8s \
  --branch=main \
  --path=cluster-talos/kubernetes/bootstrap \
  --personal \
  --token-auth
```

Flux reconciles automatically in dependency order — all Kustomizations are defined in
`kubernetes/bootstrap/flux-system/cluster-kustomizations.yaml` and self-managed from
the moment `flux bootstrap` completes. No additional `kubectl apply` steps required.

```
flux-repositories    (HelmRepositories — no dependencies)
infrastructure-controllers  (Gateway API CRDs, Cilium, cert-manager, ESO,
                             kubelet-csr-approver, metrics-server,
                             vsphere-cpi, vsphere-csi)
    └── infrastructure-certs  (internal cluster CA + bitwarden-sdk-server TLS cert)
            └── infrastructure-configs  (BGP, ClusterIssuers, bitwarden-sdk-server,
                                         ClusterSecretStore, vsan StorageClass)
                    └── apps
```

Gateway API CRDs are committed to `infrastructure/controllers/gateway-api/` and installed
as part of `infrastructure-controllers`. This ensures they exist before `infrastructure-configs`
tries to create the Cilium `GatewayClass`.

---

## Step 6 — Create bootstrap secrets

**Why:** ESO needs a Bitwarden machine account token to connect to Bitwarden SM, and
cert-manager needs a Cloudflare API token to issue Let's Encrypt certificates via
DNS-01. These are the only secrets created manually — everything else flows through
ESO once it is running.

**Timing:** The `external-secrets` and `cert-manager` namespaces are created by
`infrastructure-controllers`. Wait for it to finish before running these commands:

```bash
# Wait for infrastructure-controllers to complete (creates the namespaces)
kubectl get kustomizations -n flux-system -w
# Once infrastructure-controllers shows Ready=True, Ctrl-C and continue

# Bitwarden SM machine account token — used by ESO ClusterSecretStore
# (organizationID and projectID are baked into cluster-secret-store.yaml, not here)
kubectl create secret generic bitwarden-credentials \
  --namespace external-secrets \
  --from-literal=token=<machine-account-token>

# Cloudflare API token — used by cert-manager ClusterIssuer for DNS-01
kubectl create secret generic cloudflare-api-token \
  --namespace cert-manager \
  --from-literal=api-token=<cloudflare-token>

# Cluster variables — used by Flux postBuild substituteFrom for variable substitution
# across infrastructure-configs, infrastructure-external-dns, infrastructure-renovate, and apps.
# Add more keys here as new manifests require ${VARIABLE} substitution.
kubectl create secret generic cluster-vars \
  --namespace flux-system \
  --from-literal=SECRET_DOMAIN=<base-domain> \
  --from-literal=SECRET_ACME_EMAIL=<letsencrypt-contact-email> \
  --from-literal=SECRET_EXTERNAL_DNS_KERBEROS_REALM=<AD-realm-uppercase> \
  --from-literal=SECRET_EXTERNAL_DNS_KDC_1=<dc1-ip> \
  --from-literal=SECRET_EXTERNAL_DNS_KDC_2=<dc2-ip>
```

vSphere CCM + CSI credentials are fetched from Bitwarden SM by ExternalSecret — no
manual kubectl secret needed. Ensure these two BW SM secrets exist in the project:

- `SECRET_VSPHERE_USERNAME` — vCenter admin (e.g. `administrator@vsphere.local`)
- `SECRET_VSPHERE_PASSWORD` — vCenter admin password

ESO creates `vsphere-cloud-secret` (kube-system, for CCM) and `vsphere-config-secret`
(vmware-system-csi, for CSI) from these once the ClusterSecretStore is Ready.

To retrieve tokens from a running cluster before teardown:

```bash
kubectl get secret bitwarden-credentials -n external-secrets -o jsonpath='{.data.token}' | base64 -d
kubectl get secret cloudflare-api-token -n cert-manager -o jsonpath='{.data.api-token}' | base64 -d
kubectl get secret cluster-vars -n flux-system -o jsonpath='{.data}' | python3 -c "import sys,json,base64; [print(k,base64.b64decode(v).decode()) for k,v in json.load(sys.stdin).items()]"
```

`infrastructure-certs` and `infrastructure-configs` will then reconcile automatically.
The `ClusterSecretStore` will become Ready once the bitwarden-credentials secret exists.

---

## Step 7 — Verify

```bash
kubectl get nodes                        # all 6 Ready
kubectl get pods -A                      # all Running
cilium status                            # BGP peers established (once nodes are up)
flux get kustomizations                  # all True / Applied (flux-repositories, infrastructure-controllers, infrastructure-certs, infrastructure-configs, apps)
kubectl get clusterissuers               # selfsigned, cluster-ca, letsencrypt-prod — all Ready
kubectl get clustersecretstores         # bitwarden-secretsmanager — Valid + Ready
kubectl get svc -A | grep LoadBalancer   # LB services get IPs from 172.16.4.100–.200
kubectl get nodes -o=jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.providerID}{"\n"}{end}'
                                         # every node has vsphere://<vm-uuid> (CCM initialized)
kubectl get sc                           # vsan (default) present
```

---

## Teardown (for test cycles)

To destroy the cluster and start fresh:

```bash
cd ~/git/k8s/cluster-talos/tofu/

# Destroy all 6 VMs (portgroup and content library are preserved)
tofu destroy -target vsphere_virtual_machine.cp -target vsphere_virtual_machine.worker
```

The portgroup (`dv-SKW-K8s`) and content library + OVA are intentionally kept — they are
expensive to recreate (OVA download). Only the VMs are destroyed.

> **If tofu reports "0 destroyed":** The VMs exist in vSphere but are not in tofu state
> (state file was lost or the VMs were created in a previous session). Destroy them via govc:
> ```bash
> source ~/.bashrc
> for vm in k8s-talos-cp-1 k8s-talos-cp-2 k8s-talos-cp-3 \
>            k8s-talos-worker-1 k8s-talos-worker-2 k8s-talos-worker-3; do
>   govc vm.power -off -force "/SKW/vm/Kubernetes/$vm"
>   govc vm.destroy "/SKW/vm/Kubernetes/$vm"
> done
> ```
> Then proceed with `tofu apply` as normal — it will recreate all 6 VMs.

Then to rebuild:
1. Fresh cluster identity (new PKI): `cd talos && talosctl gen secrets -o secrets.yaml --force`
2. Keep same identity (existing kubeconfigs stay valid): skip step 1.
3. `cd tofu && make apply-bootstrap` — recreates VMs with primary NIC only (minimal guestinfo from `talos/nodes/bootstrap/`).
4. `cd ../talos && make bootstrap-template && make bootstrap-apply` — render + push full configs.
5. `make bootstrap-etcd` — etcd on cp-1.
6. `cd ../tofu && make apply` — adds storage NIC (NIC2) to workers.
7. Continue from Step 3e (fetch kubeconfig, approve CSRs, install Cilium, flux bootstrap, create secrets).

> **Note:** Day-2 config management also uses talm — `make apply NODE=nodes/cp-1.yaml`
> re-renders from the same `templates/` + `nodes/values/` used at bootstrap and applies
> to the running authenticated node. Same tool, same sources, day-0 → day-2.

---

## Design notes

### CoreDNS and Cilium native routing

Talos has a `machine.features.hostDNS.forwardKubeDNSToHost` setting which, when `true`,
patches CoreDNS to forward external queries to the hostDNS proxy at `169.254.20.10` on
the node. This proxy then forwards to the node's configured nameservers.

With Cilium in native routing mode (BGP, no kube-proxy), `169.254.20.10` is not reachable
from pods — Cilium does not set up the necessary routes for link-local addresses. This
causes all external DNS resolution to fail with SERVFAIL.

`talconfig.yaml` sets `forwardKubeDNSToHost: false`. Talos then leaves the CoreDNS
`forward` directive pointing at `/etc/resolv.conf`. Since CoreDNS pods use
`dnsPolicy: Default` they inherit the node's `/etc/resolv.conf`, which Talos populates
from `machine.network.nameservers` (172.16.0.2, 172.16.128.2). External DNS works.

### cluster.network podSubnets / serviceSubnets — must be empty arrays

Cilium in native routing mode assigns each node a host-scope IP in the pod CIDR range
(e.g. 10.244.1.160/32). If `cluster.network.podSubnets` contains e.g. `10.244.0.0/16`,
Talos detects the overlap and fires the `host and Kubernetes pod/service CIDR addresses
overlap` diagnostic, which prevents static IPs from being applied → node unreachable.

**Pitfall:** `talm template --full` emits default subnets (`10.244.0.0/16`,
`10.96.0.0/12`) unless the template explicitly sets `podSubnets: []` and
`serviceSubnets: []` under `cluster.network`. Simply omitting the field in the template
is **not enough** — talm merges in the defaults.

**Fix (already applied):** both `templates/controlplane.yaml` and `templates/worker.yaml`
include the shared helper `k8s-talos.emptyClusterSubnets` (`templates/_helpers.tpl`),
which emits `podSubnets: []` + `serviceSubnets: []`. Any new template must include it.

**Safety net:** `make validate-rendered` (`scripts/validate-rendered.py`) fails if any
rendered `nodes/*.yaml` or `nodes/bootstrap/*.yaml` has non-empty
`cluster.network.{podSubnets,serviceSubnets}`. It is wired as a prereq of
`bootstrap-apply` and runs automatically at the end of `make render`.

### Gateway API CRDs

Gateway API CRDs are committed to `infrastructure/controllers/gateway-api/` rather than
fetched at bootstrap time. This gives Flux explicit control over when they are installed
and ensures `infrastructure-configs` (which creates the Cilium `GatewayClass`) never
runs before the CRD exists. The `infrastructure-controllers` Kustomization has `wait: true`,
so all resources in it — including these CRDs — must be applied before dependent
Kustomizations proceed.

### vSphere CCM + CSI — cloud-provider: external

Kubelets run with `--cloud-provider=external` so every Node joins with
`node.cloudprovider.kubernetes.io/uninitialized:NoSchedule`. vSphere CCM
(`vsphere-cpi` Helm chart in `kube-system`) removes this taint and populates
`spec.providerID` on each Node. vSphere CSI driver (vendored v3.7.0 manifests in
`vmware-system-csi`) requires ProviderID to provision volumes.

- Chart values override: `config.region: ""` + `config.zone: ""` disable vSphere
  tag-category lookup (homelab has no tags — chart defaults `k8s-region`/`k8s-zone`
  would deadlock Node initialization).
- CSI CRDs are vendored separately (`vsphere-csi-crds.yaml`) — upstream manifest
  ships without them; fetched from `pkg/apis` + `pkg/internalapis`.
- `csi-vsphere.conf` uses `insecure-flag = "true"` (vCenter self-signed cert).
- Default `StorageClass vsan` uses `SKW-VSAN-Stripe` vSAN policy, `reclaimPolicy: Retain`,
  `WaitForFirstConsumer`, `allowVolumeExpansion: true`.

### Uninitialized-taint tolerations (bootstrap deadlock prevention)

On a fresh cluster every Node is tainted `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule`
until CCM sees it. Pods that cannot tolerate the taint stay Pending — including Flux,
which would mean CCM never gets reconciled → taint never cleared → permanent deadlock.

Tolerations are applied to:

- Flux controllers (source/kustomize/helm/notification-controller) — via kustomize
  `patches:` block in `kubernetes/bootstrap/flux-system/kustomization.yaml`. Applied
  by `flux bootstrap` (and any subsequent `kubectl apply --server-side --force-conflicts
  -k kubernetes/bootstrap/flux-system/`).
- external-secrets, cert-manager, kubelet-csr-approver, metrics-server — via
  `tolerations` in the HelmRelease `values:` block.
- Cilium (`op: Exists`) and vsphere-cpi tolerate it by default/explicit config.

If adding a new controller that must run before CCM clears the taint (ESO dependency,
for example), add the same toleration to its HelmRelease values.

### Day-2 kubelet flag change — Node re-registration

`talm apply --mode auto` hot-reloads kubelet config without a reboot. When the change
enables a new taint source (e.g. adding `cloud-provider: external`), the Node object
keeps its stale spec — the uninitialized taint is not re-applied. Fix per node:

```bash
kubectl delete node <node-name>
talosctl -n <node-ip> service kubelet restart
# Node re-registers with uninitialized taint → CCM clears it → providerID set.
```

Not needed on fresh bootstrap — first-ever kubelet start registers the Node with the
correct taint from the beginning.

---

## Day-2 Operations

Day-2 Talos configuration is managed by **talm**, same tool as day-0 render. After
initial bootstrap, nodes are authenticated via mTLS (client cert in `talosconfig`)
so `talm apply` runs without `-i`.

All talm commands run from `talos/` with `TALOSCONFIG` set:

```bash
cd ~/git/k8s/cluster-talos/talos/
export TALOSCONFIG=$PWD/clusterconfig/talosconfig
```

### Apply a Talos config change

Edit the relevant template in `talos/templates/` or a per-node values file in
`talos/nodes/values/`. Then:

```bash
# Apply to one node (re-renders then applies):
make apply NODE=nodes/worker-1.yaml

# Apply to all workers (worker-1 → worker-2 → worker-3):
make apply-workers

# Apply to all CPs (cp-3 → cp-2 → cp-1, safest order):
make apply-cps

# Re-render all nodes offline (no live queries) + run validator:
make render
```

Most changes apply without a reboot (`--mode auto`). Changes to install disk, kernel args,
or Talos extensions require a reboot.

After each CP apply: `make etcd-health` to confirm all 3 etcd members healthy.

### Upgrade Talos version

1. Update `installer.version` in `talos/values.yaml`
2. Update `talos_version` in `tofu/variables.tf` (for OVA URL)
3. `cd tofu && make apply` — uploads new OVA (does not touch running VMs)
4. Upgrade nodes via talm, workers first:

```bash
cd talos/
make upgrade NODE=nodes/worker-1.yaml VERSION=v1.13.0
# Repeat for worker-2, worker-3, cp-3, cp-2, cp-1
make etcd-health   # verify after each CP
```

### Check cluster status

```bash
cd talos/
make status         # talosctl machinestatus for all 6 nodes
make etcd-health    # etcd member list
```

### Add a worker node

Active worker count is controlled by `var.worker_count` in `tofu/variables.tf`
(default `2`). It selects the first N entries from the `worker_nodes` map by sorted
key. To add the next worker (assuming its entry and per-node values file already exist,
as is the case for worker-3):

```bash
# 1. Render the new worker's offline config (used by tofu guestinfo + day-2 apply).
cd ~/git/k8s/cluster-talos/talos
make render                       # re-renders all 6 nodes + runs validator

# 2. Create the VM with primary NIC only (OVA 1-NIC limit blocks clone with storage NIC).
cd ../tofu
TF_VAR_worker_count=3 make apply-bootstrap

# 3. Wait for the new worker to Ready.
export KUBECONFIG=~/git/k8s/cluster-talos/talos/clusterconfig/kubeconfig
kubectl get nodes -w

# 4. Hot-add storage NIC (NIC2).
TF_VAR_worker_count=3 make apply

# 5. Re-render day-2 stubs + apply to configure storage NIC live.
cd ../talos
make render
make apply NODE=nodes/worker-3.yaml
```

Once worker-3 is stable, bump the default in `tofu/variables.tf` to `3` and commit
so future `tofu apply` without the env var won't destroy it.

For a new worker that isn't yet declared: first add an entry to `worker_nodes` in
`tofu/variables.tf`, create `talos/nodes/values/worker-N.yaml` (copy from an
existing worker and adjust IPs), then `make render` regenerates the day-2 stub
(`talos/nodes/worker-N.yaml`) + bootstrap config automatically.

**Heterogeneous worker classes** (GPU, storage) should get their own
`vsphere_virtual_machine "worker_<class>"` resource block with its own map + count
variable + talm template — not be mixed into `worker_nodes`. They typically need
different sizing, Talos schematic (e.g. nvidia extensions), and VM host pinning
(PCI passthrough).

### Remove a node

```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
kubectl delete node <node>
# Remove from tofu/variables.tf and talos/nodes/
cd tofu && make apply   # destroys the VM
```

### Upgrade a workload

Edit the HelmRelease or image tag in `kubernetes/` and commit. Flux reconciles automatically.
