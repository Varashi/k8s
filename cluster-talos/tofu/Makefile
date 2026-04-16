plan:
	tofu plan

apply:
	tofu apply -parallelism=1 -auto-approve

# Fresh cluster rebuild: OVA has 1 NIC template; adding the storage NIC during clone fails.
# Run apply-bootstrap first (primary NIC only), then apply to add storage NIC via VM reconfigure.
apply-bootstrap:
	tofu apply -parallelism=1 -auto-approve -var="worker_storage_nic=false"

init:
	tofu init

.PHONY: plan apply apply-bootstrap init
