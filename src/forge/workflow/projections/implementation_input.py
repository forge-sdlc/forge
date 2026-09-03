"""Projection for the implementation-input station."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from forge.domain import (
    StationInvocationIdentity,
    StationRequest,
    WorkflowIdentity,
    stable_identity,
)
from forge.workflow.planning_state import planning_artifacts
from forge.workflow.stations.implementation_input import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    ImplementationInput,
    NoPendingImplementationWork,
    WorkItemSnapshot,
)


class IssueSnapshotReader(Protocol):
    async def get_issue(self, issue_key: str) -> Any: ...


def _task_candidates(state: Mapping[str, Any], repository: str) -> list[str]:
    completed = set(state.get("implemented_tasks") or []) | {
        str(unit.get("id"))
        for unit in state.get("work_units") or []
        if unit.get("status") == "completed"
    }
    mapped = state.get("tasks_by_repo") or {}
    candidates: list[str] = []
    current = state.get("current_task_key")
    if isinstance(current, str) and current and current not in completed:
        for other_repo, keys in mapped.items():
            if other_repo != repository and current in (keys or []):
                raise ValueError(f"Current task {current} belongs to repository {other_repo}")
        candidates.append(current)
    for key in mapped.get(repository, []):
        if isinstance(key, str) and key not in completed and key not in candidates:
            candidates.append(key)
    for unit in state.get("work_units") or []:
        if (
            unit.get("kind") == "task"
            and unit.get("repository") == repository
            and unit.get("status") in {"pending", "active"}
        ):
            key = unit.get("jira_key") or unit.get("key") or unit.get("id")
            if isinstance(key, str) and key not in completed and key not in candidates:
                candidates.append(key)
    ticket_type = getattr(state.get("ticket_type"), "value", state.get("ticket_type"))
    ticket_key = state.get("ticket_key")
    if (
        ticket_type in {"Task", "Epic"}
        and isinstance(ticket_key, str)
        and ticket_key not in completed
        and ticket_key not in candidates
    ):
        candidates.append(ticket_key)
    return candidates


async def project_implementation_input(
    state: Mapping[str, Any], reader: IssueSnapshotReader
) -> StationRequest[ImplementationInput]:
    repository = state.get("current_repository") or state.get("current_repo")
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("current_repo is required to resolve implementation input")
    repository = repository.strip()
    candidates = _task_candidates(state, repository)
    stale = [
        unit.get("id")
        for unit in state.get("work_units") or []
        if unit.get("kind") == "task"
        and unit.get("repository") == repository
        and unit.get("status") == "stale"
    ]
    if stale:
        raise ValueError(f"Repository {repository} has Tasks derived from stale planning: {stale}")
    if (state.get("tasks_by_repo") or {}).get(repository) and not candidates:
        raise NoPendingImplementationWork(f"All Jira tasks are complete for {repository}")
    ticket_key = state.get("ticket_key") if isinstance(state.get("ticket_key"), str) else None
    epic_keys = tuple(key for key in state.get("epic_keys") or [] if isinstance(key, str))
    keys = list(dict.fromkeys([*candidates[:1], *epic_keys, *([ticket_key] if ticket_key else [])]))
    items: dict[str, WorkItemSnapshot] = {}
    for key in keys:
        issue = await reader.get_issue(key)
        items[key] = WorkItemSnapshot(
            # The requested key is authoritative; lightweight reader doubles and
            # some provider responses do not repeat it on the returned object.
            key=key,
            summary=issue.summary or "",
            description=issue.description or "",
            labels=tuple(issue.labels or []),
        )
    completed = set(state.get("implemented_tasks") or []) | {
        str(unit.get("id"))
        for unit in state.get("work_units") or []
        if unit.get("status") == "completed"
    }
    run_id = str(state.get("thread_id") or ticket_key or "local")
    workflow_name = str(state.get("workflow_name") or state.get("ticket_type") or "legacy")
    revision = int(state.get("workflow_revision") or 1)
    invocation_id = stable_identity(
        "station-invocation",
        {"run_id": run_id, "station": CONTRACT_NAME, "repository": repository},
    )
    timestamp = (
        datetime.fromisoformat(str(state["updated_at"]))
        if state.get("updated_at")
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    return StationRequest[ImplementationInput](
        workflow=WorkflowIdentity(
            run_id=run_id,
            workflow_name=workflow_name,
            definition_revision=revision,
            definition_digest=state.get("workflow_digest"),
        ),
        invocation=StationInvocationIdentity(
            invocation_id=invocation_id, station_name=CONTRACT_NAME
        ),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=timestamp,
        artifact_references=tuple(
            str(item.get("id")) for item in planning_artifacts(state) if item.get("id")
        ),
        input=ImplementationInput(
            repository=repository,
            ticket_key=ticket_key,
            candidate_task_keys=tuple(candidates),
            configured_repository_tasks=bool((state.get("tasks_by_repo") or {}).get(repository)),
            epic_keys=epic_keys,
            artifacts=tuple(planning_artifacts(state)),
            work_units=tuple(state.get("work_units") or []),
            implemented_work_ids=tuple(sorted(completed)),
            work_items=items,
        ),
    )
