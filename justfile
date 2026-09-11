# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

set export := true

MODEL_DEFAULT := "nifi-uats"
TFVARS := "uats.tfvars"

[private]
default:
    @just --list

[private]
initialize:
    #!/usr/bin/bash
    set -euo pipefail
    if [ ! -d "terraform/.terraform" ]; then
        terraform -chdir=terraform init
    fi

[private]
destroy-model model_name:
    juju destroy-model --no-prompt --destroy-storage --force ${model_name} || true

[private]
add-model model_name: (destroy-model model_name)
    juju add-model ${model_name}

# Write the tfvars file: model UUID, channel, revision and a fresh sensitive properties key.
[private]
write-tfvars model_name channel revision="":
    #!/usr/bin/bash
    set -euo pipefail

    MODEL_UUID=$(juju show-model ${model_name} --format=json | jq -er ".[\"${model_name}\"][\"model-uuid\"]")

    # Hex rather than base64: the base64 alphabet includes "/", which is awkward
    # for anything that later substitutes this value into a file.
    KEY=$(openssl rand -hex 16)

    cat > "terraform/${TFVARS}" <<EOF
    model_uuid          = "${MODEL_UUID}"
    channel             = "${channel}"
    sensitive_props_key = "${KEY}"
    EOF

    if [ -n "${revision}" ]; then
        echo "revision = ${revision}" >> "terraform/${TFVARS}"
    fi

# Wait until every application in the model is active.
wait-for-active model_name=MODEL_DEFAULT:
    #!/usr/bin/bash
    set -euo pipefail

    timeout=1800
    elapsed=0
    interval=15
    while [ ${elapsed} -lt ${timeout} ]; do
        # Polled in a loop rather than one long wait-for: juju sometimes reports a
        # stale model state on a single long-running query.
        if juju wait-for model ${model_name} \
            --query='forEach(applications, app => app.status == "active")' \
            --timeout=${interval}s 2>/dev/null; then
            exit 0
        fi
        elapsed=$((elapsed + interval))
    done
    echo "Timed out after ${timeout}s waiting for ${model_name} to become active" >&2
    exit 1

# Deploy Charmed NiFi for the UATs, from a given channel and optional revision.
deploy model_name=MODEL_DEFAULT channel="2.10/edge" revision="": (add-model model_name) (initialize)
    #!/usr/bin/bash
    set -euxo pipefail

    just write-tfvars ${model_name} ${channel} "${revision}"
    terraform -chdir=terraform apply -auto-approve -var-file="${TFVARS}"
    just wait-for-active ${model_name}

    echo "Charmed NiFi deployed in model ${model_name}."

# Destroy the deployment and the model.
destroy model_name=MODEL_DEFAULT:
    #!/usr/bin/bash
    set -euxo pipefail

    if [ -f "terraform/${TFVARS}" ]; then
        terraform -chdir=terraform destroy -auto-approve -var-file="${TFVARS}" || true
        rm -f "terraform/${TFVARS}"
    fi
    just destroy-model ${model_name}

# Run the framework's own tests, proving the fixtures attach to a deployment
test-framework model_name=MODEL_DEFAULT:
    uv tool run --python 3.12 tox -e framework -- --model=${model_name}

# Lint python code
lint:
    uv tool run --python 3.12 tox -e lint

# Apply formatting to python code
format:
    uv tool run --python 3.12 tox -e format

# Terraform format and validate
fmt: (initialize)
    terraform -chdir=terraform fmt -recursive
    terraform -chdir=terraform validate
