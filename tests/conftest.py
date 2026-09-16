# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for the Charmed NiFi UATs.

The deployment itself is owned by the justfile, which bootstraps a model and
applies the Terraform root module. These fixtures attach to that model, so any
suite can be run on its own against an existing deployment.
"""

import logging
import sys
import time

import jubilant
import pytest

from tests.helpers import NifiClient, nifi_base_url, proxied_url

logger = logging.getLogger(__name__)

WAIT_TIMEOUT = 20 * 60


def pytest_addoption(parser):
    parser.addoption(
        "--model",
        action="store",
        required=True,
        help="Juju model holding the Charmed NiFi deployment under test",
    )
    parser.addoption(
        "--nifi-app",
        action="store",
        default="nifi",
        help="Name of the NiFi application (default: nifi)",
    )
    parser.addoption(
        "--traefik-app",
        action="store",
        default=None,
        help="Name of the Traefik application. Omit when ingress is not enabled; "
        "ingress tests are then skipped",
    )


@pytest.fixture(scope="session")
def nifi_app(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--nifi-app")


@pytest.fixture(scope="session")
def traefik_app(request: pytest.FixtureRequest) -> str | None:
    return request.config.getoption("--traefik-app")


@pytest.fixture(scope="session")
def juju(request: pytest.FixtureRequest):
    """A jubilant client for the model under test.

    On failure the model's debug log is printed, so a CI run carries enough
    context to diagnose without re-deploying.
    """
    model = request.config.getoption("--model")
    juju = jubilant.Juju(model=model)
    juju.wait_timeout = WAIT_TIMEOUT

    yield juju

    if request.session.testsfailed:
        time.sleep(0.5)
        print(juju.debug_log(limit=1000), end="", file=sys.stderr)


@pytest.fixture(scope="session")
def deployment_ready(juju: jubilant.Juju):
    """Wait until every application in the model is active and idle.

    Suites depend on this rather than assuming the justfile already waited, so
    a suite run on its own against an existing model behaves the same way.
    """
    juju.wait(jubilant.all_active, delay=10, timeout=WAIT_TIMEOUT)


@pytest.fixture(scope="session")
def nifi_client(juju: jubilant.Juju, nifi_app: str, deployment_ready) -> NifiClient:
    """A NiFi client bound to the unit address, bypassing the ingress."""
    client = NifiClient(nifi_base_url(juju, nifi_app))
    client.wait_until_ready()
    return client


@pytest.fixture(scope="session")
def ingress_url(
    juju: jubilant.Juju, traefik_app: str | None, nifi_app: str, deployment_ready
) -> str:
    """NiFi's external URL, as reported by Traefik's show-proxied-endpoints.

    Ingress is optional in the deployment, so every test depending on this
    fixture is skipped when no Traefik application is given.
    """
    if not traefik_app:
        pytest.skip("Ingress not enabled: no --traefik-app given")
    return proxied_url(juju, traefik_app, nifi_app)


@pytest.fixture(scope="session")
def nifi_client_via_ingress(ingress_url: str) -> NifiClient:
    """A NiFi client that reaches NiFi through the Traefik ingress."""
    client = NifiClient(ingress_url)
    client.wait_until_ready()
    return client


@pytest.fixture
def process_group(nifi_client: NifiClient):
    """Factory creating process groups that are deleted when the test ends.

    Keeps the canvas clean so a suite can be re-run against the same deployment.
    """
    created: list[str] = []

    def _create(name: str, position: tuple[float, float] = (0, 0)):
        pg = nifi_client.create_process_group(name, position)
        created.append(name)
        return pg

    yield _create

    for name in reversed(created):
        try:
            nifi_client.delete_process_group(name)
        except Exception as e:  # noqa: BLE001 - cleanup must not mask a test failure
            logger.warning("Could not delete process group %s: %s", name, e)
