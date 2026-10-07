# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Git registry UATs: relating git-integrator gives NiFi a flow registry client."""

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jubilant
import pytest

from tests.helpers import NifiClient, best_effort

# The name the charm gives the flow registry client it manages.
REGISTRY_CLIENT_NAME = "juju-git-registry"

# The charm maps a GitHub repository to this client type and API URL. The suite
# assumes the configured repository is on github.com, as the deployment's is.
REGISTRY_CLIENT_TYPE = "org.apache.nifi.github.GitHubFlowRegistryClient"
GITHUB_API_URL = "https://api.github.com/"

NIFI_ENDPOINT = "git-registry"
GIT_ENDPOINT = "git"

RECONCILE_TIMEOUT = 300
POLL_INTERVAL = 5


@dataclass
class Observation:
    """What NiFi and Juju report at one point in the relation's life."""

    registry_client: Any | None
    related: bool
    workload_status: str
    agent_status: str


def _registry_client(nifi_client: NifiClient) -> Any | None:
    """The flow registry client NiFi reports under the charm's name, or None."""
    for entity in nifi_client.registry_clients():
        if entity.component and entity.component.name == REGISTRY_CLIENT_NAME:
            return entity.component
    return None


def _is_related(juju: jubilant.Juju, nifi_app: str) -> bool:
    """Whether Juju reports a relation on NiFi's git-registry endpoint."""
    return NIFI_ENDPOINT in (juju.status().apps[nifi_app].relations or {})


def _observe(juju: jubilant.Juju, nifi_client: NifiClient, nifi_app: str) -> Observation:
    """Read what NiFi and Juju currently report."""
    unit = juju.status().apps[nifi_app].units[f"{nifi_app}/0"]
    return Observation(
        registry_client=_registry_client(nifi_client),
        related=_is_related(juju, nifi_app),
        workload_status=unit.workload_status.current,
        agent_status=unit.juju_status.current,
    )


def _wait_for_registry_client(nifi_client: NifiClient, present: bool) -> None:
    """Poll until the charm has created or removed its registry client."""
    deadline = time.monotonic() + RECONCILE_TIMEOUT
    while time.monotonic() < deadline:
        if (_registry_client(nifi_client) is not None) == present:
            return
        time.sleep(POLL_INTERVAL)
    wanted = "created" if present else "removed"
    raise AssertionError(
        f"Timed out after {RECONCILE_TIMEOUT}s waiting for {REGISTRY_CLIENT_NAME!r} to be {wanted}"
    )


def _wait_for_active(juju: jubilant.Juju, *apps: str) -> None:
    """Wait until the given applications are active with idle agents."""
    juju.wait(
        lambda status: (
            jubilant.all_active(status, *apps) and jubilant.all_agents_idle(status, *apps)
        ),
        delay=10,
    )


def _ensure_related(
    juju: jubilant.Juju, nifi_app: str, nifi_endpoint: str, git_endpoint: str
) -> None:
    """Put the relation back when it is missing, leaving the model as found.

    Checked first because re-integrating an existing relation fails, and the
    checks may have left it either way.
    """
    if not _is_related(juju, nifi_app):
        juju.integrate(nifi_endpoint, git_endpoint)


def _owner_and_repo(repository_url: str) -> tuple[str, str]:
    """The owner and repository name in a git URL, as the charm derives them."""
    path = urlsplit(repository_url).path.rstrip("/").removesuffix(".git")
    parts = [part for part in path.split("/") if part]
    return parts[-2], parts[-1]


@dataclass
class RelationCycle:
    """NiFi as observed with the relation, without it, and with it again."""

    related: Observation
    unrelated: Observation
    rerelated: Observation


@pytest.fixture(scope="module")
def relation_cycle(
    juju: jubilant.Juju, nifi_client: NifiClient, nifi_app: str, git_app: str | None
):
    """Remove the git-registry relation, then put it back, observing each state.

    Removing first and adding second means the relation NiFi ends up with is one
    this suite created, and the model is left as the deployment had it.
    """
    if not git_app:
        pytest.skip("git-integrator not deployed: no --git-app given")

    nifi_endpoint = f"{nifi_app}:{NIFI_ENDPOINT}"
    git_endpoint = f"{git_app}:{GIT_ENDPOINT}"

    _wait_for_registry_client(nifi_client, present=True)
    related = _observe(juju, nifi_client, nifi_app)

    try:
        juju.remove_relation(nifi_endpoint, git_endpoint)
        _wait_for_registry_client(nifi_client, present=False)
        _wait_for_active(juju, nifi_app, git_app)
        unrelated = _observe(juju, nifi_client, nifi_app)

        juju.integrate(nifi_endpoint, git_endpoint)
        _wait_for_registry_client(nifi_client, present=True)
        _wait_for_active(juju, nifi_app, git_app)
        rerelated = _observe(juju, nifi_client, nifi_app)

        yield RelationCycle(related, unrelated, rerelated)
    finally:
        best_effort(
            "restoring the git-registry relation",
            lambda: _ensure_related(juju, nifi_app, nifi_endpoint, git_endpoint),
        )


def test_registry_client_created_when_related(relation_cycle: RelationCycle):
    """Relating git-integrator gives NiFi the flow registry client the charm manages."""
    client = relation_cycle.rerelated.registry_client

    assert relation_cycle.rerelated.related, "Juju does not report the relation"
    assert client is not None, f"{REGISTRY_CLIENT_NAME!r} was not created"
    assert client.name == REGISTRY_CLIENT_NAME
    assert client.type == REGISTRY_CLIENT_TYPE, client.type


def test_registry_client_points_at_the_configured_repository(
    relation_cycle: RelationCycle, juju: jubilant.Juju, git_app: str
):
    """The client points at the repository git-integrator was configured with.

    Read back from git-integrator's own configuration rather than hard-coded, so
    this compares what NiFi was told with what it did.
    """
    config = juju.config(git_app)
    owner, repo = _owner_and_repo(str(config["repository_url"]))
    properties = relation_cycle.rerelated.registry_client.properties

    assert properties["Repository Owner"] == owner
    assert properties["Repository Name"] == repo
    assert properties["GitHub API URL"] == GITHUB_API_URL
    assert properties["Default Branch"] == str(config.get("tracking_ref") or "main")


def test_registry_client_removed_when_relation_removed(relation_cycle: RelationCycle):
    """Removing the relation takes the registry client with it."""
    assert not relation_cycle.unrelated.related, "Juju still reports the relation"
    assert relation_cycle.unrelated.registry_client is None, (
        "the registry client outlived the relation"
    )


def test_unit_active_through_the_relation_cycle(relation_cycle: RelationCycle):
    """NiFi stays active and idle with the relation, without it, and with it again."""
    states = {
        "with the relation": relation_cycle.related,
        "without it": relation_cycle.unrelated,
        "with it again": relation_cycle.rerelated,
    }
    for description, observation in states.items():
        assert observation.workload_status == "active", (
            f"{description}: NiFi is {observation.workload_status}"
        )
        assert observation.agent_status == "idle", (
            f"{description}: the agent is {observation.agent_status}"
        )
