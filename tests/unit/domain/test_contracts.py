"""Conformance tests for the Phase 1 domain-contract kernel."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from forge.domain import (
    DomainModel,
    Observation,
    ObservationSource,
    ResourceIdentity,
    StationInvocationIdentity,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
    WorkflowIdentity,
    stable_identity,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)


class ExampleInput(DomainModel):
    ticket_key: str


class ExampleOutput(DomainModel):
    summary: str


def workflow_identity() -> WorkflowIdentity:
    return WorkflowIdentity(
        run_id="run-1",
        workflow_name="feature",
        definition_revision=3,
    )


def test_observation_round_trips_through_json() -> None:
    observation = Observation(
        observation_id="github:event-1",
        source=ObservationSource.WEBHOOK,
        source_system="github",
        resource=ResourceIdentity(resource_type="pull_request", external_id="acme/repo#7"),
        resource_revision="abc123",
        observed_at=NOW,
        received_at=NOW,
        facts={"merged": False, "labels": ["ready"]},
    )

    restored = Observation.model_validate_json(observation.model_dump_json())

    assert restored == observation


def test_contracts_reject_unknown_fields_and_statuses() -> None:
    with pytest.raises(ValidationError):
        ResourceIdentity(
            resource_type="issue",
            external_id="TEST-1",
            provider="jira",  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        StationOutcome[ExampleOutput](
            workflow=workflow_identity(),
            invocation=StationInvocationIdentity(
                invocation_id="invocation-1", station_name="example"
            ),
            contract_name="example",
            contract_version="1.0",
            status="maybe",  # type: ignore[arg-type]
            completed_at=NOW,
        )

    with pytest.raises(ValidationError):
        Observation(
            schema_version="2.0",  # type: ignore[arg-type]
            observation_id="future",
            source=ObservationSource.INTERNAL,
            source_system="forge",
            resource=ResourceIdentity(resource_type="issue", external_id="TEST-1"),
            observed_at=NOW,
            received_at=NOW,
        )


def test_station_request_and_outcome_are_typed_and_round_trip() -> None:
    invocation = StationInvocationIdentity(invocation_id="invocation-1", station_name="example")
    request = StationRequest[ExampleInput](
        workflow=workflow_identity(),
        invocation=invocation,
        contract_name="example",
        contract_version="1.0",
        attempt=1,
        requested_at=NOW,
        input=ExampleInput(ticket_key="TEST-1"),
    )
    outcome = StationOutcome[ExampleOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=NOW,
        output=ExampleOutput(summary="done"),
    )

    assert StationRequest[ExampleInput].model_validate_json(request.model_dump_json()) == request
    assert StationOutcome[ExampleOutput].model_validate_json(outcome.model_dump_json()) == outcome


def test_stable_identity_is_order_independent_and_namespaced() -> None:
    first = stable_identity("observation", {"provider": "github", "event": 7})
    second = stable_identity("observation", {"event": 7, "provider": "github"})

    assert first == second
    assert first.startswith("observation:")
