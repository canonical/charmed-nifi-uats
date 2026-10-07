# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Persistence UATs: NiFi's state survives the workload pod being replaced."""

import time
from dataclasses import dataclass

import jubilant
import nipyapi
import pytest
from lightkube import Client

from tests.helpers import (
    NifiClient,
    best_effort,
    nifi_base_url,
    pod_uid,
    replace_pod,
    sensitive_props_key_digest,
    unique_name,
)

PAYLOAD = "uat persistence payload"

# Enough queued FlowFiles for a count to mean something, few enough to collect
# in seconds.
QUEUE_TARGET = 3

QUEUE_TIMEOUT = 120
POLL_INTERVAL = 2


@dataclass
class Flow:
    """The queued flow under test."""

    pg_id: str
    source_id: str
    transform_id: str
    connection_id: str


@dataclass
class State:
    """What NiFi reports about the flow, recorded either side of the restart."""

    pod_uid: str
    component_ids: set[str]
    group_exists: bool
    queued_uuids: list[str]
    first_content: str | None
    provenance_event_ids: list[int]
    key_digest: str


def _queued_count(client: NifiClient, flow: Flow) -> int:
    """How many FlowFiles are waiting in the flow's connection."""
    snapshot = client.process_group_status(flow.pg_id)
    for entry in snapshot.connection_status_snapshots or []:
        if entry.connection_status_snapshot.id == flow.connection_id:
            return entry.connection_status_snapshot.flow_files_queued
    return 0


def _record(
    client: NifiClient,
    juju: jubilant.Juju,
    kube: Client,
    unit: str,
    pod: str,
    flow: Flow,
) -> State:
    """Read everything the restart is expected to preserve."""
    uuids = sorted(client.queued_flowfile_uuids(flow.connection_id))
    return State(
        pod_uid=pod_uid(kube, pod),
        component_ids=client.process_group_component_ids(flow.pg_id),
        group_exists=client.process_group_exists(flow.pg_id),
        queued_uuids=uuids,
        # Left unset on an empty queue, so a lost queue fails the content check
        # with a comparison rather than an IndexError here.
        first_content=client.flowfile_content(flow.connection_id, uuids[0]) if uuids else None,
        provenance_event_ids=client.provenance_event_ids(flow.source_id),
        key_digest=sensitive_props_key_digest(juju, unit),
    )


def _wait_until(condition, description: str) -> None:
    """Poll until *condition* holds, or fail the test saying what was awaited."""
    deadline = time.monotonic() + QUEUE_TIMEOUT
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"Timed out after {QUEUE_TIMEOUT}s waiting for {description}")


@pytest.fixture(scope="module")
def restarted(juju: jubilant.Juju, kube: Client, nifi_client: NifiClient, nifi_app: str):
    """Queue FlowFiles, replace the pod, and return the state either side of it.

    One restart serves every check, so each is a comparison of the same two
    readings rather than its own pod replacement.
    """
    group = nifi_client.create_process_group(unique_name("uat-persistence"))

    # Everything after the group exists runs inside the try, so a flow that
    # fails half-built is still taken off the canvas.
    try:
        source = nifi_client.create_processor(
            group,
            "GenerateFlowFile",
            "generate",
            nipyapi.nifi.ProcessorConfigDTO(
                scheduling_period="0.2 sec",
                properties={"Custom Text": PAYLOAD, "Batch Size": "1", "File Size": "0B"},
            ),
        )
        # Left stopped, so FlowFiles collect in the queue instead of being consumed.
        transform = nifi_client.create_processor(
            group,
            "UpdateAttribute",
            "hold",
            nipyapi.nifi.ProcessorConfigDTO(properties={"uat.marker": "queued"}),
            position=(0, 200),
        )
        connection = nifi_client.connect(source, transform, ["success"])
        flow = Flow(group.id, source.id, transform.id, connection.id)

        nifi_client.schedule_processor(source, True)
        _wait_until(
            lambda: _queued_count(nifi_client, flow) >= QUEUE_TARGET,
            f"{QUEUE_TARGET} FlowFiles to queue",
        )
        # Stopped so the queue holds still and the two readings are comparable.
        nifi_client.schedule_processor(source, False)

        # Juju names the pod after the unit it runs, with the slash replaced.
        unit, pod = f"{nifi_app}/0", f"{nifi_app}-0"
        before = _record(nifi_client, juju, kube, unit, pod, flow)

        replace_pod(kube, pod)
        juju.wait(jubilant.all_active, delay=10)

        # A replaced pod comes back on a new address, so the fixture's client can
        # no longer reach NiFi and the reading after the restart needs its own.
        restarted_client = NifiClient(nifi_base_url(juju, nifi_app))
        restarted_client.wait_until_ready()
        after = _record(restarted_client, juju, kube, unit, pod, flow)

        yield before, after
    finally:
        # Rebuilt rather than reused: the address has changed if the restart ran.
        # Each step independently, so one failure neither hides the test's own
        # failure nor stops the group being taken off the canvas.
        cleanup = NifiClient(nifi_base_url(juju, nifi_app))
        best_effort(
            "stopping the process group", lambda: cleanup.schedule_process_group(group.id, False)
        )
        best_effort(
            "purging the queues", lambda: nipyapi.canvas.purge_process_group(group, stop=True)
        )
        best_effort(
            "deleting the process group",
            lambda: cleanup.delete_process_group(nipyapi.canvas.get_process_group(group.id, "id")),
        )


def test_pod_was_replaced(restarted: tuple[State, State]):
    """The pod really was replaced, so the other checks mean something."""
    before, after = restarted
    assert after.pod_uid != before.pod_uid, (
        f"pod UID is still {before.pod_uid}: the pod was not replaced"
    )


def test_flow_definition_survived(restarted: tuple[State, State]):
    """The process group, its processors and its connection are back.

    Read from NiFi after the restart, this is the flow it loaded from
    flow.json.gz on the data volume.
    """
    before, after = restarted
    assert len(before.component_ids) == 3, f"expected 3 components, built {before.component_ids}"
    assert after.group_exists, "the process group is gone"
    assert after.component_ids == before.component_ids


def test_queued_flowfiles_survived(restarted: tuple[State, State]):
    """The same FlowFiles are still queued, so the FlowFile repository persisted."""
    before, after = restarted
    assert len(before.queued_uuids) >= QUEUE_TARGET, before.queued_uuids
    assert after.queued_uuids == before.queued_uuids


def test_flowfile_content_survived(restarted: tuple[State, State]):
    """The content of a queued FlowFile is intact, so the content repository persisted.

    The FlowFile surviving is not enough: its content lives in a separate
    repository on its own volume.
    """
    before, after = restarted
    assert before.first_content == PAYLOAD, before.first_content
    assert after.first_content == before.first_content


def test_provenance_survived(restarted: tuple[State, State]):
    """The provenance events recorded before the restart are still queryable."""
    before, after = restarted
    assert before.provenance_event_ids, "no provenance events were recorded before the restart"
    missing = set(before.provenance_event_ids) - set(after.provenance_event_ids)
    assert not missing, (
        f"provenance events lost: {sorted(missing)}; "
        f"the query returned {after.provenance_event_ids}"
    )


def test_sensitive_props_key_unchanged(restarted: tuple[State, State]):
    """The key NiFi encrypts the flow with is the same, so the flow stays decryptable."""
    before, after = restarted
    assert after.key_digest == before.key_digest, (
        "nifi.sensitive.props.key changed across the restart"
    )
