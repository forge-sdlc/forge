"""Pure station for selecting repository-scoped implementation work."""

from __future__ import annotations

import hashlib

from pydantic import Field

from forge.domain import (
    DomainModel,
    JsonValue,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
)

CONTRACT_NAME = "implementation-input"
CONTRACT_VERSION = "1.0"


class NoPendingImplementationWork(Exception):
    """Known implementation work has already completed."""


class WorkItemSnapshot(DomainModel):
    key: str
    summary: str = ""
    description: str = ""
    labels: tuple[str, ...] = ()


class ImplementationInput(DomainModel):
    repository: str
    ticket_key: str | None = None
    candidate_task_keys: tuple[str, ...] = ()
    configured_repository_tasks: bool = False
    epic_keys: tuple[str, ...] = ()
    artifacts: tuple[dict[str, JsonValue], ...] = ()
    work_units: tuple[dict[str, JsonValue], ...] = ()
    implemented_work_ids: tuple[str, ...] = ()
    work_items: dict[str, WorkItemSnapshot] = Field(default_factory=dict)


class ImplementationOutput(DomainModel):
    work_unit: dict[str, JsonValue]
    context_artifacts: tuple[dict[str, JsonValue], ...]
    instructions: str
    summary: str | None = None


def _current(artifact: dict[str, JsonValue]) -> bool:
    status = artifact.get("status")
    return status == "approved" and (
        bool(artifact.get("digest")) and artifact.get("approved_digest") == artifact.get("digest")
    )


def _issue_artifact(kind: str, item: WorkItemSnapshot, repository: str) -> dict[str, JsonValue]:
    content = item.description.strip()
    if kind == "task" and not content:
        content = item.summary.strip()
    return {
        "id": f"jira:{item.key}:{kind}",
        "kind": kind,
        "source": item.key,
        "content": content,
        "digest": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
        "repository": repository,
    }


def run_implementation_input_station(
    request: StationRequest[ImplementationInput],
) -> StationOutcome[ImplementationOutput]:
    """Select work using only the request payload, with no provider or graph access."""
    data = request.input
    repository = data.repository
    stale = [
        unit.get("id")
        for unit in data.work_units
        if unit.get("kind") == "task"
        and unit.get("repository") == repository
        and unit.get("status") == "stale"
    ]
    if stale:
        raise ValueError(f"Repository {repository} has Tasks derived from stale planning: {stale}")
    if data.configured_repository_tasks and not data.candidate_task_keys:
        raise NoPendingImplementationWork(f"All Jira tasks are complete for {repository}")

    artifacts: list[dict[str, JsonValue]] = []
    summaries: dict[str, str] = {}
    if data.candidate_task_keys:
        item = data.work_items[data.candidate_task_keys[0]]
        artifact = _issue_artifact("task", item, repository)
        if artifact["content"]:
            artifacts.append(artifact)
            summaries[str(artifact["id"])] = item.summary

    for key in data.epic_keys:
        item = data.work_items[key]
        repos = {label.removeprefix("repo:") for label in item.labels if label.startswith("repo:")}
        if repository not in repos:
            continue
        artifact = _issue_artifact("epic_plan", item, repository)
        if artifact["content"]:
            artifacts.append(artifact)
            summaries[str(artifact["id"])] = item.summary

    existing_ids = {item.get("id") for item in artifacts}
    rank = {"task": 0, "epic_plan": 1, "plan": 2, "spec": 3, "rca": 4, "prd": 5}
    for original in sorted(data.artifacts, key=lambda item: rank.get(str(item.get("kind")), 99)):
        artifact = dict(original)
        if artifact.get("id") in existing_ids or not _current(artifact):
            continue
        if artifact.get("repository") not in {None, repository} or not artifact.get("content"):
            continue
        artifacts.append(artifact)
        existing_ids.add(artifact.get("id"))

    if data.ticket_key and data.ticket_key not in data.candidate_task_keys[:1]:
        item = data.work_items[data.ticket_key]
        repos = {label.removeprefix("repo:") for label in item.labels if label.startswith("repo:")}
        if repos and repository not in repos:
            raise ValueError(
                f"Jira issue {item.key} is scoped to {sorted(repos)}, not current repository {repository}"
            )
        artifact = _issue_artifact("ticket", item, repository)
        if artifact["content"]:
            artifacts.append(artifact)
            summaries[str(artifact["id"])] = item.summary

    if not artifacts:
        raise ValueError(f"No implementation artifact is available for repository {repository}")
    primary = artifacts[0]
    source = primary.get("source")
    jira_key = str(source) if primary.get("kind") in {"task", "epic_plan", "ticket"} else None
    digest = str(primary["digest"])
    work_id = jira_key or f"internal:{repository}:{primary['kind']}:{digest[7:19]}"
    if work_id in data.implemented_work_ids:
        raise NoPendingImplementationWork(f"Work unit {work_id} is already complete")
    work_unit: dict[str, JsonValue] = {
        "id": work_id,
        "kind": primary["kind"],
        "key": jira_key,
        "repository": repository,
        "status": "pending",
        "source_artifact_ids": [primary["id"]],
        "context_artifact_ids": [item["id"] for item in artifacts[1:]],
    }
    return StationOutcome[ImplementationOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=ImplementationOutput(
            work_unit=work_unit,
            context_artifacts=tuple(artifacts),
            instructions=str(primary["content"]),
            summary=summaries.get(str(primary["id"])),
        ),
    )
