# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the UAT framework itself.

These prove the fixtures and the NiFi client attach to a deployment and work.
"""

import jubilant
import pytest

from tests.helpers import NifiClient


def test_juju_fixture_targets_requested_model(juju: jubilant.Juju, request: pytest.FixtureRequest):
    """The juju fixture points at the model passed with --model."""
    assert juju.status().model.name == request.config.getoption("--model")


def test_nifi_client_reaches_unit(nifi_client: NifiClient):
    """The direct client reaches NiFi on the unit address."""
    assert nifi_client.is_up(), f"NiFi API not answering at {nifi_client.api_url}"


def test_nifi_client_reaches_nifi_through_ingress(nifi_client_via_ingress: NifiClient):
    """The ingress client reaches NiFi through the URL Traefik reports."""
    assert nifi_client_via_ingress.is_up(), (
        f"NiFi API not answering at {nifi_client_via_ingress.api_url}"
    )


def test_client_round_trips_a_process_group(nifi_client: NifiClient):
    """The client can create a process group, see it, and delete it again."""
    name = "uat-framework-round-trip"
    nifi_client.create_process_group(name)
    try:
        assert name in nifi_client.list_process_group_names()
    finally:
        nifi_client.delete_process_group(name)

    assert name not in nifi_client.list_process_group_names()


def test_process_group_factory_creates_group(nifi_client: NifiClient, process_group):
    """The process_group fixture creates a group; it deletes it on teardown."""
    name = "uat-framework-factory"
    process_group(name)
    assert name in nifi_client.list_process_group_names()
