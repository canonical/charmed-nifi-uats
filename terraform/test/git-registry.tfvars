# Variables for the git-registry suite, applied on top of the ones the justfile
# generates. The model UUID, channel and sensitive properties key are injected at
# runtime; only what this scenario needs belongs here.

# NiFi creates its flow registry client from this relation, so the suite has
# nothing to assert without it. No other suite needs it.
git_integrator = {
  enabled = true
}
