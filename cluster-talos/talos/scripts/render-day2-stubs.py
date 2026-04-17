#!/usr/bin/env python3
"""
Emit thin modeline stubs to nodes/<name>.yaml from nodes/values/<name>.yaml.

Each stub is a modeline that talm reads; `talm apply` re-renders the referenced
class template on-host using live discovery (talm.discovered.*) plus the
cluster-wide values.yaml. All class-level bits (labels, taints, schematic,
Longhorn UserVolume) live in the class template — NOT in the stub.

Classes:
  cp-*          → templates/controlplane.yaml
  worker-*      → templates/worker.yaml
  gpu-worker-*  → templates/worker-gpu.yaml

Endpoint is always cp-1 (172.16.4.10). nodes= is the node's primary IP (CIDR
stripped) so talm contacts it directly.

CP stubs inline `cluster.apiServer.certSANs` from values.local.yaml. Reason:
talm apply only loads the chart's values.yaml — values.local.yaml is not
passed at apply time, so empty default `apiCertSANs: []` would strip the
live certSAN on day-2 apply. Nodes/*.yaml are gitignored, so inlining
identifiable DNS names here does not leak them into the committed tree.
"""

import glob
import os
import sys
import yaml

CP_ENDPOINT = "172.16.4.10"
VALUES_DIR = "nodes/values"
OUT_DIR = "nodes"
LOCAL_VALUES = "values.local.yaml"


def classify(name: str) -> str:
    if name.startswith("cp-"):
        return "templates/controlplane.yaml"
    if name.startswith("gpu-worker-"):
        return "templates/worker-gpu.yaml"
    if name.startswith("worker-"):
        return "templates/worker.yaml"
    raise SystemExit(f"unknown node class: {name}")


def node_ip(values_path: str) -> str:
    with open(values_path) as f:
        v = yaml.safe_load(f) or {}
    ip = (v.get("node") or {}).get("ip") or ""
    if not ip:
        raise SystemExit(f"{values_path}: node.ip missing")
    return ip.split("/")[0]


def local_cert_sans() -> list[str]:
    if not os.path.exists(LOCAL_VALUES):
        return []
    with open(LOCAL_VALUES) as f:
        v = yaml.safe_load(f) or {}
    return ((v.get("cluster") or {}).get("apiCertSANs")) or []


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    paths = sorted(glob.glob(f"{VALUES_DIR}/*.yaml"))
    if not paths:
        sys.exit(f"no values files under {VALUES_DIR}")
    sans = local_cert_sans()
    for p in paths:
        name = os.path.splitext(os.path.basename(p))[0]
        tmpl = classify(name)
        ip = node_ip(p)
        out = f"{OUT_DIR}/{name}.yaml"
        with open(out, "w") as f:
            f.write(
                f'# talm: nodes=["{ip}"], endpoints=["{CP_ENDPOINT}"], '
                f'templates=["{tmpl}"]\n'
            )
            if name.startswith("cp-") and sans:
                f.write("cluster:\n  apiServer:\n    certSANs:\n")
                for s in sans:
                    f.write(f"      - {s}\n")
        print(f"  {out}")


if __name__ == "__main__":
    main()
