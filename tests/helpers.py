# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for interacting with a deployed Charmed NiFi."""

import json
import logging
import time
import uuid

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

    def create_processor(
        self, process_group, processor_type: str, name: str, config=None, position=(0, 0)
    ):
        """Add a processor of *processor_type*, e.g. "GenerateFlowFile", to a process group."""
        self._activate()
        try:
            return nipyapi.canvas.create_processor(
                process_group,
                nipyapi.canvas.get_processor_type(processor_type),
                position,
                name,
                config,
            )
        except ApiException as e:
            raise NifiClientError(f"Failed to create processor {name!r}: {e}") from e

    def connect(self, source, target, relationships: list[str]):
        """Connect two components for the given relationships."""
        self._activate()
        try:
            return nipyapi.canvas.create_connection(source, target, relationships)
        except ApiException as e:
            raise NifiClientError(f"Failed to connect {source.id} to {target.id}: {e}") from e

    def schedule_process_group(self, pg_id: str, scheduled: bool) -> None:
        """Start or stop every component in a process group."""
        self._activate()
        nipyapi.canvas.schedule_process_group(pg_id, scheduled)

    def schedule_processor(self, processor, scheduled: bool) -> None:
        """Start or stop a single processor."""
        self._activate()
        nipyapi.canvas.schedule_processor(processor, scheduled)

    def process_group_status(self, pg_id: str):
        """The aggregate status snapshot for a process group, with per-component counters."""
        self._activate()
        try:
            return (
                nipyapi.nifi.FlowApi()
                .get_process_group_status(pg_id)
                .process_group_status.aggregate_snapshot
            )
        except ApiException as e:
            raise NifiClientError(f"Failed to read status of process group {pg_id}: {e}") from e

    def latest_provenance_events(self, component_id: str) -> list:
        """The most recent provenance events a component recorded.

        Enough for the checks here, and far cheaper than the submit, poll and
        delete cycle a full provenance query needs.
        """
        self._activate()
        try:
            result = nipyapi.nifi.ProvenanceEventsApi().get_latest_provenance_events(component_id)
            return result.latest_provenance_events.provenance_events or []
        except ApiException as e:
            raise NifiClientError(f"Failed to read provenance for {component_id}: {e}") from e

    def provenance_event(self, event_id):
        """One provenance event in full, including the FlowFile's attributes."""
        self._activate()
        try:
            return (
                nipyapi.nifi.ProvenanceEventsApi().get_provenance_event(event_id).provenance_event
            )
        except ApiException as e:
            raise NifiClientError(f"Failed to read provenance event {event_id}: {e}") from e

    def bulletins(self, pg_id: str) -> list:
        """Bulletins raised by a process group and its children."""
        self._activate()
        return nipyapi.canvas.get_bulletin_board(pg_id=pg_id)

    def list_process_group_names(self) -> list[str]:
        """Names of every process group below the root."""
        self._activate()
        return [pg.component.name for pg in nipyapi.canvas.list_all_process_groups()]

    def delete_process_group(self, process_group) -> None:
        """Delete the process group returned by :meth:`create_process_group`.

        Takes the entity rather than a name: NiFi allows duplicate names, so a
        name lookup could delete a group this client never created.
        """
        self._activate()
        try:
            nipyapi.canvas.delete_process_group(process_group, force=True)
        except ApiException as e:
            name = process_group.component.name if process_group.component else "?"
            raise NifiClientError(f"Failed to delete process group {name!r}: {e}") from e


def best_effort(description: str, action) -> None:
    """Run a teardown step, logging rather than raising when it fails.

    A teardown that raises takes over as the reported failure, pushing the one
    the test found into a chained traceback, and stops the steps after it from
    running at all -- which is how a stopped-but-undeleted process group would
    be left on the canvas.
    """
    try:
        action()
    except Exception as e:  # noqa: BLE001 - cleanup must not mask a test failure
        logger.warning("Cleanup step failed, %s: %s", description, e)


def unique_name(prefix: str) -> str:
    """A canvas object name unlikely to collide with another run's leftovers."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


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
