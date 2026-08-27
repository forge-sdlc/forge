"""Shared station outcome ownership validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from forge.domain import DomainModel, StationOutcome, StationRequest

InputT = TypeVar("InputT", bound=DomainModel)
OutputT = TypeVar("OutputT", bound=DomainModel)


def validate_station_outcome(
    state: Mapping[str, Any],
    request: StationRequest[InputT],
    outcome: StationOutcome[OutputT],
) -> None:
    expected_run = state.get("thread_id") or state.get("ticket_key")
    if expected_run and str(expected_run) != request.workflow.run_id:
        raise ValueError("Station request does not belong to the checkpoint workflow run")
    expected_name = state.get("workflow_name")
    if expected_name and expected_name != request.workflow.workflow_name:
        raise ValueError("Station request workflow definition does not match the checkpoint")
    expected_revision = state.get("workflow_revision")
    if expected_revision and expected_revision != request.workflow.definition_revision:
        raise ValueError("Station request workflow revision does not match the checkpoint")
    if outcome.workflow != request.workflow or outcome.invocation != request.invocation:
        raise ValueError("Station outcome does not belong to this workflow invocation")
    if (outcome.contract_name, outcome.contract_version) != (
        request.contract_name,
        request.contract_version,
    ):
        raise ValueError("Station outcome contract does not match its request")


def append_station_attempt(
    state: Mapping[str, Any],
    request: StationRequest[InputT],
    outcome: StationOutcome[OutputT],
) -> list[dict[str, Any]]:
    """Append compact durable evidence without retaining full station payloads."""
    history = list(state.get("station_history") or [])
    record = {
        "station_name": request.invocation.station_name,
        "invocation_id": request.invocation.invocation_id,
        "attempt": request.attempt,
        "status": outcome.status.value,
        "completed_at": outcome.completed_at.isoformat(),
        "reason": outcome.reason,
    }
    for index, existing in enumerate(history):
        if (
            existing.get("invocation_id") == request.invocation.invocation_id
            and existing.get("attempt") == request.attempt
        ):
            history[index] = record
            break
    else:
        history.append(record)
    return history
