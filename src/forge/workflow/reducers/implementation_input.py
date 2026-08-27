"""Allowlisted checkpoint reducer for implementation-input outcomes."""

from collections.abc import Mapping
from typing import Any

from forge.domain import StationOutcome, StationOutcomeStatus, StationRequest
from forge.workflow.stations.implementation_input import ImplementationInput, ImplementationOutput


def reduce_implementation_input(
    state: Mapping[str, Any],
    request: StationRequest[ImplementationInput],
    outcome: StationOutcome[ImplementationOutput],
) -> dict[str, Any]:
    expected_run = state.get("thread_id")
    if expected_run and expected_run != request.workflow.run_id:
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
        "artifacts": list(artifacts_by_id.values()),
        "work_units": list(units_by_id.values()),
        "current_work_unit_id": work_unit["id"],
        "work_resolution": {
            "strategy": "task_first",
            "selected_work_unit_id": work_unit["id"],
            "selected_artifact_id": work_unit["source_artifact_ids"][0],
        },
    }
