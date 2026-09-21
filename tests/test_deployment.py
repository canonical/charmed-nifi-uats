# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Deployment UATs: the deployment path produces a healthy solution."""

import secrets
import sys

import jubilant
import pytest

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

# The charm's config option, and the field it reads from the Juju secret, share
# this name. The message is the charm's blocked status while the key is unset.
SENSITIVE_PROPS_KEY = "sensitive-props-key"
MSG_SENSITIVE_KEY_MISSING = f"Missing required config '{SENSITIVE_PROPS_KEY}' (Juju user secret)"

MANUAL_DEPLOY_TIMEOUT = 20 * 60


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
    """Each of NiFi's repositories is on attached Juju storage, at the path NiFi uses."""
    unit = f"{nifi_app}/0"
    mounts = {}
    for storage_id, info in juju.status().storage.storage.items():
        attachment = (info.attachments.units if info.attachments else {}).get(unit)
        if attachment and info.status.current == "attached":
            mounts[storage_id.split("/")[0]] = attachment.location

    for name, path in NIFI_STORAGE.items():
        assert mounts.get(name) == path, (
            f"Storage {name!r} should be attached to {unit} at {path}; attached storage: {mounts}"
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


@pytest.mark.deploys
def test_blocked_until_sensitive_props_key_configured(juju: jubilant.Juju, nifi_app: str):
    """Deployed by hand, NiFi stays blocked until the key is set.

    The deployment under test cannot show this, because Terraform grants the key
    in the same apply that deploys NiFi. So this deploys the same charm revision
    into a temporary model without a key, then configures it exactly as the
    deploy does. The deployment under test is not touched.

    It creates a model on the controller; skip it with `-m "not deploys"`.
    """
    status = juju.status()
    charm = status.apps[nifi_app]
    if charm.charm_origin != "charmhub":
        pytest.skip(
            f"{nifi_app} was not deployed from Charmhub, so there is no revision to redeploy"
        )

    with jubilant.temp_model(controller=status.model.controller) as manual:
        manual.wait_timeout = MANUAL_DEPLOY_TIMEOUT
        try:
            manual.deploy(
                charm.charm_name, "nifi", channel=charm.charm_channel, revision=charm.charm_rev
            )

            manual.wait(lambda s: jubilant.all_blocked(s, "nifi"), error=jubilant.any_error)
            message = manual.status().apps["nifi"].app_status.message
            assert message == MSG_SENSITIVE_KEY_MISSING, f"Unexpected blocked message: {message!r}"

            secret = manual.add_secret(
                "nifi-sensitive-key", {SENSITIVE_PROPS_KEY: secrets.token_hex(16)}
            )
            manual.grant_secret("nifi-sensitive-key", "nifi")
            manual.config("nifi", {SENSITIVE_PROPS_KEY: str(secret)})

            manual.wait(lambda s: jubilant.all_active(s, "nifi"), error=jubilant.any_error)
        except Exception:
            # The model is destroyed on exit, taking its logs with it, and
            # collect-artifacts only covers the deployment under test.
            print(manual.debug_log(limit=1000), end="", file=sys.stderr)
            raise
