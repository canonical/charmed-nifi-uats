# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Deployment UATs: the deployment path produces a healthy solution."""

import jubilant

from tests.helpers import NifiClient

# The NiFi minor version this track branch ships. Patch releases stay on the
# same track, so the check is against the minor version.
NIFI_TRACK = "2.10"

# Juju storage name -> mount path in the workload container
NIFI_STORAGE = {
    "nifi-data": "/var/lib/nifi/data",
    "content-repo": "/var/lib/nifi/content_repository",
    "provenance-repo": "/var/lib/nifi/provenance_repository",
}

WORKLOAD_CONTAINER = "nifi"


ROCK_MANIFEST = "/usr/share/rocks/dpkg.query"

ACTIVE_IDLE_TIMEOUT = 10 * 60


def test_applications_active_and_idle(
    juju: jubilant.Juju, nifi_app: str, traefik_app: str | None, deployment_ready
):
    """The expected applications are deployed and settle to active/idle."""
    expected = {nifi_app} | ({traefik_app} if traefik_app else set())
    missing = expected - set(juju.status().apps)
    assert not missing, f"Applications missing from the model: {missing}"

    # Waited on rather than read once: a single status snapshot can catch an
    # agent mid-hook, and wait() requires several consecutive polls to agree.
    juju.wait(
        lambda status: (
            jubilant.all_active(status, *expected) and jubilant.all_agents_idle(status, *expected)
        ),
        timeout=ACTIVE_IDLE_TIMEOUT,
    )


def test_nifi_storage_attached_and_mounted(juju: jubilant.Juju, nifi_app: str):
    """NiFi's repositories are on attached Juju storage, mounted where NiFi writes."""
    unit = f"{nifi_app}/0"
    attached = {
        storage_id.split("/")[0]
        for storage_id, info in juju.status().storage.storage.items()
        if info.status.current == "attached"
        and unit in (info.attachments.units if info.attachments else {})
    }
    missing = set(NIFI_STORAGE) - attached
    assert not missing, f"Storage not attached to {unit}: {missing} (attached: {attached})"

    # Juju reports no mount location for Kubernetes attachments, so the paths are
    # checked in the container, which is also where they matter.
    mounts = set(juju.ssh(unit, "findmnt -rn -o TARGET", container=WORKLOAD_CONTAINER).split())
    for name, path in NIFI_STORAGE.items():
        assert path in mounts, (
            f"storage {name!r} is attached but {path} is not a mount point in the "
            f"{WORKLOAD_CONTAINER} container"
        )


def test_nifi_version_matches_track(nifi_client: NifiClient):
    """NiFi runs a release from the minor version this track ships."""
    version = nifi_client.version()
    assert version.startswith(f"{NIFI_TRACK}."), (
        f"NiFi reports {version}, expected a {NIFI_TRACK}.x release"
    )


def test_nifi_runs_canonical_rock(juju: jubilant.Juju, nifi_app: str):
    """The workload runs the Canonical NiFi rock.

    The version alone cannot show this: the upstream image reports the same
    version string.
    """
    result = juju.ssh(
        f"{nifi_app}/0",
        f"test -f {ROCK_MANIFEST} && echo present || echo absent",
        container=WORKLOAD_CONTAINER,
    ).strip()
    assert result == "present", f"{ROCK_MANIFEST} not found in the {WORKLOAD_CONTAINER} container"


def test_flow_controller_serving(nifi_client: NifiClient):
    """The flow controller reports its status, so NiFi is serving and not only listening."""
    status = nifi_client.flow_status()["controller_status"]
    assert status is not None, "Flow status returned no controller status"
    assert isinstance(status["active_thread_count"], int), status


def test_anonymous_access_is_full_access(nifi_client: NifiClient):
    """An unauthenticated caller is NiFi's anonymous user, with write access.

    This cycle the charm serves plain HTTP with anonymous authentication and is expected to
    change when user authentication lands.
    """
    user = nifi_client.current_user()
    assert user.anonymous is True, f"Expected the anonymous user, got {user.identity!r}"
    assert user.identity == "anonymous", user.identity
    assert user.controller_permissions.can_read, "Anonymous user cannot read the controller"
    assert user.controller_permissions.can_write, "Anonymous user cannot write the controller"


def test_login_not_supported(nifi_client: NifiClient):
    """NiFi offers no login, so a user is not left looking for credentials that do not exist.

    NiFi only offers a login once a login identity provider is configured, which
    this cycle's charm does not do.
    """
    configuration = nifi_client.authentication_configuration()
    assert configuration.login_supported is False, (
        f"NiFi reports a login at {configuration.login_uri!r}"
    )
