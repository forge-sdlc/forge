import json

import pytest

from forge.domain import StationOutcomeStatus
from forge.workflow.projections.task_routing import (
    project_repository_aggregation,
    project_task_routing,
)
from forge.workflow.reducers.task_routing import (
    reduce_repository_aggregation,
    reduce_task_routing,
)
from forge.workflow.stations.runner import run_serialized
from forge.workflow.stations.task_routing import (
    TaskRoutingOutput,
    run_repository_aggregation_station,
    run_task_routing_station,
)


def _state(**updates):
    state = {
        "thread_id": "FORGE-1",
        "ticket_key": "FORGE-1",
        "ticket_type": "Feature",
        "workflow_name": "feature",
        "workflow_revision": 2,
        "current_node": "task_router",
        "retry_count": 0,
        "updated_at": "2026-08-27T12:00:00+00:00",
        "tasks_by_repo": {"acme/api": ["FORGE-2"], "acme/web": ["FORGE-3"]},
    }
    return {**state, **updates}


def test_station_has_no_graph_or_provider_state() -> None:
    request = project_task_routing(_state())

    outcome = run_task_routing_station(request)

    assert outcome.status is StationOutcomeStatus.SUCCEEDED
    assert outcome.output == TaskRoutingOutput(
        repositories=("acme/api", "acme/web"),
        first_repository="acme/api",
        task_count=2,
    )
    assert "current_node" not in outcome.output.model_fields


def test_reducer_owns_legacy_topology_mapping() -> None:
    state = _state()
    request = project_task_routing(state)
    outcome = run_task_routing_station(request)

    update = reduce_task_routing(state, request, outcome)

    assert update["current_node"] == "setup_workspace"
    assert update["current_repo"] == "acme/api"
    assert set(update) == {
        "station_history",
        "repos_to_process",
        "current_repo",
        "repos_completed",
        "implemented_tasks",
        "current_node",
        "last_error",
    }


def test_empty_mapping_returns_structured_blocked_outcome() -> None:
    state = _state(tasks_by_repo={})
    request = project_task_routing(state)

    outcome = run_task_routing_station(request)
    update = reduce_task_routing(state, request, outcome)

    assert outcome.status is StationOutcomeStatus.BLOCKED
    assert outcome.failure is not None
    assert outcome.failure.code == "no_tasks"
    assert update["last_error"] == "No tasks available for routing"
    assert update["current_node"] == "route_tasks"
    assert update["station_history"][0]["status"] == "blocked"


def test_stale_outcome_is_rejected() -> None:
    state = _state()
    request = project_task_routing(state)
    outcome = run_task_routing_station(request).model_copy(
        update={"workflow": request.workflow.model_copy(update={"run_id": "OTHER"})}
    )

    with pytest.raises(ValueError, match="does not belong"):
        reduce_task_routing(state, request, outcome)


def test_station_runs_from_serialized_fixture_without_control_plane() -> None:
    request = project_task_routing(_state())

    raw_outcome = run_serialized("task-routing", request.model_dump_json())

    assert json.loads(raw_outcome)["output"]["first_repository"] == "acme/api"


def test_repository_results_are_aggregated_without_complete_state_access() -> None:
    branches = [
        _state(
            pr_urls=["https://github.com/acme/api/pull/1"],
            repos_completed=["acme/api"],
            implemented_tasks=["FORGE-2"],
        ),
        _state(
            pr_urls=["https://github.com/acme/web/pull/2"],
            repos_completed=["acme/web", "acme/api"],
            implemented_tasks=["FORGE-3"],
            last_error="documentation failed",
        ),
    ]
    request = project_repository_aggregation(branches)
    outcome = run_repository_aggregation_station(request)

    update = reduce_repository_aggregation(branches[0], request, outcome)

    assert update["pr_urls"] == [
        "https://github.com/acme/api/pull/1",
        "https://github.com/acme/web/pull/2",
    ]
    assert update["repos_completed"] == ["acme/api", "acme/web"]
    assert update["implemented_tasks"] == ["FORGE-2", "FORGE-3"]
    assert update["last_error"] == "documentation failed"
    assert update["current_node"] == "ci_evaluator"
