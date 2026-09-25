# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for interacting with a deployed Charmed NiFi."""

import json
import logging
import time

import jubilant
import nipyapi
from nipyapi.nifi.rest import ApiException

logger = logging.getLogger(__name__)

NIFI_PORT = 8080
DEFAULT_TIMEOUT = 300


class NifiClientError(Exception):
    """Raised when a NiFi API call fails."""


class NifiClient:
    """Thin wrapper around nipyapi, bound to one NiFi base URL.

    nipyapi keeps its endpoint in module-level configuration, so every call
    re-points it at this client's URL. That lets a test hold two clients at once
    -- one on the unit address and one on the ingress URL -- without them
    fighting over the global.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    @property
    def api_url(self) -> str:
        """The /nifi-api endpoint this client talks to."""
        return f"{self.base_url}/nifi-api"

    def _activate(self) -> None:
        """Point nipyapi's global configuration at this client's endpoint."""
        nipyapi.utils.set_endpoint(self.api_url)

    def is_up(self) -> bool:
        """Whether the NiFi API is answering.

        Probes /flow/about rather than the API root: NiFi serves nothing at
        /nifi-api/ (it redirects there and returns 404), which nipyapi treats
        as not ready. /flow/about needs no privileges, and once authentication
        is enabled it returns 401, which nipyapi still counts as up.
        """
        return bool(nipyapi.utils.is_endpoint_up(f"{self.api_url}/flow/about"))

    def wait_until_ready(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        """Block until the NiFi API answers, or raise.

        Raises:
            TimeoutError: if the API is still not answering after *timeout*.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_up():
                self._activate()
                return
            time.sleep(5)
        raise TimeoutError(f"NiFi API at {self.api_url} not ready after {timeout}s")

    def version(self) -> str:
        """The running NiFi version, e.g. "2.10.0"."""
        self._activate()
        try:
            return nipyapi.system.get_nifi_version_info().ni_fi_version
        except ApiException as e:
            raise NifiClientError(f"Failed to read version info: {e}") from e

    def flow_status(self) -> dict:
        """The controller status from /flow/status."""
        self._activate()
        try:
            return nipyapi.nifi.FlowApi().get_controller_status().to_dict()
        except ApiException as e:
            raise NifiClientError(f"Failed to read flow status: {e}") from e

    def system_diagnostics(self) -> dict:
        """System diagnostics, including the version and repository usage."""
        self._activate()
        try:
            return nipyapi.system.get_system_diagnostics().to_dict()
        except ApiException as e:
            raise NifiClientError(f"Failed to read system diagnostics: {e}") from e

    def current_user(self):
        """The identity and permissions NiFi grants to this client's requests."""
        self._activate()
        try:
            return nipyapi.nifi.FlowApi().get_current_user()
        except ApiException as e:
            raise NifiClientError(f"Failed to read the current user: {e}") from e

    def authentication_configuration(self):
        """Whether NiFi offers a login, and where, from /authentication/configuration."""
        self._activate()
        try:
            return (
                nipyapi.nifi.AuthenticationApi()
                .get_authentication_configuration()
                .authentication_configuration
            )
        except ApiException as e:
            raise NifiClientError(f"Failed to read the authentication configuration: {e}") from e

    def create_process_group(self, name: str, position: tuple[float, float] = (0, 0)):
        """Create a process group under the root, and return it."""
        self._activate()
        try:
            return nipyapi.canvas.create_process_group(
                nipyapi.canvas.get_process_group(nipyapi.canvas.get_root_pg_id(), "id"),
                name,
                position,
            )
        except ApiException as e:
            raise NifiClientError(f"Failed to create process group {name!r}: {e}") from e

    def list_process_group_names(self) -> list[str]:
        """Names of every process group below the root."""
        self._activate()
        return [pg.component.name for pg in nipyapi.canvas.list_all_process_groups()]

    def delete_process_group(self, name: str) -> None:
        """Delete a process group by name. A missing group is not an error."""
        self._activate()
        pg = nipyapi.canvas.get_process_group(name)
        if pg is None:
            return
        try:
            nipyapi.canvas.delete_process_group(pg, force=True)
        except ApiException as e:
            raise NifiClientError(f"Failed to delete process group {name!r}: {e}") from e


def unit_address(juju: jubilant.Juju, app: str, unit: int = 0) -> str:
    """The pod address of a unit, as NiFi binds 0.0.0.0."""
    status = juju.status()
    return status.apps[app].units[f"{app}/{unit}"].address


def nifi_base_url(juju: jubilant.Juju, app: str) -> str:
    """The NiFi base URL reachable directly on the unit address."""
    return f"http://{unit_address(juju, app)}:{NIFI_PORT}"


def proxied_url(juju: jubilant.Juju, traefik_app: str, nifi_app: str) -> str:
    """NiFi's external URL, as reported by Traefik.

    This is the same call the ingress how-to tells users to make, so the tests
    exercise the URL an operator is actually given.

    Raises:
        KeyError: if Traefik is not proxying the NiFi application.
    """
    task = juju.run(f"{traefik_app}/0", "show-proxied-endpoints")
    endpoints = json.loads(task.results["proxied-endpoints"])
    return endpoints[nifi_app]["url"].rstrip("/")
