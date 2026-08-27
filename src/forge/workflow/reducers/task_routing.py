"""Allowlisted legacy checkpoint reducer for task routing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge.domain import StationOutcome, StationOutcomeStatus, StationRequest
from forge.workflow.reducers.common import append_station_attempt, validate_station_outcome
from forge.workflow.stations.task_routing import (
    RepositoryAggregationInput,
    RepositoryAggregationOutput,
    TaskRoutingInput,
    TaskRoutingOutput,
)


def reduce_task_routing(
    state: Mapping[str, Any],
    request: StationRequest[TaskRoutingInput],
    outcome: StationOutcome[TaskRoutingOutput],
) -> dict[str, Any]:
    validate_station_outcome(state, request, outcome)
    if outcome.output is None:
        raise ValueError("Task-routing station returned no output")
    if outcome.status is StationOutcomeStatus.BLOCKED:
        return {
            "station_history": append_station_attempt(state, request, outcome),
            "last_error": outcome.reason or "No tasks available for routing",
            "current_node": "route_tasks",
        }
    if outcome.status is not StationOutcomeStatus.SUCCEEDED:
        raise ValueError(f"Task-routing station did not succeed: {outcome.status}")
    return {
        "station_history": append_station_attempt(state, request, outcome),
        "repos_to_process": list(outcome.output.repositories),
        "current_repo": outcome.output.first_repository,
        "repos_completed": [],
        "implemented_tasks": [],
        "current_node": "setup_workspace",
        "last_error": None,
    }


def reduce_repository_aggregation(
    state: Mapping[str, Any],
    request: StationRequest[RepositoryAggregationInput],
    outcome: StationOutcome[RepositoryAggregationOutput],
) -> dict[str, Any]:
    validate_station_outcome(state, request, outcome)
    if outcome.status is not StationOutcomeStatus.SUCCEEDED or outcome.output is None:
        raise ValueError(f"Repository aggregation did not succeed: {outcome.status}")
    return {
        "station_history": append_station_attempt(state, request, outcome),
        "pr_urls": list(outcome.output.pull_request_urls),
        "repos_completed": list(outcome.output.completed_repositories),
        "implemented_tasks": list(outcome.output.implemented_tasks),
        "parallel_branch_id": None,
        "parallel_total_branches": None,
        "last_error": "; ".join(outcome.output.errors) if outcome.output.errors else None,
        "current_node": "ci_evaluator",
    }
