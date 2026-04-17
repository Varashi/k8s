locals {
  # Path to minimal bootstrap machine configs (hand-written, committed).
  # Injected via guestinfo to bring each VM up on its final static IP + hostname.
  # Full cluster config (with PKI) is pushed post-boot via `talm apply -i` — see
  # talos/Makefile bootstrap-template + bootstrap-apply targets.
  talos_config_dir = "${path.module}/../talos/nodes/bootstrap"

  # First N workers from worker_nodes (sorted by key). Lets worker_count act as
  # a single knob without forcing all declared workers to provision.
  _sorted_worker_names = sort(keys(var.worker_nodes))
  active_worker_names  = slice(local._sorted_worker_names, 0, var.worker_count)
  active_worker_nodes  = { for k in local.active_worker_names : k => var.worker_nodes[k] }

  _sorted_gpu_worker_names = sort(keys(var.gpu_worker_nodes))
  active_gpu_worker_names  = slice(local._sorted_gpu_worker_names, 0, var.gpu_worker_count)
  active_gpu_worker_nodes  = { for k in local.active_gpu_worker_names : k => var.gpu_worker_nodes[k] }
}

resource "vsphere_virtual_machine" "cp" {
  for_each = var.cp_nodes

  name             = "${var.cluster_name}-${each.key}"
  resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
  datastore_id     = data.vsphere_datastore.datastore.id
  folder           = var.vsphere_folder

  num_cpus                    = var.cp_cpu
  memory                      = var.cp_memory_mb
  guest_id                    = "other5xLinux64Guest"
  hardware_version            = 19
  enable_disk_uuid            = true
  firmware                    = "efi"
  wait_for_guest_net_timeout  = 0    # static IPs; no need to wait for VMware Tools to report
  force_power_off             = false # graceful shutdown via vmtoolsd before hardware changes
  shutdown_wait_timeout       = 5    # minutes to wait for guest OS shutdown

  network_interface {
    network_id = vsphere_distributed_port_group.k8s.id
  }

  disk {
    label            = "disk0"
    size             = var.cp_disk_size_gb
    thin_provisioned = true
  }

  clone {
    template_uuid = vsphere_content_library_item.talos_standard.id
  }

  extra_config = {
    "guestinfo.talos.config"          = base64encode(file("${local.talos_config_dir}/${each.key}.yaml"))
    "guestinfo.talos.config.encoding" = "base64"
  }

  lifecycle {
    # Talos manages its own config after initial bootstrap; don't reset on re-apply
    ignore_changes = [
      extra_config["guestinfo.talos.config"],
      extra_config["guestinfo.talos.config.encoding"],
      clone,
      disk[0].io_reservation,  # vSphere resets this to 1 regardless of config
    ]
  }
}

# GPU worker — Intel ARC fixed PCI passthrough, host-pinned.
#
# Each GPU worker is pinned to a specific ESXi host (gpu_worker_hosts) with a
# fixed BDF. Fully declarative; no vSphere UI step. Trade-off vs Dynamic
# DirectPath: node can't cold-migrate between hosts. Acceptable because PCI
# passthrough already blocks vMotion, and ESXi maintenance already requires
# node power-off. With 3 GPU workers + 3-replica Longhorn, one node off during
# maintenance is failsafe.
#
# Two-pass deploy (tofu limitation, vmware/terraform-provider-vsphere#1918):
#   1. tofu apply -target='...["gpu-worker-N"]' -var=gpu_worker_storage_nic=false \
#                                               -var=gpu_pci_enabled=false
#   2. tofu apply -target='...["gpu-worker-N"]'   # adds storage NIC + PCI
#
# Second VMDK on SKW-VSAN is for Longhorn (local-disk-style storage bound to
# this node; replaces vSphere CSI for GPU-node workloads where vSAN FCD
# snapshots misbehave under passthrough).
resource "vsphere_virtual_machine" "gpu_worker" {
  for_each = local.active_gpu_worker_nodes

  name             = "${var.cluster_name}-${each.key}"
  resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
  host_system_id   = data.vsphere_host.gpu_worker[each.key].id
  datastore_id     = data.vsphere_datastore.datastore.id
  folder           = var.vsphere_folder

  num_cpus                          = var.gpu_worker_cpu
  memory                            = var.gpu_worker_memory_mb
  memory_reservation                = var.gpu_worker_memory_mb
  memory_reservation_locked_to_max  = true
  # latency_sensitivity=high would require full CPU reservation too; skipped.
  # Memory reservation locked to max is sufficient for PCI passthrough.
  guest_id                          = "other5xLinux64Guest"
  hardware_version                  = 19
  enable_disk_uuid                  = true
  firmware                          = "efi"
  wait_for_guest_net_timeout        = 0
  force_power_off                   = false
  shutdown_wait_timeout             = 5

  network_interface {
    network_id = vsphere_distributed_port_group.k8s.id
  }

  dynamic "network_interface" {
    for_each = var.gpu_worker_storage_nic ? [1] : []
    content {
      network_id = data.vsphere_network.storage.id
    }
  }

  disk {
    label            = "disk0"
    size             = var.gpu_worker_disk_size_gb
    thin_provisioned = true
  }

  disk {
    label            = "disk1"
    size             = var.gpu_worker_longhorn_disk_gb
    thin_provisioned = true
    unit_number      = 1
  }

  pci_device_id = var.gpu_pci_enabled ? [var.gpu_worker_hosts[each.key].pci_bdf] : []

  clone {
    template_uuid = vsphere_content_library_item.talos_gpu.id
  }

  extra_config = {
    "guestinfo.talos.config"          = base64encode(file("${local.talos_config_dir}/${each.key}.yaml"))
    "guestinfo.talos.config.encoding" = "base64"
  }

  lifecycle {
    ignore_changes = [
      extra_config["guestinfo.talos.config"],
      extra_config["guestinfo.talos.config.encoding"],
      clone,
      disk[0].io_reservation,
      disk[1].io_reservation,
    ]
  }
}

resource "vsphere_virtual_machine" "worker" {
  for_each = local.active_worker_nodes

  name             = "${var.cluster_name}-${each.key}"
  resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
  datastore_id     = data.vsphere_datastore.datastore.id
  folder           = var.vsphere_folder

  num_cpus                    = var.worker_cpu
  memory                      = var.worker_memory_mb
  memory_hot_add_enabled      = true
  cpu_hot_add_enabled         = true
  guest_id                    = "other5xLinux64Guest"
  hardware_version            = 19
  enable_disk_uuid            = true
  firmware                    = "efi"
  wait_for_guest_net_timeout  = 0    # static IPs; no need to wait for VMware Tools to report
  force_power_off             = false # graceful shutdown via vmtoolsd before hardware changes
  shutdown_wait_timeout       = 5    # minutes to wait for guest OS shutdown

  network_interface {
    network_id = vsphere_distributed_port_group.k8s.id
  }

  dynamic "network_interface" {
    for_each = var.worker_storage_nic ? [1] : []
    content {
      network_id = data.vsphere_network.storage.id
    }
  }

  disk {
    label            = "disk0"
    size             = var.worker_disk_size_gb
    thin_provisioned = true
  }

  clone {
    template_uuid = vsphere_content_library_item.talos_standard.id
  }

  extra_config = {
    "guestinfo.talos.config"          = base64encode(file("${local.talos_config_dir}/${each.key}.yaml"))
    "guestinfo.talos.config.encoding" = "base64"
  }

  lifecycle {
    ignore_changes = [
      extra_config["guestinfo.talos.config"],
      extra_config["guestinfo.talos.config.encoding"],
      clone,
      disk[0].io_reservation,  # vSphere resets this to 1 regardless of config
    ]
  }
}
