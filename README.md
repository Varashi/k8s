# k8s

Homelab Kubernetes — cluster configuration and GitOps manifests for a single
Talos cluster, managed via [FluxCD](https://fluxcd.io/) against this repo.

## Cluster

| Directory         | Status     | Distribution                     | Notes                                                       |
|-------------------|------------|----------------------------------|-------------------------------------------------------------|
| `cluster-talos/`  | **Active** | Talos Linux + vanilla Kubernetes | Primary cluster; Cilium, Gateway API, ESO, Kasten, Longhorn |

`cluster-talos/` ships OpenTofu + talm configs under `tofu/` and `talos/` for
day-0 and day-2 cluster management.

## Related tools

Reusable tooling extracted from this homelab lives in its own repository:

- [`gpu-node-vsphere-maintenance-controller`](https://github.com/Varashi/gpu-node-vsphere-maintenance-controller) —
  Kubernetes controller that safely handles ESXi maintenance-mode
  transitions for worker VMs using PCI passthrough.

## Layout conventions

- `kubernetes/bootstrap/` — Flux bootstrap and the `cluster-kustomizations.yaml`
  index of every Flux Kustomization in the cluster.
- `kubernetes/infrastructure/` — cluster-wide controllers, CRDs, operators,
  and ops tooling (cert-manager, ESO, Cilium values, CSI/CPI, Kasten, etc.).
- `kubernetes/apps/` — user-facing workloads, each in its own namespace
  with namespace, `HelmRelease` or manifests, `HTTPRoute`, and
  `TunnelBinding` where applicable.
- `kubernetes/flux/` — reference copies of Flux Kustomizations; the
  bootstrap file remains the source of truth.

## License

Individual tools under subdirectories declare their own licenses (e.g.
`gpu-node-vsphere-maintenance-controller/LICENSE`). The cluster
configuration itself is provided as-is; feel free to borrow ideas, but
treat credentials, hostnames, and secret references as homelab-specific.
