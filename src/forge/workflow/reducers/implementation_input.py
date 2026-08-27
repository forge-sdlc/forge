"""Allowlisted checkpoint reducer for implementation-input outcomes."""

from collections.abc import Mapping
from typing import Any

from forge.domain import StationOutcome, StationOutcomeStatus, StationRequest
from forge.workflow.reducers.common import append_station_attempt, validate_station_outcome
from forge.workflow.stations.implementation_input import ImplementationInput, ImplementationOutput


def reduce_implementation_input(
    state: Mapping[str, Any],
    request: StationRequest[ImplementationInput],
    outcome: StationOutcome[ImplementationOutput],
) -> dict[str, Any]:
    validate_station_outcome(state, request, outcome)
    if outcome.status is not StationOutcomeStatus.SUCCEEDED or outcome.output is None:
        raise ValueError(f"Implementation-input station did not succeed: {outcome.status}")
    output = outcome.output
    artifacts_by_id = {item.get("id"): dict(item) for item in state.get("artifacts") or []}
    for artifact in output.context_artifacts:
        artifacts_by_id[artifact.get("id")] = dict(artifact)
    units_by_id = {item.get("id"): dict(item) for item in state.get("work_units") or []}
    work_unit = dict(output.work_unit)
    previous = units_by_id.get(work_unit["id"])
    units_by_id[work_unit["id"]] = (
        previous if previous and previous.get("status") == "completed" else work_unit
    )
    return {
        "station_history": append_station_attempt(state, request, outcome),
        "artifacts": list(artifacts_by_id.values()),
        "work_units": list(units_by_id.values()),
        "current_work_unit_id": work_unit["id"],
        "work_resolution": {
            "strategy": "task_first",
            "selected_work_unit_id": work_unit["id"],
            "selected_artifact_id": work_unit["source_artifact_ids"][0],
        },
    }
