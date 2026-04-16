TALOSCONFIG := $(CURDIR)/clusterconfig/talosconfig
KUBECONFIG  := $(CURDIR)/clusterconfig/kubeconfig
SECRETS     := $(CURDIR)/secrets.yaml
TALOS_VERSION := v1.12.6
export TALOSCONFIG KUBECONFIG

# ──────────────────────────────────────────────
# Day-2: apply (nodes must be running)
# ──────────────────────────────────────────────

## apply NODE=nodes/cp-1.yaml — apply rendered template to running node (reboot only if needed)
apply:
	talm apply -f $(NODE) --mode auto

## apply-workers — apply all workers sequentially
apply-workers:
	$(MAKE) apply NODE=nodes/worker-1.yaml
	$(MAKE) apply NODE=nodes/worker-2.yaml
	$(MAKE) apply NODE=nodes/worker-3.yaml

## apply-cps — apply all CPs sequentially (cp-3 first, cp-1 last)
apply-cps:
	$(MAKE) apply NODE=nodes/cp-3.yaml
	$(MAKE) apply NODE=nodes/cp-2.yaml
	$(MAKE) apply NODE=nodes/cp-1.yaml

## apply-all — apply workers then CPs
apply-all: apply-workers apply-cps

## upgrade NODE=nodes/worker-1.yaml VERSION=v1.13.0 — upgrade Talos on one node
upgrade:
	talm upgrade -f $(NODE) --image $(shell yq '.machine.install.image' nodes/patches/$(notdir $(NODE)) | cut -d: -f1):$(VERSION)

# ──────────────────────────────────────────────
# Bootstrap (fresh cluster, run once)
# ──────────────────────────────────────────────

## render — offline-render all node configs into nodes/*.yaml AND
## nodes/bootstrap/*.yaml. Safe to run against a live cluster (no node queries).
## Use after template/values changes to keep day-2 apply + tofu guestinfo
## injection in sync.
render:
	@mkdir -p nodes nodes/bootstrap
	@for n in cp-1 cp-2 cp-3; do \
		echo "  $$n..."; \
		talm template --full --offline --with-secrets $(SECRETS) \
			-t templates/controlplane.yaml \
			--values values.local.yaml --values nodes/values/$$n.yaml \
			> nodes/$$n.yaml; \
		cp nodes/$$n.yaml nodes/bootstrap/$$n.yaml; \
	done
	@for n in worker-1 worker-2 worker-3; do \
		echo "  $$n..."; \
		talm template --full --offline --with-secrets $(SECRETS) \
			-t templates/worker.yaml --values nodes/values/$$n.yaml \
			> nodes/$$n.yaml; \
		cp nodes/$$n.yaml nodes/bootstrap/$$n.yaml; \
	done
	@# GPU workers: rendered from worker.yaml template; per-node values override
	@# installer image to the GPU schematic (i915/intel-ucode/iscsi/util-linux).
	@for n in $$(ls nodes/values/gpu-worker-*.yaml 2>/dev/null | xargs -n1 basename | sed 's/.yaml$$//'); do \
		echo "  $$n..."; \
		talm template --full --offline --with-secrets $(SECRETS) \
			-t templates/worker.yaml --values nodes/values/$$n.yaml \
			> nodes/$$n.yaml; \
		cp nodes/$$n.yaml nodes/bootstrap/$$n.yaml; \
	done
	@$(MAKE) validate-rendered

## bootstrap-template — fresh-cluster online render via maintenance-mode nodes.
## Prereq: VMs up on static IPs in maintenance mode (minimal guestinfo via tofu).
## Use `make render` instead for day-2 template changes on running cluster.
bootstrap-template:
	@mkdir -p nodes
	@echo "=== Rendering CP configs ==="
	@for i in 1 2 3; do \
		ip=172.16.4.1$$i; \
		echo "  cp-$$i ($$ip)..."; \
		talm template -n $$ip -e $$ip \
			-t templates/controlplane.yaml \
			--values values.local.yaml --values nodes/values/cp-$$i.yaml \
			--full --with-secrets $(SECRETS) -i \
			> nodes/cp-$$i.yaml; \
	done
	@echo "=== Rendering worker configs ==="
	@for i in 1 2 3; do \
		ip=172.16.4.2$$i; \
		echo "  worker-$$i ($$ip)..."; \
		talm template -n $$ip -e $$ip \
			-t templates/worker.yaml \
			--values nodes/values/worker-$$i.yaml \
			--full --with-secrets $(SECRETS) -i \
			> nodes/worker-$$i.yaml; \
	done
	@echo "Done. Next: make bootstrap-apply"

## bootstrap-apply — push rendered configs to maintenance-mode nodes (insecure)
## Node reboots into running state on same static IP after apply.
bootstrap-apply: validate-rendered
	@for n in cp-1 cp-2 cp-3 worker-1 worker-2 worker-3; do \
		echo "=== talm apply $$n ==="; \
		talm apply -f nodes/$$n.yaml -i; \
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
	talm bootstrap -f nodes/cp-1.yaml

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
		--version 1.19.2 \
		--namespace cilium \
		--set kubeProxyReplacement=true \
		--set k8sServiceHost=172.16.4.1 \
		--set k8sServicePort=6443 \
		--set routingMode=native \
		--set autoDirectNodeRoutes=true \
		--set ipv4NativeRoutingCIDR=10.244.0.0/16 \
		--set bgpControlPlane.enabled=true \
		--set gatewayAPI.enabled=true \
		--set ipam.mode=kubernetes \
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

## status — machine status of all live nodes (3 CP + 2 worker + 2 gpu-worker)
status:
	talosctl get machinestatus \
		-n 172.16.4.10,172.16.4.11,172.16.4.12,172.16.4.20,172.16.4.21,172.16.4.30,172.16.4.31

## etcd-health — etcd member list (via cp-1)
etcd-health:
	talosctl -n 172.16.4.10 etcd members

.PHONY: apply apply-workers apply-cps apply-all upgrade \
        render bootstrap-template bootstrap-apply validate-rendered \
        talosconfig bootstrap-etcd \
        kubeconfig bootstrap-cilium status etcd-health
