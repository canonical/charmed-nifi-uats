# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Deploy Charmed NiFi through the solution module
module "charmed_nifi" {
  source     = "git::https://github.com/canonical/charmed-nifi-solutions//modules/charmed-nifi?ref=track/2.10"
  model_uuid = var.model_uuid

  nifi_k8s = {
    channel  = var.channel
    revision = var.revision
    config = {
      "sensitive-props-key" = var.sensitive_props_key
    }
  }

  traefik = {
    enabled = var.traefik.enabled
    channel = var.traefik.channel
  }

  git_integrator = {
    enabled = var.git_integrator.enabled
    channel = var.git_integrator.channel
    config = {
      repository_url = var.git_integrator.repository_url
      tracking_ref   = var.git_integrator.tracking_ref
    }
  }
}
