variable "vsphere_server" {
  type      = string
  sensitive = true
}

variable "vsphere_user" {
  type      = string
  sensitive = true
}

variable "vsphere_password" {
  type      = string
  sensitive = true
}

variable "vsphere_datacenter" {
  type    = string
  default = "SKW"
}

variable "vsphere_cluster" {
  type    = string
  default = "SKW-Cluster"
}

variable "vsphere_datastore" {
  type    = string
  default = "SKW-VSAN"
}

variable "vsphere_dvs" {
  type    = string
  default = "dv-SKW"
}

variable "vsphere_folder" {
  type    = string
  default = "Kubernetes"
}

variable "cluster_name" {
  type    = string
  default = "k8s-talos"
}

variable "talos_version" {
  type    = string
  default = "v1.12.6"
}

variable "talos_schematic_standard" {
  type        = string
  description = "Talos Image Factory schematic ID for standard nodes (vmtoolsd-guest-agent). Generated via: POST https://factory.talos.dev/schematics with {\"customization\":{\"systemExtensions\":{\"officialExtensions\":[\"siderolabs/vmtoolsd-guest-agent\"]}}}"
  default     = "903b2da78f99adef03cbbd4df6714563823f63218508800751560d3bc3557e40"
}

variable "talos_schematic_gpu" {
  type        = string
  description = <<-EOT
    Talos Image Factory schematic ID for GPU nodes. Adds i915 (Intel ARC),
    intel-ucode, iscsi-tools (Longhorn), util-linux-tools, mei on top of the standard
    vmtoolsd-guest-agent extension. mei is required for Intel Arc HuC firmware
    (MEI GSC); without it 4K HEVC/AV1 QSV encode hangs the GPU. Regenerate via
    POST https://factory.talos.dev/schematics with officialExtensions =
    [vmtoolsd-guest-agent, i915, intel-ucode, iscsi-tools, util-linux-tools, mei].
  EOT
  default     = "d561b3847baff0099e71d536c1144eab0eeab32fada49913901d034a3d7d4503"
}

variable "cp_cpu" {
  type    = number
  default = 4
}

variable "cp_memory_mb" {
  type    = number
  default = 8192
}

variable "cp_disk_size_gb" {
  type    = number
  default = 50
}

variable "worker_cpu" {
  type    = number
  default = 4
}

variable "worker_memory_mb" {
  type    = number
  default = 16384
}

variable "worker_disk_size_gb" {
  type    = number
  default = 100
}

variable "cp_nodes" {
  type = map(object({
    ip = string
  }))
  default = {
    "cp-1" = { ip = "172.16.4.10" }
    "cp-2" = { ip = "172.16.4.11" }
    "cp-3" = { ip = "172.16.4.12" }
  }
}

variable "worker_nodes" {
  type = map(object({
    ip         = string
    storage_ip = string
  }))
  default = {
    "worker-1" = { ip = "172.16.4.20", storage_ip = "10.5.1.20" }
    "worker-2" = { ip = "172.16.4.21", storage_ip = "10.5.1.21" }
    "worker-3" = { ip = "172.16.4.22", storage_ip = "10.5.1.22" }
  }
}

variable "worker_count" {
  description = <<-EOT
    Number of workers from worker_nodes to provision (ordered by sorted key).
    Override via `TF_VAR_worker_count=N` or `-var=worker_count=N`.
    Heterogeneous worker classes (e.g. GPU) should live in their own resource
    block with their own map + count, not be mixed into worker_nodes.
  EOT
  type    = number
  default = 2
}

variable "gpu_worker_nodes" {
  description = <<-EOT
    GPU-capable workers (Intel ARC Dynamic DirectPath). Separate from
    worker_nodes per heterogeneous-worker-class convention. IPs in .30+ range
    to leave .20-.22 free for generic workers.
  EOT
  type = map(object({
    ip         = string
    storage_ip = string
  }))
  default = {
    "gpu-worker-1" = { ip = "172.16.4.30", storage_ip = "10.5.1.30" }
    "gpu-worker-2" = { ip = "172.16.4.31", storage_ip = "10.5.1.31" }
    "gpu-worker-3" = { ip = "172.16.4.32", storage_ip = "10.5.1.32" }
  }
}

variable "gpu_worker_count" {
  description = "Number of GPU workers to provision (ordered by sorted key). Default 1 — scale up as GPU workloads migrate."
  type        = number
  default     = 1
}

variable "gpu_worker_cpu" {
  type    = number
  default = 4
}

variable "gpu_worker_memory_mb" {
  type    = number
  default = 12288
}

variable "gpu_worker_disk_size_gb" {
  type    = number
  default = 100
}

variable "gpu_worker_storage_nic" {
  description = <<-EOT
    Add storage NIC (NIC2, dv-SKW-Storage) to GPU workers.
    Same 1-NIC OVA clone limitation as regular workers — set false on the
    initial `tofu apply -target=...gpu_worker`, then `true` on re-apply.
  EOT
  type    = bool
  default = true
}

variable "gpu_worker_hosts" {
  description = <<-EOT
    Per-GPU-worker ESXi host pinning + Arc A380 PCI BDF for fixed passthrough.
    Replaces Dynamic DirectPath UI attach flow — fully declarative.
    Trade-off: VM pinned to host; ESXi maintenance requires node power-off
    (already true for any PCI passthrough VM). BDFs differ per host — pulled
    from `lspci -p | grep 8086:56a6` on each ESXi.
  EOT
  type = map(object({
    host    = string
    pci_bdf = string
  }))
  default = {
    "gpu-worker-1" = { host = "skw-esxi4.boeye.net", pci_bdf = "0000:03:00.0" }
    "gpu-worker-2" = { host = "skw-esxi5.boeye.net", pci_bdf = "0000:2a:00.0" }
    "gpu-worker-3" = { host = "skw-esxi6.boeye.net", pci_bdf = "0000:2a:00.0" }
  }
}

variable "gpu_pci_enabled" {
  description = <<-EOT
    Attach the pinned Arc A380 via fixed PCI passthrough. Same two-pass
    pattern as gpu_worker_storage_nic: tofu clone + PCI in one apply fails
    (vmware/terraform-provider-vsphere#1918). First -target apply with
    `false`, then re-apply with default `true`.
  EOT
  type    = bool
  default = true
}

variable "worker_storage_nic" {
  description = <<-EOT
    Add storage NIC (NIC2, dv-SKW-Storage) to workers.
    The Talos OVA has only 1 NIC template — adding the storage NIC during initial OVA
    clone causes a vSphere 400 error. Use the two-step process on fresh builds:
      make apply-bootstrap   # step 1: create VMs with primary NIC only
      make apply             # step 2: add storage NIC (VM reconfigure, not clone)
  EOT
  type    = bool
  default = true
}
