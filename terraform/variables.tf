# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

variable "model_uuid" {
  description = "UUID of the Juju model to deploy Charmed NiFi into."
  type        = string
}

variable "channel" {
  description = "Charmhub channel to deploy nifi-k8s from."
  type        = string
  default     = "2.10/edge"
}

variable "revision" {
  description = "Charm revision to deploy. Null deploys the channel head."
  type        = number
  default     = null
}

variable "sensitive_props_key" {
  description = "Value for nifi.sensitive.props.key. The charmed-nifi module turns this into a Juju user secret and grants it to NiFi."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.sensitive_props_key) >= 12
    error_message = "sensitive_props_key must be at least 12 characters; the charm blocks otherwise."
  }
}

variable "traefik" {
  description = "Ingress provider. Enabled by default so the UI and API are reachable from outside the cluster."
  type = object({
    enabled = optional(bool, true)
    channel = optional(string, "latest/stable")
  })
  default = {}
}
