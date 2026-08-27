from datetime import UTC, datetime

import pytest

from forge.domain import StationInvocationIdentity, StationRequest, WorkflowIdentity
from forge.workflow.reducers.implementation_input import reduce_implementation_input
from forge.workflow.stations.implementation_input import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    ImplementationInput,
    run_implementation_input_station,
)
from forge.workflow.stations.runner import run_serialized

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def request() -> StationRequest[ImplementationInput]:
    return StationRequest[ImplementationInput](
        workflow=WorkflowIdentity(run_id="run-1", workflow_name="feature", definition_revision=1),
        invocation=StationInvocationIdentity(
            invocation_id="invocation-1", station_name=CONTRACT_NAME
        ),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=1,
        requested_at=NOW,
        input=ImplementationInput(
            repository="acme/api",
            ticket_key=None,
            artifacts=(
                {
                    "id": "plan:1",
                    "kind": "plan",
                    "content": "Implement it",
                    "digest": "sha256:plan",
                    "approved_digest": "sha256:plan",
                    "status": "approved",
                },
            ),
        ),
    )


def test_identical_requests_produce_identical_outcomes() -> None:
    first = run_implementation_input_station(request())
    second = run_implementation_input_station(request())

    assert first == second


def test_local_runner_round_trips_without_control_plane() -> None:
    serialized = run_serialized(CONTRACT_NAME, request().model_dump_json())

    assert '"status":"succeeded"' in serialized
    assert '"instructions":"Implement it"' in serialized


def test_reducer_owns_only_documented_checkpoint_fields() -> None:
    station_request = request()
    outcome = run_implementation_input_station(station_request)

    update = reduce_implementation_input({"unrelated": "preserved"}, station_request, outcome)

    assert set(update) == {
        "artifacts",
        "work_units",
        "current_work_unit_id",
        "work_resolution",
    }
    assert "unrelated" not in update


def test_reducer_rejects_stale_invocation() -> None:
    station_request = request()
    outcome = run_implementation_input_station(station_request).model_copy(
        update={
            "invocation": StationInvocationIdentity(
                invocation_id="other", station_name=CONTRACT_NAME
            )
        }
    )

    with pytest.raises(ValueError, match="does not belong"):
        reduce_implementation_input({}, station_request, outcome)


def test_reducer_rejects_request_for_another_checkpoint_run() -> None:
    station_request = request()
    outcome = run_implementation_input_station(station_request)

    with pytest.raises(ValueError, match="checkpoint workflow run"):
        reduce_implementation_input({"thread_id": "other-run"}, station_request, outcome)
