# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

output "nifi_app_name" {
  description = "Name of the deployed NiFi application."
  value       = module.charmed_nifi.nifi_k8s.name
}

output "traefik_app_name" {
  description = "Name of the deployed Traefik application (null when not enabled)."
  value       = var.traefik.enabled ? module.charmed_nifi.traefik.name : null
}
