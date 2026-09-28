# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Ingress UATs: an operator outside the cluster can reach NiFi through Traefik."""

import json
import urllib.error
import urllib.request

import pytest

from tests.helpers import NifiClient

HTTP_TIMEOUT = 30

# The Angular root element and the page title of NiFi's UI shell.
UI_MARKERS = ("<nifi>", "<title>NiFi</title>")


def http_get(url: str) -> tuple[int, str]:
    """GET a URL and return its status and body.

    An error response is an answer, not a failure to reach NiFi, so it is
    returned rather than raised.
    """
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 (http, by design)
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


@pytest.fixture(scope="session")
def ingress(nifi_client_via_ingress: NifiClient, ingress_url: str) -> str:
    """The external URL, once NiFi is answering on it.

    Depending on the client fixture makes every test here wait for the route to
    be live, and skip when ingress is not enabled.
    """
    return ingress_url


def test_ui_served_through_ingress(ingress: str):
    """The NiFi UI shell is served through the ingress."""
    status, body = http_get(f"{ingress}/nifi/")

    assert status == 200, f"GET {ingress}/nifi/ returned {status}"
    for marker in UI_MARKERS:
        assert marker in body, f"{marker!r} missing from the UI response"


def test_api_served_through_ingress(ingress: str):
    """A REST call succeeds through the ingress.

    This is the assertion that exercises NiFi's proxy handling: the UI shell
    loads even when the proxy settings are wrong, whereas NiFi rejects a request
    carrying an X-Forwarded-Prefix it has not been configured to accept.
    """
    status, body = http_get(f"{ingress}/nifi-api/flow/status")

    assert status == 200, f"GET {ingress}/nifi-api/flow/status returned {status}: {body[:200]}"
    assert "controllerStatus" in json.loads(body), body[:200]


def test_canvas_is_writable_through_ingress(nifi_client_via_ingress: NifiClient):
    """The API is usable through the ingress, not only readable."""
    name = "uat-ingress-write"
    nifi_client_via_ingress.create_process_group(name)
    try:
        assert name in nifi_client_via_ingress.list_process_group_names()
    finally:
        nifi_client_via_ingress.delete_process_group(name)
