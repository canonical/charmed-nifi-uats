# Charmed NiFi UATs

User Acceptance Tests for [Charmed NiFi](https://github.com/canonical/nifi-k8s-operator).

They check a deployed NiFi from a Charmhub channel. We run them before promoting a
channel, and you can run them against your own deployment to see if it is healthy.

- [The suites](#the-suites)
- [Before you start](#before-you-start)
- [Run them](#run-them)
- [Read the results](#read-the-results)
- [When something fails](#when-something-fails)
- [Working on the tests](#working-on-the-tests)

## The suites

| Suite | Checks |
|---|---|
| `framework` | The fixtures can reach the deployment. Start here if something looks wrong — if this fails, everything else will. |
| `deployment` | Applications are active, storage is mounted, the NiFi version is right, and it is the Canonical rock. |
| `persistence` | Flows, queued FlowFiles and their content survive the pod being replaced. |
| `ingress` | The UI and the REST API work through Traefik. |
| `flow` | A flow can be built and run, and data moves through it. |
| `git-registry` | Relating `git-integrator` gives NiFi a flow registry client, and removing the relation removes it. |

## Before you start

You need a Kubernetes cluster with a load balancer, a Juju controller on it, and
these tools: `juju`, `kubectl`, `terraform`, `just`, `astral-uv` and
[`goss`](https://github.com/goss-org/goss).

[`concierge`](https://github.com/canonical/concierge) sets all of that up, and its
`k8s` preset includes the load balancer Traefik needs:

```bash
sudo snap install concierge --classic
sudo -E concierge prepare -p k8s --extra-snaps=astral-uv,just,terraform
```

The tests run on Python 3.12, which `just` pins for you.

## Run them

### Locally

```bash
just deploy      # deploys to the nifi-uats model from 2.10/edge
just uats        # runs every suite
just destroy
```

One suite at a time is faster while you are iterating, and it reuses the
deployment:

```bash
just uats-persistence
```

To use a different model, channel or revision, pass them in:

```bash
just deploy my-model 2.10/beta 12
just uats-flow my-model
just destroy my-model
```

The `git-registry` suite needs `git-integrator`, which is not deployed by default
because no other suite uses it. Add `true` to deploy it:

```bash
just deploy my-model 2.10/edge "" true
just uats-git-registry my-model
```

Without it, `just uats` runs the other suites and skips `git-registry`.

### In CI

The `UATs` workflow runs on every pull request and nightly. To test a specific
channel, go to **Actions → UATs → Run workflow** and pick one — that is how we
check a promotion candidate.

### Against your own deployment

The tests attach to a model, so you can point them at a deployment you already
have. Only `--model` is required:

```bash
uv tool run --python 3.12 tox -e deployment -- --model my-model
```

`--nifi-app` defaults to `nifi`. The `ingress` and `git-registry` suites also need
`--traefik-app` and `--git-app`, and skip without them.

## Read the results

| Result | What it means |
|---|---|
| `PASSED` | The check held. |
| `FAILED` | A real problem. The message says what was expected and what NiFi, Juju or Kubernetes reported instead. |
| `XFAIL` | A known bug, with the reason printed next to it. Not a failure. |
| `SKIPPED` | The check does not apply here. The reason says why. |

A suite only exits non-zero if something `FAILED`, so `5 passed, 1 xfailed` is a
green run.

**One test is expected to fail.** `test_provenance_survived` is marked `XFAIL`
because the charm leaves NiFi on its in-memory provenance repository, so
provenance cannot survive a pod restart. When the charm is fixed, this test will
start passing and the suite will go red to tell you to remove the marker.

**Skips are normal.** `ingress` skips without `--traefik-app`, and `git-registry`
skips without `--git-app`. If a whole suite skips when you did not expect it to,
that application is not deployed — check `terraform output`, and see the
`git-integrator` note above.

## When something fails

A failed run collects diagnostics into `artifacts/`, and CI uploads them as
`<suite>-artifacts`:

| File | What is in it |
|---|---|
| `MANIFEST` | The size of every file, with `EMPTY` marked. Read this first. |
| `juju-status.txt` | Applications, relations and storage |
| `juju-debug-log.txt` | Charm hook output and errors |
| `kubectl-describe-pods.txt` | Pods and their events |
| `pod-logs.txt` | Container logs |
| `k8s-inspect.txt` | Cluster inspection report |
| `terraform-state.txt` | What Terraform thinks it created |
| `disk.txt` | Free disk, for runs that fail on a full runner |

NiFi's own log is not in `pod-logs.txt`. Read it from the container:

```bash
juju ssh -m my-model --container nifi nifi/0 'tail -200 /var/log/nifi/nifi-app.log'
```

You can collect the same files any time with `just collect-artifacts my-model`.

## Working on the tests

```bash
just lint        # the same checks CI runs
just format      # fixes most of what lint reports
just validate    # checks the terraform
```

The scenarios come from WF032, *Charmed NiFi UATs*.
