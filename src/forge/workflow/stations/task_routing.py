"""Provider- and graph-independent repository task routing station."""

from __future__ import annotations

from pydantic import Field

from forge.domain import (
    DomainModel,
    StationFailure,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
)

CONTRACT_NAME = "task-routing"
CONTRACT_VERSION = "1.0"
AGGREGATION_CONTRACT_NAME = "repository-result-aggregation"


class TaskRoutingInput(DomainModel):
    ticket_key: str
    tasks_by_repository: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class TaskRoutingOutput(DomainModel):
    repositories: tuple[str, ...]
    first_repository: str | None
    task_count: int = Field(ge=0)


class RepositoryBranchResult(DomainModel):
    pull_request_urls: tuple[str, ...] = ()
    completed_repositories: tuple[str, ...] = ()
    implemented_tasks: tuple[str, ...] = ()
    error: str | None = None


class RepositoryAggregationInput(DomainModel):
    ticket_key: str
    branches: tuple[RepositoryBranchResult, ...]


class RepositoryAggregationOutput(DomainModel):
    pull_request_urls: tuple[str, ...]
    completed_repositories: tuple[str, ...]
    implemented_tasks: tuple[str, ...]
    errors: tuple[str, ...]


def run_task_routing_station(
    request: StationRequest[TaskRoutingInput],
) -> StationOutcome[TaskRoutingOutput]:
    repositories = tuple(request.input.tasks_by_repository)
    output = TaskRoutingOutput(
        repositories=repositories,
        first_repository=repositories[0] if repositories else None,
        task_count=sum(len(tasks) for tasks in request.input.tasks_by_repository.values()),
    )
    if not repositories:
        return StationOutcome[TaskRoutingOutput](
            workflow=request.workflow,
            invocation=request.invocation,
            contract_name=request.contract_name,
            contract_version=request.contract_version,
            status=StationOutcomeStatus.BLOCKED,
            completed_at=request.requested_at,
            output=output,
            reason="No tasks available for routing",
            failure=StationFailure(
                code="no_tasks",
                message="No tasks available for routing",
            ),
        )
    return StationOutcome[TaskRoutingOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=output,
    )


def run_repository_aggregation_station(
    request: StationRequest[RepositoryAggregationInput],
) -> StationOutcome[RepositoryAggregationOutput]:
    """Combine isolated branch results without checkpoint or LangGraph access."""
    pull_requests = tuple(
        url for branch in request.input.branches for url in branch.pull_request_urls
    )
    completed = tuple(
        dict.fromkeys(
            repo for branch in request.input.branches for repo in branch.completed_repositories
        )
    )
    implemented = tuple(
        dict.fromkeys(
            task for branch in request.input.branches for task in branch.implemented_tasks
        )
    )
    errors = tuple(branch.error for branch in request.input.branches if branch.error)
    return StationOutcome[RepositoryAggregationOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=RepositoryAggregationOutput(
            pull_request_urls=pull_requests,
            completed_repositories=completed,
            implemented_tasks=implemented,
            errors=errors,
        ),
    )
