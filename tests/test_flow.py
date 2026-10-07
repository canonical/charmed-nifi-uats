# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Flow UATs: a flow can be built, run, and moves data."""

import time
from dataclasses import dataclass

import nipyapi
import pytest

from tests.helpers import NifiClient, best_effort, unique_name

# Set by UpdateAttribute, then looked for on the provenance event it records.
MARKER_KEY = "uat.marker"
MARKER_VALUE = "uat-flow-check"

# Only processors from NARs the rock ships: nifi-standard-nar and
# nifi-update-attribute-nar.
SOURCE_TYPE = "GenerateFlowFile"
TRANSFORM_TYPE = "UpdateAttribute"
SINK_TYPE = "LogAttribute"

FLOW_TIMEOUT = 120
POLL_INTERVAL = 2


@dataclass
class Flow:
    """The running flow under test, and the pieces the checks need."""

    pg_id: str
    source_id: str
    transform_id: str
    sink_id: str
    connection_ids: tuple[str, ...]
    bulletins: list


def _processor_counters(client: NifiClient, pg_id: str) -> dict[str, tuple[int, int]]:
    """FlowFiles in and out, per processor id, for one process group."""
    snapshot = client.process_group_status(pg_id)
    return {
        entry.processor_status_snapshot.id: (
            entry.processor_status_snapshot.flow_files_in,
            entry.processor_status_snapshot.flow_files_out,
        )
        for entry in snapshot.processor_status_snapshots or []
    }


def _queued(client: NifiClient, pg_id: str) -> dict[str, int]:
    """FlowFiles waiting in each connection of a process group."""
    snapshot = client.process_group_status(pg_id)
    return {
        entry.connection_status_snapshot.id: entry.connection_status_snapshot.flow_files_queued
        for entry in snapshot.connection_status_snapshots or []
    }


def _drained(client: NifiClient, pg_id: str, connection_ids: tuple[str, ...]) -> bool:
    """Whether every connection of the flow is reported and holds nothing.

    Looked up by id rather than tested with ``any``: a connection absent from
    the status must not read as drained.
    """
    queued = _queued(client, pg_id)
    return all(queued.get(connection_id) == 0 for connection_id in connection_ids)


@pytest.fixture(scope="module")
def flow(nifi_client: NifiClient):
    """Build GenerateFlowFile -> UpdateAttribute -> LogAttribute, run it, then stop it.

    The source is stopped first and the rest left running, so the queues drain
    the way they would in a flow an operator stops by hand. The process group is
    removed afterwards, leaving the canvas as it was found.
    """
    group = nifi_client.create_process_group(unique_name("uat-flow"))

    # Everything after the group exists runs inside the try, so a flow that
    # fails half-built is still taken off the canvas.
    try:
        source = nifi_client.create_processor(
            group,
            SOURCE_TYPE,
            "generate",
            nipyapi.nifi.ProcessorConfigDTO(
                scheduling_period="1 sec",
                properties={"Custom Text": "uat payload", "Batch Size": "1", "File Size": "0B"},
            ),
        )
        transform = nifi_client.create_processor(
            group,
            TRANSFORM_TYPE,
            "mark",
            nipyapi.nifi.ProcessorConfigDTO(properties={MARKER_KEY: MARKER_VALUE}),
            position=(0, 200),
        )
        sink = nifi_client.create_processor(
            group,
            SINK_TYPE,
            "sink",
            nipyapi.nifi.ProcessorConfigDTO(auto_terminated_relationships=["success"]),
            position=(0, 400),
        )
        connection_ids = (
            nifi_client.connect(source, transform, ["success"]).id,
            nifi_client.connect(transform, sink, ["success"]).id,
        )

        nifi_client.schedule_process_group(group.id, True)
        _wait_until(
            lambda: _processor_counters(nifi_client, group.id).get(sink.id, (0, 0))[0] > 0,
            "a FlowFile to reach the sink",
        )
        # Stop only the source, so anything in flight still reaches the sink.
        nifi_client.schedule_processor(source, False)
        _wait_until(lambda: _drained(nifi_client, group.id, connection_ids), "the queues to drain")
        nifi_client.schedule_process_group(group.id, False)

        # Read here, while the run is fresh: NiFi's bulletin repository drops
        # anything older than five minutes, so a check reading the board after
        # the waits above could miss an error raised early in the run.
        bulletins = nifi_client.bulletins(group.id)

        yield Flow(group.id, source.id, transform.id, sink.id, connection_ids, bulletins)
    finally:
        # Each step independently, so one failure neither hides the test's own
        # failure nor stops the group being taken off the canvas.
        best_effort(
            "stopping the process group",
            lambda: nifi_client.schedule_process_group(group.id, False),
        )
        best_effort(
            "purging the queues", lambda: nipyapi.canvas.purge_process_group(group, stop=True)
        )
        best_effort(
            "deleting the process group",
            lambda: nifi_client.delete_process_group(
                nipyapi.canvas.get_process_group(group.id, "id")
            ),
        )


def _wait_until(condition, description: str) -> None:
    """Poll until *condition* holds, or fail the test saying what was awaited."""
    deadline = time.monotonic() + FLOW_TIMEOUT
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"Timed out after {FLOW_TIMEOUT}s waiting for {description}")


def test_flowfiles_move_through_every_processor(nifi_client: NifiClient, flow: Flow):
    """Each processor in the flow sent or received FlowFiles."""
    counters = _processor_counters(nifi_client, flow.pg_id)

    assert counters[flow.source_id][1] > 0, "the source emitted nothing"
    assert counters[flow.transform_id][0] > 0, "nothing reached UpdateAttribute"
    assert counters[flow.transform_id][1] > 0, "UpdateAttribute passed nothing on"
    assert counters[flow.sink_id][0] > 0, "nothing reached LogAttribute"


def test_queues_drained(nifi_client: NifiClient, flow: Flow):
    """No FlowFile is left waiting, so the flow ran to completion."""
    queued = _queued(nifi_client, flow.pg_id)

    for connection_id in flow.connection_ids:
        assert queued.get(connection_id) == 0, (
            f"connection {connection_id} reports {queued.get(connection_id)!r}, queues: {queued}"
        )


def test_attribute_recorded_in_provenance(nifi_client: NifiClient, flow: Flow):
    """The attribute UpdateAttribute set is on its provenance event.

    This is what separates data being transformed from data merely passing
    through: the event records the attribute NiFi added to the FlowFile.
    """
    events = nifi_client.latest_provenance_events(flow.transform_id)
    assert events, "UpdateAttribute recorded no provenance events"

    modified = [e for e in events if e.event_type == "ATTRIBUTES_MODIFIED"]
    assert modified, f"no ATTRIBUTES_MODIFIED event, saw: {sorted({e.event_type for e in events})}"

    event = nifi_client.provenance_event(modified[0].event_id)
    marker = [a for a in event.attributes or [] if a.name == MARKER_KEY]
    assert marker, f"{MARKER_KEY!r} missing from the event's attributes"
    assert marker[0].value == MARKER_VALUE, marker[0].value


def test_no_error_bulletins(flow: Flow):
    """The flow ran without NiFi reporting an error against it.

    Asserted on the snapshot the fixture took when the run finished, not on a
    fresh read, which the five-minute retention could have emptied by now.
    """
    errors = [b for b in flow.bulletins if b.level == "ERROR"]
    assert not errors, f"error bulletins raised: {[(b.source_name, b.message) for b in errors]}"
