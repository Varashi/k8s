terraform {
  required_providers {
    vsphere = {
      source  = "hashicorp/vsphere"
      version = "~> 2.10"
    }
  }
}

provider "vsphere" {
  user                 = var.vsphere_user
  password             = var.vsphere_password
  vsphere_server       = var.vsphere_server
  allow_unverified_ssl = true
}

data "vsphere_datacenter" "dc" {
  name = var.vsphere_datacenter
}

data "vsphere_compute_cluster" "cluster" {
  name          = var.vsphere_cluster
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "datastore" {
  name          = var.vsphere_datastore
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_distributed_virtual_switch" "dvs" {
  name          = var.vsphere_dvs
  datacenter_id = data.vsphere_datacenter.dc.id
}

# Existing VLAN 101 port group for direct TrueNAS NFS access
data "vsphere_network" "storage" {
  name          = "dv-SKW-Storage"
  datacenter_id = data.vsphere_datacenter.dc.id
}

# ESXi hosts pinned per GPU worker (fixed PCI passthrough requires host match).
data "vsphere_host" "gpu_worker" {
  for_each      = var.gpu_worker_hosts
  name          = each.value.host
  datacenter_id = data.vsphere_datacenter.dc.id
}

# VLAN 104 port group for k8s-talos cluster
resource "vsphere_distributed_port_group" "k8s" {
  name                            = "dv-SKW-K8s"
  distributed_virtual_switch_uuid = data.vsphere_distributed_virtual_switch.dvs.id
  vlan_id                         = 104
  type                            = "earlyBinding"
  number_of_ports                 = 128
}

# Content library for Talos OVA images
resource "vsphere_content_library" "talos" {
  name = "k8s-talos"
  storage_backing = [data.vsphere_datastore.datastore.id]
}

# Standard Talos OVA (vmtoolsd extension)
resource "vsphere_content_library_item" "talos_standard" {
  name       = "talos-${var.talos_version}-standard"
  library_id = vsphere_content_library.talos.id
  file_url   = "https://factory.talos.dev/image/${var.talos_schematic_standard}/${var.talos_version}/vmware-amd64.ova"
  type       = "ovf"
}

# GPU Talos OVA (adds i915, intel-ucode, iscsi-tools, util-linux-tools)
resource "vsphere_content_library_item" "talos_gpu" {
  name       = "talos-${var.talos_version}-gpu"
  library_id = vsphere_content_library.talos.id
  file_url   = "https://factory.talos.dev/image/${var.talos_schematic_gpu}/${var.talos_version}/vmware-amd64.ova"
  type       = "ovf"
}
