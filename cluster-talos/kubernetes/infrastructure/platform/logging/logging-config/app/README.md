# `logging/` — cluster log forwarding to vcflogs via Logging Operator

Replaces the previous `tanzu-system-logging/` stack (standalone
fluent-bit DS + custom `vcflogs-cfapi-adapter` sidecar). Migrated
2026-05-27 to the [Logging Operator](https://kube-logging.dev/)
pattern so the VMware Aria CFAPI translation comes from a
maintained-by-VMware plugin instead of code we own.

## Architecture

```
/var/log/containers/*.log   on every node
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│  Fluent Bit DaemonSet (operator-managed, ns=logging)         │
│    INPUT  tail        /var/log/containers/*.log              │
│    FILTER kubernetes  enrich w/ pod/ns/labels                │
│    OUTPUT forward     → Fluentd Service (operator-managed)   │
└─────────────────────────────┬────────────────────────────────┘
                              │ Fluentd forward protocol
                              │ (port 24240, in-cluster)
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Fluentd StatefulSet ×2 (HA, operator-managed)               │
│    @type forward                                             │
│    @type vmware_loginsight (fluent-plugin-vmware-loginsight) │
│      → CFAPI POST {"events":[…]}                             │
└─────────────────────────────┬────────────────────────────────┘
                              │ HTTPS POST
                              ▼
              skw-vcflogs.boeye.net:9543
              /api/v1/events/ingest/k8s-talos
```

No 2048-byte syslog cap. No homemade adapter. The
`fluent-plugin-vmware-loginsight` gem (v1.4.2) is bundled in the
operator's `ghcr.io/kube-logging/fluentd:v1.17-5.0-full` image —
nothing to build.

## CRD breakdown

| File | CRD | Purpose |
|---|---|---|
| `helmrelease-operator.yaml` | `HelmRelease` | Installs the operator + CRDs |
| `logging.yaml` | `Logging` | Declares the pipeline (which Fluent Bit + Fluentd specs to render) |
| `clusteroutput-vcflogs.yaml` | `ClusterOutput` | The vmwareLogInsight destination, cluster-scoped |
| `clusterflow-all.yaml` | `ClusterFlow` | "Match everything → send to vcflogs" |

The split between Logging (infrastructure) and Flow/Output (routing)
is intentional in the operator design — Logging is platform, Flow
+ Output is policy. At a multi-tenant work-scale, namespaces would
get their own `Flow` CRs (namespace-scoped, can only target outputs
their team owns), while ops would manage `ClusterFlow` /
`ClusterOutput` for cross-cutting destinations.

## What's where in the cluster

- **`logging` namespace** holds the operator pod + Fluent Bit DS + Fluentd STS
- **Fluent Bit pods** mount `hostPath: /var/log/containers` to read CRI logs
- **Fluentd pods** mount `5Gi` PVC each (`longhorn` StorageClass) for the file buffer that absorbs vcflogs back-pressure
- **leader election** uses a Lease in this ns

## Tuning knobs

| What | Where |
|---|---|
| Fluent Bit resources / tolerations | `logging.yaml` → `spec.fluentbit` |
| Fluentd replicas (HA) | `logging.yaml` → `spec.fluentd.scaling.replicas` |
| Fluentd buffer size / storage class | `logging.yaml` → `spec.fluentd.bufferStorageVolume.pvc` |
| CFAPI endpoint / TLS posture | `clusteroutput-vcflogs.yaml` → `spec.vmwareLogInsight` |
| Buffer flush cadence / retry | `clusteroutput-vcflogs.yaml` → `spec.vmwareLogInsight.buffer` |
| Per-namespace routing | replace `clusterflow-all.yaml` with multiple `Flow` / `ClusterFlow` CRs |

## Reverting (if needed)

```
git revert <merge-commit-of-this-PR>
flux reconcile kustomization platform -n flux-system
```

This re-creates the old `tanzu-system-logging/fluent-bit` HelmRelease.
The `vcflogs-cfapi-adapter` ghcr.io image was deleted with this
migration — re-installing would put the cluster back on the
2048-byte syslog cap until the image is rebuilt and republished
from `git history`.

## References

- [Logging Operator docs](https://kube-logging.dev/docs/)
- [`vmwareLogInsight` output reference](https://kube-logging.dev/docs/configuration/plugins/outputs/vmware_loginsight/)
- [`fluent-plugin-vmware-loginsight` upstream (archived)](https://github.com/vmware-archive/fluent-plugin-vmware-loginsight)
- VMware Aria Operations for Logs [ingest API](https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/)
- Predecessor: PR Varashi/k8s#151 (homemade vcflogs-cfapi-adapter sidecar)
