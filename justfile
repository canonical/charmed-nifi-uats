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

# Write the tfvars file: model UUID, channel, revision, a fresh sensitive properties
# key, and whether to deploy git-integrator.
[private]
write-tfvars model_name channel revision="" git_integrator="false":
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

    # Rejected rather than ignored when it is neither: a typo silently deploying
    # no git-integrator would make the git-registry suite skip instead of fail.
    case "${git_integrator}" in
        true) echo 'git_integrator = { enabled = true }' >> "terraform/${TFVARS}" ;;
        false) ;;
        *) echo "git_integrator must be 'true' or 'false', got '${git_integrator}'" >&2; exit 1 ;;
    esac

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
# Pass git_integrator=true to deploy git-integrator too, which the git-registry
# suite needs and the others do not.
deploy model_name=MODEL_DEFAULT channel="2.10/edge" revision="" git_integrator="false": (add-model model_name) (initialize)
    #!/usr/bin/bash
    set -euxo pipefail

    just write-tfvars ${model_name} ${channel} "${revision}" "${git_integrator}"
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

# Print the application names from the Terraform outputs, one per line, in the
# order NiFi, Traefik, git-integrator. The optional ones are blank lines when
# they are not deployed
[private]
app-names:
    #!/usr/bin/bash
    set -euo pipefail

    outputs=$(terraform -chdir=terraform output -json)
    nifi_app=$(jq -er '.nifi_app_name.value' <<< "${outputs}")
    # A null output is left out of state, so a disabled application has no key.
    traefik_app=$(jq -r '.traefik_app_name.value // empty' <<< "${outputs}")
    git_app=$(jq -r '.git_app_name.value // empty' <<< "${outputs}")

    printf '%s\n%s\n%s\n' "${nifi_app}" "${traefik_app}" "${git_app}"

# Check the deployment is up before a suite runs, so that a failure inside the
# suite is a real failure rather than an under-deployed model.
[private]
preflight model_name nifi_app traefik_app="" git_app="":
    MODEL="${model_name}" NIFI_APP="${nifi_app}" TRAEFIK_APP="${traefik_app}" GIT_APP="${git_app}" \
        goss --gossfile tests/goss/goss.yaml validate --retry-timeout 900s --sleep 15s --color

# Run one UAT suite by tox env name, after the pre-flight check
uats-suite suite model_name=MODEL_DEFAULT:
    #!/usr/bin/bash
    set -euxo pipefail

    # Collect diagnostics if the suite fails, so a local run leaves the same
    # evidence the CI job uploads.
    trap 'rc=$?; if [ ${rc} -ne 0 ]; then just collect-artifacts ${model_name}; fi; exit ${rc}' EXIT

    # Captured into a variable before splitting: a failed command substitution
    # inside `mapfile <<<` does not trip `set -e`, so the suite would otherwise
    # run, and pass, against empty application names.
    names=$(just app-names)
    mapfile -t apps <<< "${names}"
    nifi_app="${apps[0]}"
    # Defaulted, because $() drops the trailing newlines of absent applications.
    traefik_app="${apps[1]:-}"
    git_app="${apps[2]:-}"

    just preflight ${model_name} ${nifi_app} "${traefik_app}" "${git_app}"

    uv tool run --python 3.12 tox -e ${suite} -- --model=${model_name} \
        --nifi-app=${nifi_app} ${traefik_app:+--traefik-app=${traefik_app}} \
        ${git_app:+--git-app=${git_app}}

# Run the framework's own tests, proving the fixtures attach to a deployment
test-framework model_name=MODEL_DEFAULT: (uats-suite "framework" model_name)

# Run the deployment UATs
uats-deployment model_name=MODEL_DEFAULT: (uats-suite "deployment" model_name)

# Run the persistence UATs
uats-persistence model_name=MODEL_DEFAULT: (uats-suite "persistence" model_name)

# Run the ingress UATs
uats-ingress model_name=MODEL_DEFAULT: (uats-suite "ingress" model_name)

# Run the flow UATs
uats-flow model_name=MODEL_DEFAULT: (uats-suite "flow" model_name)

# Run the git registry UATs
uats-git-registry model_name=MODEL_DEFAULT: (uats-suite "git-registry" model_name)

# Run every UAT suite against one deployment
uats model_name=MODEL_DEFAULT:
    just test-framework ${model_name}
    just uats-deployment ${model_name}
    just uats-persistence ${model_name}
    just uats-ingress ${model_name}
    just uats-flow ${model_name}
    just uats-git-registry ${model_name}

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

    # Every command goes through a pipe rather than redirecting straight to a
    # file. juju, kubectl, terraform and k8s are snaps, and a confined snap
    # cannot write to a descriptor it did not open itself: redirected to a file
    # they produce an empty one and report success, which is how a failed run
    # ended up with nothing to debug. Writing through `cat` is unconfined.
    capture() {
        local file="$1"
        shift
        "$@" 2>&1 | cat > "${file}"
    }

    capture "${out_dir}/juju-status.txt" \
        juju status --model "${model_name}" --relations --storage
    capture "${out_dir}/juju-debug-log.txt" \
        juju debug-log --model "${model_name}" --replay --no-tail

    # Every pod in the model, so this stays correct as applications are added.
    capture "${out_dir}/kubectl-describe-pods.txt" kubectl describe pods -n "${model_name}"
    capture "${out_dir}/pod-logs.txt" \
        kubectl logs -n "${model_name}" --all-containers --ignore-errors \
        --prefix --tail=2000 -l 'app.kubernetes.io/name'

    capture "${out_dir}/k8s-inspect.txt" sudo k8s inspect --output-dir "${out_dir}"
    capture "${out_dir}/terraform-state.txt" terraform -chdir=terraform state list
    capture "${out_dir}/disk.txt" df -h

    # Report what each command actually produced.
    # Named without .txt so the glob below does not list the manifest itself.
    manifest="${out_dir}/MANIFEST"
    : > "${manifest}"
    empty=0
    for file in "${out_dir}"/*.txt; do
        bytes=$(wc -c < "${file}")
        if [ "${bytes}" -eq 0 ]; then
            # Counted here rather than in a pipeline: `for ... done | tee` runs the
            # loop in a subshell, which would discard the count.
            empty=$((empty + 1))
            printf 'EMPTY   %s\n' "${file}" | tee -a "${manifest}"
        else
            printf '%7s %s\n' "${bytes}" "${file}" | tee -a "${manifest}"
        fi
    done

    if [ "${empty}" -ne 0 ]; then
        echo "WARNING: ${empty} artifact file(s) are empty; the commands that write them produced no output" >&2
    fi
    echo "Artifacts written to ${out_dir}/"
