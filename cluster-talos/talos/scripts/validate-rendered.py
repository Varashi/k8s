#!/usr/bin/env python3
"""
Fail if any rendered Talos config has non-empty cluster.network.podSubnets or
serviceSubnets. talm --full emits 10.244.0.0/16 + 10.96.0.0/12 defaults; Cilium
native routing assigns pod CIDR itself, so presence of those here triggers a
Talos overlap diagnostic that leaves the node unreachable. Templates must
include k8s-talos.emptyClusterSubnets under cluster.network.
"""

import glob
import sys
import yaml

patterns = ["nodes/*.yaml", "nodes/bootstrap/*.yaml"]
files = sorted({f for p in patterns for f in glob.glob(p)})

fail = False
for f in files:
    with open(f) as fh:
        docs = list(yaml.safe_load_all(fh))
    machine_cfg = docs[0] if docs else {}
    net = (machine_cfg or {}).get("cluster", {}).get("network", {}) or {}
    pod = net.get("podSubnets") or []
    svc = net.get("serviceSubnets") or []
    if pod or svc:
        print(f"FAIL: {f} podSubnets={pod} serviceSubnets={svc}")
        fail = True

if fail:
    sys.exit(1)
print(f"OK: {len(files)} rendered config(s) have empty cluster subnets")
