"""Shared engine for repository-scoped implementation nodes.

Workflow nodes remain responsible for resolving a work item and its artifacts.
This module owns the invariant execution mechanics once that context is known.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from forge.domain import StationRequest
from forge.prompts import load_prompt
from forge.sandbox.runner import ContainerRunner
from forge.workflow.nodes.git_persistence import PushPersistenceError, push_to_fork_with_retry
from forge.workflow.nodes.repository_scope import implementation_repository_scope
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.sandbox_execution import (
    CONTRACT_NAME as SANDBOX_CONTRACT_NAME,
)
from forge.workflow.stations.sandbox_execution import (
    CONTRACT_VERSION as SANDBOX_CONTRACT_VERSION,
)
from forge.workflow.stations.sandbox_execution import (
    SandboxExecutionInput,
    as_container_result,
    run_sandbox_execution_station,
)
from forge.workflow.utils import merge_review_exhaustion
from forge.workspace.git_ops import GitOperations
from forge.workspace.handoff import capture_handoff


@dataclass(frozen=True)
class ExecutionArtifact:
    """A resolved planning artifact supplied to an implementation run."""

    title: str
    content: str


class ExecutionPersistenceError(Exception):
    """A durable push failed after execution state was collected."""

    def __init__(self, state: dict[str, Any], cause: PushPersistenceError) -> None:
        super().__init__(str(cause))
        self.state = state
        self.cause = cause


@dataclass(frozen=True)
class ExecutionRequest:
    """Normalized input shared by task- and artifact-based implementation."""

    ticket_key: str
    work_id: str
    repository: str
    workspace_path: str
    summary: str
    description: str
    node_name: str
    step_name: str
    policy_key: str
    commit_message: str
    description_title: str = "Work Item Description"
    artifacts: Sequence[ExecutionArtifact] = field(default_factory=tuple)
    review_feedback: str | None = None
    skill_name: str = "implement-task"
    critical_instructions: str = ""
    runner_options: Mapping[str, Any] = field(default_factory=dict)


def build_execution_prompt(request: ExecutionRequest) -> str:
    """Render resolved work and supporting artifacts into a stable prompt."""
    review_feedback = ""
    if request.review_feedback:
        review_feedback = load_prompt(
            "implementation-review-feedback",
            review_feedback=request.review_feedback,
        )
    artifacts = "\n\n".join(
        load_prompt(
            "implementation-artifact",
            artifact_title=artifact.title,
            artifact_content=artifact.content,
        )
        for artifact in request.artifacts
        if artifact.content
    )
    return load_prompt(
        "implementation-execution",
        work_id=request.work_id,
        repository_scope=implementation_repository_scope(
            request.repository, request.workspace_path
        ),
        review_feedback=review_feedback,
        artifacts=artifacts,
        description_title=request.description_title,
        description=request.description,
        critical_instructions=request.critical_instructions,
    )


async def run_and_persist_execution(
    state: Mapping[str, Any],
    request: ExecutionRequest,
    *,
    runner: ContainerRunner,
    git: GitOperations,
    prompt: str,
) -> dict[str, Any]:
    """Run one normalized work item, commit changes, and durably push them.

    ``prompt`` is accepted separately so a workflow can inject Jira references
    after rendering without coupling this engine to a specific state type.
    Push failures intentionally propagate for the calling node to apply its
    workflow-specific retry state.
    """
    station_request = StationRequest[SandboxExecutionInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(
            state, f"{SANDBOX_CONTRACT_NAME}:{request.step_name}:{request.work_id}"
        ),
        contract_name=SANDBOX_CONTRACT_NAME,
        contract_version=SANDBOX_CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        input=SandboxExecutionInput(
            workspace_path=request.workspace_path,
            task_summary=request.summary,
            task_description=prompt,
            ticket_key=request.ticket_key,
            task_key=request.work_id,
            repo_name=request.repository,
            step_name=request.step_name,
            policy_key=request.policy_key,
            skill_name=request.skill_name,
            runner_options=dict(request.runner_options),
        ),
    )
    outcome = await run_sandbox_execution_station(station_request, runner=runner)
    assert outcome.output is not None
    result = as_container_result(outcome.output)
    updated = merge_review_exhaustion(dict(state), result, request.work_id, request.step_name)
    updated = capture_handoff(
        request.workspace_path,
        request.repository,
        request.work_id,
        updated,
    )

    committed = False
    if git.has_uncommitted_changes():
        git.stage_all()
        committed = git.commit(request.commit_message)

    previous_commit = updated.get("commit_info") or {}
    execution_state = {
        **updated,
        "task_execution_results": {
            "success": result.success,
            "exit_code": result.exit_code,
            "error_message": result.error_message,
        },
        "task_execution_logs": {
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
        "commit_info": {
            "sha": git.get_current_sha(),
            "message": request.commit_message,
            "committed": bool(previous_commit.get("committed", False) or committed),
        },
        "current_node": request.node_name,
        "last_error": None if result.success else result.error_message,
        "retry_count": 0 if result.success else state.get("retry_count", 0) + 1,
    }
    try:
        await push_to_fork_with_retry(git)
    except PushPersistenceError as exc:
        raise ExecutionPersistenceError(execution_state, exc) from exc
    return execution_state
