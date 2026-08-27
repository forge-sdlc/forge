"""Project legacy checkpoint state into the task-routing contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge.domain import StationRequest
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.task_routing import (
    AGGREGATION_CONTRACT_NAME,
    CONTRACT_NAME,
    CONTRACT_VERSION,
    RepositoryAggregationInput,
    RepositoryBranchResult,
    TaskRoutingInput,
)


def project_task_routing(state: Mapping[str, Any]) -> StationRequest[TaskRoutingInput]:
    raw_mapping = state.get("tasks_by_repo") or {}
    tasks = {
        str(repository): tuple(str(key) for key in keys) for repository, keys in raw_mapping.items()
    }
    return StationRequest[TaskRoutingInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(state, CONTRACT_NAME),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        input=TaskRoutingInput(
            ticket_key=str(state.get("ticket_key") or "local"),
            tasks_by_repository=tasks,
        ),
    )


def project_repository_aggregation(
    states: list[Mapping[str, Any]],
) -> StationRequest[RepositoryAggregationInput]:
    if not states:
        raise ValueError("At least one repository branch result is required")
    base = states[0]
    return StationRequest[RepositoryAggregationInput](
        workflow=project_workflow_identity(base),
        invocation=project_invocation_identity(base, AGGREGATION_CONTRACT_NAME),
        contract_name=AGGREGATION_CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(base.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(base),
        input=RepositoryAggregationInput(
            ticket_key=str(base.get("ticket_key") or "local"),
            branches=tuple(
                RepositoryBranchResult(
                    pull_request_urls=tuple(state.get("pr_urls") or []),
                    completed_repositories=tuple(state.get("repos_completed") or []),
                    implemented_tasks=tuple(state.get("implemented_tasks") or []),
                    error=state.get("last_error"),
                )
                for state in states
            ),
        ),
    )
