TALOSCONFIG := $(CURDIR)/clusterconfig/talosconfig
KUBECONFIG  := $(CURDIR)/clusterconfig/kubeconfig
SECRETS     := $(CURDIR)/secrets.yaml
TALOS_VERSION := v1.12.6
export TALOSCONFIG KUBECONFIG

# ──────────────────────────────────────────────
# Day-2: apply (nodes must be running)
# ──────────────────────────────────────────────
# Two apply paths, chosen per node class:
#
#   workers + GPU workers → `talm apply -f nodes/<name>.yaml --mode auto`
#     Thin modeline-only stub. talm re-renders the class template on-host
#     using live discovery (talm.discovered.*) + cluster-wide values.yaml.
#     All class-level bits (GPU schematic, labels, taints, Longhorn
#     UserVolume, rshared extraMounts) live in the class template.
#
#   CPs → `talosctl apply-config -f nodes/bootstrap/<name>.yaml --mode auto`
#     Full offline render that includes apiCertSANs from values.local.yaml
#     (gitignored). talm apply cannot load values.local.yaml, so any
#     modeline-driven re-render would strip `k8s-api.<domain>` from certSANs.
#
# Background on why talm apply does NOT work for fully-rendered files:
# it silently drops non-patch fields (install.image, nodeLabels, nodeTaints,
# UserVolumeConfig). See memory/feedback_talm_apply_strips_fields.md.

## apply NODE=nodes/<name>.yaml — apply to running node (reboot only if needed).
## Auto-routes: cp-* → talosctl apply-config; worker-*/gpu-worker-* → talm apply.
apply:
	@name=$$(basename $(NODE) .yaml); \
	case $$name in \
		cp-*) \
			ip=$$(yq '.node.ip' nodes/values/$$name.yaml | cut -d/ -f1); \
			echo "=== talosctl apply-config $$name ($$ip) from bootstrap ==="; \
			talosctl apply-config -n $$ip -e 172.16.4.10 \
				-f nodes/bootstrap/$$name.yaml --mode auto ;; \
		worker-*|gpu-worker-*) \
			echo "=== talm apply $$name (modeline re-render) ==="; \
			talm apply -f $(NODE) --mode auto ;; \
		*) echo "unknown node class: $$name"; exit 1 ;; \
	esac

## apply-workers — apply all workers sequentially
apply-workers:
	$(MAKE) apply NODE=nodes/worker-1.yaml
	$(MAKE) apply NODE=nodes/worker-2.yaml
	$(MAKE) apply NODE=nodes/worker-3.yaml

## apply-gpu-workers — apply all GPU workers sequentially
apply-gpu-workers:
	@for n in $$(ls nodes/values/gpu-worker-*.yaml 2>/dev/null | xargs -n1 basename | sed 's/.yaml$$//'); do \
		$(MAKE) apply NODE=nodes/$$n.yaml; \
	done

## apply-cps — apply all CPs sequentially (cp-3 first, cp-1 last)
apply-cps:
	$(MAKE) apply NODE=nodes/cp-3.yaml
	$(MAKE) apply NODE=nodes/cp-2.yaml
	$(MAKE) apply NODE=nodes/cp-1.yaml

## apply-all — apply workers, GPU workers, then CPs
apply-all: apply-workers apply-gpu-workers apply-cps

## upgrade NODE=nodes/worker-1.yaml VERSION=v1.13.0 — upgrade Talos on one node
upgrade:
	talm upgrade -f $(NODE) --image $(shell yq '.machine.install.image' nodes/bootstrap/$(notdir $(NODE)) | cut -d: -f1):$(VERSION)

# ──────────────────────────────────────────────
# Render (bootstrap + day-2 stubs)
# ──────────────────────────────────────────────

## render — regenerate both bootstrap configs (nodes/bootstrap/*.yaml) and
## day-2 stubs (nodes/*.yaml). Safe to run against live cluster (no node queries).
render: render-bootstrap render-day2
	@$(MAKE) validate-rendered

## render-bootstrap — offline-render full machineconfigs to nodes/bootstrap/*.yaml
## for tofu guestinfo injection. Needs values files because no live node exists
## during VM clone.
render-bootstrap:
	@mkdir -p nodes/bootstrap
	@for n in cp-1 cp-2 cp-3; do \
		echo "  bootstrap/$$n..."; \
		talm template --full --offline --with-secrets $(SECRETS) \
			-t templates/controlplane.yaml \
			--values values.local.yaml --values nodes/values/$$n.yaml \
			> nodes/bootstrap/$$n.yaml; \
	done
	@for n in worker-1 worker-2 worker-3; do \
		echo "  bootstrap/$$n..."; \
		talm template --full --offline --with-secrets $(SECRETS) \
			-t templates/worker.yaml --values nodes/values/$$n.yaml \
			> nodes/bootstrap/$$n.yaml; \
	done
	@for n in $$(ls nodes/values/gpu-worker-*.yaml 2>/dev/null | xargs -n1 basename | sed 's/.yaml$$//'); do \
		echo "  bootstrap/$$n..."; \
		talm template --full --offline --with-secrets $(SECRETS) \
			-t templates/worker-gpu.yaml --values nodes/values/$$n.yaml \
			> nodes/bootstrap/$$n.yaml; \
	done

## render-day2 — emit thin modeline-only stubs to nodes/*.yaml. Each stub just
## pins node IP + endpoint + class template; `talm apply` re-renders on-host
## with live discovery. Class-level bits live in the template, not here.
render-day2:
	@mkdir -p nodes
	@set -eu; python3 scripts/render-day2-stubs.py

# ──────────────────────────────────────────────
# Bootstrap (fresh cluster, run once)
# ──────────────────────────────────────────────

## bootstrap-apply — push rendered bootstrap configs to maintenance-mode nodes.
## -i bypasses the patch-diff path → whole-file push. Node reboots to running
## state on the same static IP. Standard workers + GPU workers included.
bootstrap-apply: validate-rendered
	@for n in cp-1 cp-2 cp-3 worker-1 worker-2 worker-3 \
	          $$(ls nodes/values/gpu-worker-*.yaml 2>/dev/null | xargs -n1 basename | sed 's/.yaml$$//'); do \
		echo "=== talm apply $$n (bootstrap) ==="; \
		talm apply -f nodes/bootstrap/$$n.yaml -i; \
	done

## validate-rendered — fail if rendered configs leak talm --full default subnets.
## Cilium native routing assigns pod CIDR itself; presence of non-empty
## cluster.network.{podSubnets,serviceSubnets} triggers Talos overlap diagnostic
## → node unreachable. Helpers/k8s-talos.emptyClusterSubnets must stay included.
validate-rendered:
	@python3 scripts/validate-rendered.py

## talosconfig — generate talosconfig from secrets (needed for talosctl/talm auth)
talosconfig:
	mkdir -p clusterconfig
	talosctl gen config k8s-talos https://172.16.4.1:6443 \
		--with-secrets $(SECRETS) \
		--output-types talosconfig \
		--output clusterconfig/talosconfig \
		--talos-version $(TALOS_VERSION) \
		--force

## bootstrap-etcd — bootstrap etcd on cp-1 (run after VMs are up with static IPs)
bootstrap-etcd:
	talm bootstrap -f nodes/bootstrap/cp-1.yaml

## kubeconfig — fetch kubeconfig and merge into ~/.kube/config
kubeconfig:
	talosctl kubeconfig clusterconfig/kubeconfig \
		--nodes 172.16.4.10 --force
	KUBECONFIG=~/.kube/config:$(CURDIR)/clusterconfig/kubeconfig \
		kubectl config view --merge --flatten > /tmp/merged-kubeconfig
	cp /tmp/merged-kubeconfig ~/.kube/config
	@echo "Rename context: admin@k8s-talos -> k8s-talos"
	kubectl config delete-context k8s-talos 2>/dev/null || true
	kubectl config rename-context admin@k8s-talos k8s-talos

## bootstrap-cilium — install Cilium via Helm before Flux (namespace: cilium)
bootstrap-cilium:
	helm repo add cilium https://helm.cilium.io/ 2>/dev/null || true
	helm repo update cilium
	kubectl create namespace cilium --dry-run=client -o yaml | kubectl apply -f -
	kubectl label --overwrite namespace cilium \
		pod-security.kubernetes.io/enforce=privileged \
		pod-security.kubernetes.io/warn=privileged \
		pod-security.kubernetes.io/audit=privileged
	helm upgrade --install cilium cilium/cilium \
		--version 1.19.3 \
		--namespace cilium \
		--set kubeProxyReplacement=true \
		--set k8sServiceHost=172.16.4.1 \
		--set k8sServicePort=6443 \
		--set routingMode=native \
		--set autoDirectNodeRoutes=true \
		--set ipv4NativeRoutingCIDR=10.244.0.0/16 \
		--set bgpControlPlane.enabled=true \
		--set gatewayAPI.enabled=true \
		--set ipam.mode=cluster-pool \
		--set ipam.operator.clusterPoolIPv4PodCIDRList='{10.244.0.0/16}' \
		--set ipam.operator.clusterPoolIPv4MaskSize=24 \
		--set loadBalancer.algorithm=maglev \
		--set loadBalancer.mode=dsr \
		--set bpf.masquerade=true \
		--set hubble.enabled=true \
		--set hubble.relay.enabled=true \
		--set hubble.ui.enabled=true \
		--set operator.replicas=2 \
		--set securityContext.capabilities.ciliumAgent='{CHOWN,KILL,NET_ADMIN,NET_RAW,IPC_LOCK,SYS_ADMIN,SYS_RESOURCE,DAC_READ_SEARCH,NET_BIND_SERVICE,PERFMON,BPF}' \
		--set securityContext.capabilities.cleanCiliumState='{NET_ADMIN,SYS_ADMIN,SYS_RESOURCE}' \
		--set securityContext.capabilities.mountCgroup='{SYS_ADMIN,SYS_CHROOT,SYS_PTRACE}' \
		--set securityContext.capabilities.applySysctls='{SYS_ADMIN}' \
		--set cgroup.autoMount.enabled=false \
		--set cgroup.hostRoot=/sys/fs/cgroup \
		--wait --timeout 5m

# ──────────────────────────────────────────────
# Status / health
# ──────────────────────────────────────────────

## status — machine status of all live nodes (3 CP + 2 worker + 3 gpu-worker)
status:
	talosctl get machinestatus \
		-n 172.16.4.10,172.16.4.11,172.16.4.12,172.16.4.20,172.16.4.21,172.16.4.30,172.16.4.31,172.16.4.32

## etcd-health — etcd member list (via cp-1)
etcd-health:
	talosctl -n 172.16.4.10 etcd members

.PHONY: apply apply-workers apply-gpu-workers apply-cps apply-all upgrade \
        render render-bootstrap render-day2 bootstrap-apply validate-rendered \
        talosconfig bootstrap-etcd \
        kubeconfig bootstrap-cilium status etcd-health
