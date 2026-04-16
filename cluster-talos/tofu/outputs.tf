output "cp_ips" {
  description = "Control plane node IP addresses"
  value       = { for k, v in var.cp_nodes : k => v.ip }
}

output "worker_ips" {
  description = "Worker node IP addresses (active workers only)"
  value       = { for k, v in local.active_worker_nodes : k => v.ip }
}

output "gpu_worker_ips" {
  description = "GPU worker node IP addresses (active GPU workers only)"
  value       = { for k, v in local.active_gpu_worker_nodes : k => v.ip }
}

output "api_vip" {
  description = "Kubernetes API endpoint"
  value       = "https://172.16.4.1:6443"
}
