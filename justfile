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

# Print the NiFi and Traefik application names from the Terraform outputs,
# space-separated. The Traefik name is empty when ingress is not enabled.
[private]
app-names:
    #!/usr/bin/bash
    set -euo pipefail

    outputs=$(terraform -chdir=terraform output -json)
    nifi_app=$(jq -er '.nifi_app_name.value' <<< "${outputs}")
    # A null output is left out of state, so a disabled Traefik has no key here.
    traefik_app=$(jq -r '.traefik_app_name.value // empty' <<< "${outputs}")

    echo "${nifi_app} ${traefik_app}"

# Check the deployment is up before a suite runs, so that a failure inside the
# suite is a real failure rather than an under-deployed model.
[private]
preflight model_name nifi_app traefik_app="":
    MODEL="${model_name}" NIFI_APP="${nifi_app}" TRAEFIK_APP="${traefik_app}" \
        goss --gossfile tests/goss/goss.yaml validate --retry-timeout 900s --sleep 15s --color

# Run one UAT suite by tox env name, after the pre-flight check
uats-suite suite model_name=MODEL_DEFAULT:
    #!/usr/bin/bash
    set -euxo pipefail

    # Captured into a variable before splitting: a failed command substitution
    # inside `read <<<` does not trip `set -e`, so the suite would otherwise run,
    # and pass, against empty application names.
    names=$(just app-names)
    read -r nifi_app traefik_app <<< "${names}"

    just preflight ${model_name} ${nifi_app} "${traefik_app}"

    uv tool run --python 3.12 tox -e ${suite} -- --model=${model_name} \
        --nifi-app=${nifi_app} ${traefik_app:+--traefik-app=${traefik_app}}

# Run the framework's own tests, proving the fixtures attach to a deployment
test-framework model_name=MODEL_DEFAULT: (uats-suite "framework" model_name)

# Run every UAT suite against one deployment
uats model_name=MODEL_DEFAULT:
    just test-framework ${model_name}

# Lint python code
lint:
    uv tool run --python 3.12 tox -e lint

# Apply formatting to python code
format:
    uv tool run --python 3.12 tox -e format

# Apply terraform formatting
fmt: (initialize)
    terraform -chdir=terraform fmt -recursive

# Check terraform formatting and validate the root module, without rewriting files
validate: (initialize)
    terraform -chdir=terraform fmt -check -recursive
    terraform -chdir=terraform validate

# Collect Juju, Kubernetes and Terraform state for debugging a failed run
collect-artifacts model_name=MODEL_DEFAULT out_dir="artifacts":
    #!/usr/bin/bash
    set -uo pipefail

    mkdir -p "${out_dir}"

    juju status --model ${model_name} --relations --storage > "${out_dir}/juju-status.txt" 2>&1
    juju debug-log --model ${model_name} --replay --no-tail > "${out_dir}/juju-debug-log.txt" 2>&1

    # Every pod in the model, so this stays correct as applications are added.
    kubectl describe pods -n ${model_name} > "${out_dir}/kubectl-describe-pods.txt" 2>&1
    kubectl logs -n ${model_name} --all-containers --ignore-errors \
        --prefix --tail=2000 -l 'app.kubernetes.io/name' > "${out_dir}/pod-logs.txt" 2>&1

    sudo k8s inspect --output-dir "${out_dir}" > "${out_dir}/k8s-inspect.txt" 2>&1
    terraform -chdir=terraform state list > "${out_dir}/terraform-state.txt" 2>&1
    df -h > "${out_dir}/disk.txt" 2>&1

    echo "Artifacts written to ${out_dir}/"
