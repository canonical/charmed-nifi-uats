# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for interacting with a deployed Charmed NiFi."""

import json
import logging
import time
import uuid

import jubilant
import nipyapi
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.resources.core_v1 import Pod
from nipyapi.nifi.rest import ApiException

logger = logging.getLogger(__name__)

NIFI_PORT = 8080
DEFAULT_TIMEOUT = 300

# Server-side jobs -- queue listings and provenance queries -- are submitted and
# then polled until they report finished.
REQUEST_TIMEOUT = 60

WORKLOAD_CONTAINER = "nifi"
NIFI_PROPERTIES = "/opt/nifi/conf/nifi.properties"


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

    def _await_request(self, poll, description: str):
        """Poll a submitted NiFi request until it reports finished.

        Raises:
            NifiClientError: if the request has not finished in time.
        """
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while time.monotonic() < deadline:
            result = poll()
            if result.finished:
                return result
            time.sleep(1)
        raise NifiClientError(f"{description} did not finish within {REQUEST_TIMEOUT}s")

    def queued_flowfile_uuids(self, connection_id: str) -> list[str]:
        """UUIDs of the FlowFiles waiting in a connection.

        Listing a queue is a server-side job, so it is submitted, polled and
        then deleted rather than left behind for the next caller to trip over.
        """
        self._activate()
        api = nipyapi.nifi.FlowFileQueuesApi()
        try:
            listing = api.create_flow_file_listing(connection_id).listing_request
        except ApiException as e:
            raise NifiClientError(f"Failed to list queue {connection_id}: {e}") from e
        try:
            finished = self._await_request(
                lambda: api.get_listing_request(connection_id, listing.id).listing_request,
                f"queue listing for connection {connection_id}",
            )
            return [summary.uuid for summary in finished.flow_file_summaries or []]
        finally:
            # Best-effort: a failed delete must not turn a listing that worked
            # into an error, nor take over from one that genuinely failed.
            best_effort(
                f"deleting listing request {listing.id}",
                lambda: api.delete_listing_request(connection_id, listing.id),
            )

    def flowfile_content(self, connection_id: str, uuid: str) -> str:
        """The content of one FlowFile waiting in a connection."""
        self._activate()
        try:
            return nipyapi.nifi.FlowFileQueuesApi().download_flow_file_content(connection_id, uuid)
        except ApiException as e:
            raise NifiClientError(f"Failed to read the content of FlowFile {uuid}: {e}") from e

    def provenance_event_ids(self, component_id: str) -> list[int]:
        """Ids of the provenance events a component recorded, from the repository.

        A query rather than the latest-events endpoint: that one reads an
        in-memory buffer holding only a component's newest events, which a
        restarted instance starts empty, so it cannot show what persisted.
        """
        self._activate()
        api = nipyapi.nifi.ProvenanceApi()
        request = nipyapi.nifi.ProvenanceEntity(
            provenance=nipyapi.nifi.ProvenanceDTO(
                request=nipyapi.nifi.ProvenanceRequestDTO(
                    search_terms={
                        "ProcessorID": nipyapi.nifi.ProvenanceSearchValueDTO(value=component_id)
                    },
                    max_results=100,
                    summarize=True,
                )
            )
        )
        try:
            submitted = api.submit_provenance_request(request).provenance
        except ApiException as e:
            raise NifiClientError(f"Failed to query provenance for {component_id}: {e}") from e
        try:
            query = self._await_request(
                lambda: api.get_provenance(submitted.id).provenance,
                f"provenance query for {component_id}",
            )
            return sorted(e.event_id for e in query.results.provenance_events or [])
        finally:
            best_effort(
                f"deleting provenance query {submitted.id}",
                lambda: api.delete_provenance(submitted.id),
            )

    def process_group_component_ids(self, pg_id: str) -> set[str]:
        """Ids of the processors and connections NiFi reports inside a process group.

        Read back after a restart, this is the flow definition NiFi loaded from
        its configuration file rather than the one the test built.
        """
        self._activate()
        api = nipyapi.nifi.ProcessGroupsApi()
        try:
            processors = api.get_processors(pg_id).processors or []
            connections = api.get_connections(pg_id).connections or []
        except ApiException as e:
            raise NifiClientError(f"Failed to read process group {pg_id}: {e}") from e
        return {entity.id for entity in processors} | {entity.id for entity in connections}

    def process_group_exists(self, pg_id: str) -> bool:
        """Whether NiFi still resolves a process group by id."""
        self._activate()
        return nipyapi.canvas.get_process_group(pg_id, "id") is not None

    def registry_clients(self) -> list:
        """The flow registry clients NiFi has configured."""
        self._activate()
        try:
            result = nipyapi.nifi.ControllerApi().get_flow_registry_clients()
        except ApiException as e:
            raise NifiClientError(f"Failed to list flow registry clients: {e}") from e
        return (result.registries or []) if result else []

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


def pod_uid(kube: Client, pod: str) -> str:
    """The UID of a pod, which is a new value once the pod has been replaced."""
    return kube.get(Pod, name=pod).metadata.uid


def replace_pod(kube: Client, pod: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Delete a pod and wait for a ready replacement, returning its new UID.

    The pod is deleted rather than the workload restarted: only a replaced pod
    re-attaches the volumes, which is what the persistence checks are about.
    Deleted on the cluster's own terms, with no grace period of its own, so this
    is the replacement an upgrade or a node drain would perform.

    Raises:
        TimeoutError: if no ready replacement appears within *timeout*.
    """
    previous = pod_uid(kube, pod)
    kube.delete(Pod, name=pod)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = kube.get(Pod, name=pod)
        except ApiError:
            # Between the delete landing and the replacement being created.
            time.sleep(5)
            continue
        if current.metadata.uid != previous and _pod_ready(current):
            return current.metadata.uid
        time.sleep(5)
    raise TimeoutError(f"Pod {pod} was not replaced by a ready pod within {timeout}s")


def _pod_ready(pod: Pod) -> bool:
    """Whether a pod reports the Ready condition."""
    conditions = (pod.status.conditions or []) if pod.status else []
    return any(c.type == "Ready" and c.status == "True" for c in conditions)


def sensitive_props_key_digest(juju: jubilant.Juju, unit: str) -> str:
    """A digest of nifi.sensitive.props.key, for comparing it without reading it.

    Hashed inside the container, because the key decrypts the flow and must not
    reach a test report. The first grep requires a character after the "=", so a
    missing or blank key fails here instead of hashing nothing and comparing
    equal on both sides of a restart.
    """
    command = (
        f"grep -q '^nifi.sensitive.props.key=.' {NIFI_PROPERTIES} && "
        f"grep '^nifi.sensitive.props.key=' {NIFI_PROPERTIES} | sha256sum | cut -d' ' -f1"
    )
    return juju.ssh(unit, command, container=WORKLOAD_CONTAINER).strip()


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
