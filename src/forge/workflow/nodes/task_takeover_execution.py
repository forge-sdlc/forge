"""Task execution node for Task Takeover workflow."""

import logging
from pathlib import Path
from typing import cast

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.sandbox.runner import ContainerRunner
from forge.workflow.implementation_input import resolve_implementation_input
from forge.workflow.nodes.execution_engine import (
    ExecutionArtifact,
    ExecutionPersistenceError,
    ExecutionRequest,
    build_execution_prompt,
    run_and_persist_execution,
)
from forge.workflow.nodes.git_persistence import (
    PushPersistenceError,
    build_persistence_error_state,
    push_to_fork_with_retry,
    use_fork_remote,
)
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.references import fetch_and_inject_references
from forge.workspace.handoff import capture_handoff

logger = logging.getLogger(__name__)


async def execute_task_changes(state: TaskTakeoverState) -> TaskTakeoverState:
    """Execute code modifications and run tests in a container sandbox.

    Args:
        state: Current TaskTakeoverState.

    Returns:
        Updated TaskTakeoverState.
    """
    ticket_key = state["ticket_key"]
    current_repo = state.get("current_repo") or ""
    current_task = state.get("current_task_key") or ticket_key
    container_started = False
    recorded_workspace = state.get("workspace_path")
    local_workspace_survived = bool(recorded_workspace and Path(recorded_workspace).exists())

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        # Resume safely when another worker cannot see the checkpointed local
        # workspace.  The implementation branch is persisted to the fork
        # below so a newly cloned workspace contains the reviewed changes.
        workspace_path, git = await prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}

        same_workspace_survived = (
            local_workspace_survived
            and workspace_path == recorded_workspace
            and git.workspace_recreated is not True
        )
        if state.get("implementation_push_pending") and same_workspace_survived:
            try:
                await push_to_fork_with_retry(git, use_fork=use_fork_remote(state))
            except PushPersistenceError as exc:
                return cast(
                    TaskTakeoverState,
                    update_state_timestamp(
                        build_persistence_error_state(
                            dict(state),
                            exc,
                            retry_node="execute_task_changes",
                        )
                    ),
                )
            return cast(
                TaskTakeoverState,
                update_state_timestamp(
                    {
                        **state,
                        "last_error": None,
                        "implementation_push_pending": False,
                        "implementation_push_pending_task": None,
                        "persistence_retry_count": 0,
                        "current_node": "execute_task_changes",
                    }
                ),
            )
        if state.get("implementation_push_pending"):
            logger.warning(
                "Pending task-takeover push for %s cannot be recovered on this worker; "
                "rerunning implementation",
                ticket_key,
            )
            state = {
                **state,
                "implementation_push_pending": False,
                "implementation_push_pending_task": None,
                "last_error": None,
            }

        resolved = await resolve_implementation_input(
            {**state, "current_task_key": current_task},
            jira,
        )
        state = cast(TaskTakeoverState, {**state, **resolved.state_update(state)})
        primary_id = resolved.work_unit["source_artifact_ids"][0]
        artifact_titles = {
            "epic_plan": "Approved Implementation Plan",
            "plan": "Approved Implementation Plan",
            "spec": "Technical Specification",
            "rca": "Root Cause Analysis",
            "prd": "Product Requirements Document",
            "ticket": "Root Ticket Context",
        }
        supporting_artifacts = tuple(
            ExecutionArtifact(
                artifact_titles.get(
                    str(artifact.get("kind", "artifact")),
                    str(artifact.get("kind", "artifact")).replace("_", " ").title(),
                ),
                str(artifact.get("content", "")),
            )
            for artifact in resolved.context_artifacts
            if artifact.get("id") != primary_id and artifact.get("content")
        )

        request = ExecutionRequest(
            ticket_key=ticket_key,
            work_id=current_task,
            repository=current_repo,
            workspace_path=workspace_path,
            summary=f"Execute task takeover changes for {current_task}",
            description=resolved.instructions,
            description_title="Task Description",
            node_name="execute_task_changes",
            step_name="task_takeover_execution",
            policy_key="task_takeover_execution",
            commit_message=(
                f"[{current_task}] feat: implement task takeover execution changes and tests"
            ),
            artifacts=supporting_artifacts,
            review_feedback=state.get("review_feedback"),
            critical_instructions=(
                "Read and understand the existing codebase.",
                "Apply code modifications according to the approved plan.",
                "You MUST inject at least one new or modified test file inside the workspace to verify the changes.",
                "Run compilation and local test suite commands inside the container workspace.",
                "Feed any build/test error and failure logs directly back to your reasoning process to enable iterative self-correction.",
                "Make sure all compilation and local tests pass successfully before finishing.",
            ),
        )
        task_prompt = build_execution_prompt(request)
        task_prompt = await fetch_and_inject_references(state, jira, task_prompt)

        # Let ContainerRunner derive container limits from application settings.
        runner = ContainerRunner(settings)

        # Run task execution inside the container
        container_started = True
        try:
            execution_state = await run_and_persist_execution(
                state,
                request,
                runner=runner,
                git=git,
                prompt=task_prompt,
            )
        except ExecutionPersistenceError as exc:
            container_started = False
            pending_state = {
                **exc.state,
                "implementation_push_pending": True,
                "implementation_push_pending_task": current_task,
            }
            return cast(
                TaskTakeoverState,
                update_state_timestamp(
                    build_persistence_error_state(
                        pending_state,
                        exc.cause,
                        retry_node="execute_task_changes",
                    )
                ),
            )
        container_started = False

        # Store results, logs, and commit info in state
        completed_units = list(execution_state.get("work_units") or [])
        execution_succeeded = bool(
            (execution_state.get("task_execution_results") or {}).get("success")
        )
        if execution_succeeded:
            for unit in completed_units:
                if unit.get("id") == resolved.work_unit["id"]:
                    unit["status"] = "completed"
        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
                    **execution_state,
                    "work_units": completed_units,
                    "current_work_unit_id": (
                        None if execution_succeeded else resolved.work_unit["id"]
                    ),
                    "implementation_push_pending": False,
                    "implementation_push_pending_task": None,
                    "persistence_retry_count": 0,
                }
            ),
        )

    except Exception as e:
        logger.error(f"execute_task_changes failed for {ticket_key}: {e}")
        if container_started:
            state = cast(
                TaskTakeoverState,
                capture_handoff(workspace_path, current_repo, current_task, dict(state)),
            )
        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
                    **state,
                    "last_error": str(e),
                    "current_node": "execute_task_changes",
                    "retry_count": state.get("retry_count", 0) + 1,
                }
            ),
        )
    finally:
        await jira.close()
