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
  index of the six top-level Flux Kustomizations in the cluster.
- `kubernetes/infrastructure/` — cluster-wide controllers, operators, and
  ops tooling:
  - `infrastructure/flux-system/` — Flux Operator + FluxInstance + the
    `flux-repositories/` `HelmRepository` / `OCIRepository` / `GitRepository`
    sources consumed by `HelmRelease`s and child Kustomizations.
  - `infrastructure/core/` — CNI, CRDs, storage drivers, cert-manager, ESO,
    etcd backup (everything the `platform` tier depends on).
  - `infrastructure/platform/` — higher-level platform components
    (monitoring, configs, external-dns, Cloudflare, Longhorn, Kasten,
    Spegel, Renovate, log forwarding).
- `kubernetes/apps/` — user-facing workloads, nested by category
  (`arr/`, `downloaders/`, `media/`, `tools/`); each app ships its own
  Flux `Kustomization` + `HelmRelease` + `HTTPRoute`/`TunnelBinding`.
- `kubernetes/forwarders/` — routing-only shims (external `Service` +
  `HTTPRoute` [+ `TunnelBinding`]) for off-cluster apps like Home
  Assistant and NZBGet.

## CI

PR-gated GitHub Actions workflows in `.github/workflows/`:

- **flux-diff** — runs `allenporter/flux-local/action/diff` on any PR touching
  `cluster-talos/kubernetes/**`; posts unified HelmRelease and Kustomization
  diffs as idempotent PR comments (one per resource type, edited in place on
  follow-up commits).
- **renovate-validate** — runs `renovate-config-validator --strict` on any PR
  touching `renovate.json`.
- **lint** — runs `yamlfmt -lint` on any PR touching `**/*.{yaml,yml}` (or the
  tooling configs), using the pinned toolchain from `.mise.toml`.
- **build-images** — matrix build for custom GHCR images under
  `cluster-talos/kubernetes/apps/tools/*/image/` (currently `netbox-plus`
  and `octodns`). Triggers on pushes to `main` that touch those paths;
  pushes `:latest` + `sha-<short>` to
  `ghcr.io/${{ github.repository_owner }}/<image>`. Consumers pin by
  digest so Renovate can auto-bump (see each app's `README.md`).

Local tooling (`.mise.toml`, `.yamlfmt`, `lefthook.yml`) mirrors CI; devs run
`mise install && lefthook install` once per clone to enforce formatting
pre-commit.

## License

Individual tools under subdirectories declare their own licenses (e.g.
`gpu-node-vsphere-maintenance-controller/LICENSE`). The cluster
configuration itself is provided as-is; feel free to borrow ideas, but
treat credentials, hostnames, and secret references as homelab-specific.
