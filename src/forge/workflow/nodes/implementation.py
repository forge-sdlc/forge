"""Implementation node for executing Tasks using container sandbox.

This node runs AI-powered code implementation inside a podman container
for security isolation. The agent has full tool access (read, write, bash)
within the container but cannot access host systems.

Architecture:
- Container runs Deep Agents with FilesystemBackend
- Workspace is mounted at /workspace
- Agent commits changes locally
- Orchestrator (this node) handles git push after container exits
"""

import logging
from pathlib import Path

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import TicketType
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.git_persistence import (
    PushPersistenceError,
    build_persistence_error_state,
    push_to_fork_with_retry,
)
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.utils import merge_review_exhaustion, update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workspace.git_ops import GitOperations

logger = logging.getLogger(__name__)


async def implement_task(state: WorkflowState) -> WorkflowState:
    """Implement a single Task using container sandbox.

    This node:
    1. Gets the current Task to implement
    2. Spawns a container with the workspace mounted
    3. Container runs Deep Agents with full tool access
    4. Container runs local tests and commits changes
    5. Orchestrator (here) handles git push after success

    Args:
        state: Current workflow state.

    Returns:
        Updated state after implementation.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")
    current_task = state.get("current_task_key")
    task_keys = state.get("task_keys", [])
    implementation_node = _implementation_node_name(state)
    recorded_workspace = state.get("workspace_path")
    local_workspace_survived = bool(recorded_workspace and Path(recorded_workspace).exists())

    try:
        git: GitOperations
        workspace_path, git = prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}
    except Exception as exc:
        logger.error("Unable to prepare implementation workspace for %s: %s", ticket_key, exc)
        return {
            **state,
            "last_error": str(exc),
            "current_node": implementation_node,
            "retry_count": state.get("retry_count", 0) + 1,
        }

    same_workspace_survived = (
        local_workspace_survived
        and workspace_path == recorded_workspace
        and git.workspace_recreated is not True
    )
    if state.get("implementation_push_pending") and same_workspace_survived:
        try:
            await push_to_fork_with_retry(git)
        except PushPersistenceError as exc:
            return update_state_timestamp(
                build_persistence_error_state(state, exc, retry_node=implementation_node)
            )

        pending_task = state.get("implementation_push_pending_task")
        implemented = list(state.get("implemented_tasks", []))
        if pending_task and pending_task not in implemented:
            implemented.append(pending_task)
        return update_state_timestamp(
            {
                **state,
                "current_task_key": None,
                "implemented_tasks": implemented,
                "current_node": implementation_node,
                "last_error": None,
                "implementation_push_pending": False,
                "implementation_push_pending_task": None,
                "persistence_retry_count": 0,
            }
        )
    if state.get("implementation_push_pending"):
        logger.warning(
            "Pending implementation push for %s cannot be recovered on this worker; "
            "rerunning implementation",
            ticket_key,
        )
        state = {
            **state,
            "implementation_push_pending": False,
            "implementation_push_pending_task": None,
            "last_error": None,
        }

    # Get next task to implement if not set
    if not current_task and task_keys:
        # Get tasks for current repo
        current_repo = state.get("current_repo", "")
        repo_tasks = state.get("tasks_by_repo", {}).get(current_repo, [])
        # Find first unimplemented task
        implemented = state.get("implemented_tasks", [])
        for task_key in repo_tasks:
            if task_key not in implemented:
                current_task = task_key
                break

    if not current_task:
        logger.info(f"All tasks implemented for {ticket_key}")

        try:
            # Fallback: commit any files the container agent left uncommitted.
            # The container is responsible for committing, but this catches edge
            # cases where it exited before the final commit step.
            if git.has_uncommitted_changes():
                logger.warning(
                    f"Uncommitted changes found after all tasks for {ticket_key} — "
                    "committing as fallback"
                )
                git.stage_all()
                git.commit(f"[{ticket_key}] chore: commit uncommitted changes after implementation")
            await push_to_fork_with_retry(git)
        except PushPersistenceError as exc:
            return update_state_timestamp(
                build_persistence_error_state(state, exc, retry_node=implementation_node)
            )
        except Exception as exc:
            logger.error(
                "Unable to persist completed implementation for %s: %s",
                ticket_key,
                exc,
            )
            return update_state_timestamp(
                {
                    **state,
                    "last_error": str(exc),
                    "current_node": implementation_node,
                    "retry_count": state.get("retry_count", 0) + 1,
                }
            )

        return update_state_timestamp(
            {
                **state,
                "current_node": "local_review",
                "local_review_pass_number": 1,
                "last_error": None,
            }
        )

    logger.info(f"Implementing Task {current_task} for {ticket_key}")

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        # Get Task details from Jira
        task_issue = await jira.get_issue(current_task)
        task_description = task_issue.description or ""
        task_summary = task_issue.summary

        # Post status comment at task implementation start
        await post_status_comment(
            jira,
            current_task,
            f"🔨 Forge started implementing [{current_task}]: {task_summary}",
        )

        # Get guardrails context
        guardrails = state.get("context", {}).get("guardrails", "")

        # Build full task description with context
        full_description = _build_task_description(
            task_summary=task_summary,
            task_description=task_description,
            guardrails=guardrails,
        )

        # Run implementation in container sandbox
        runner = ContainerRunner(settings)

        current_repo = state.get("current_repo", "")
        # Copy list to avoid mutation after passing to runner
        implemented_tasks = list(state.get("implemented_tasks", []))
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=task_summary,
            task_description=full_description,
            ticket_key=ticket_key,
            task_key=current_task,
            repo_name=current_repo,
            step_name="implement_task",
            skill_name="implement-task",
            previous_task_keys=implemented_tasks,
            trace_context=_build_implementation_trace_context(
                state,
                implementation_node=implementation_node,
                current_repo=current_repo,
            ),
            policy_key=implementation_node,
        )

        # Collect review exhaustion data (if auto-review ran and exhausted)
        state = merge_review_exhaustion(state, result, current_task, "implement_task")

        if result.success:
            logger.info(f"Container completed successfully for {current_task}")

            # Persist each task commit before checkpointing. A subsequent task
            # or local review may resume on a worker with a different filesystem.
            try:
                await push_to_fork_with_retry(git)
            except PushPersistenceError as exc:
                pending_state = {
                    **state,
                    "implementation_push_pending": True,
                    "implementation_push_pending_task": current_task,
                }
                return update_state_timestamp(
                    build_persistence_error_state(
                        pending_state,
                        exc,
                        retry_node=implementation_node,
                    )
                )

            # Persist workflow bookkeeping immediately after the durable push.
            implemented = list(state.get("implemented_tasks", []))
            if current_task not in implemented:
                implemented.append(current_task)

            # Post status comment at task implementation completion
            await post_status_comment(
                jira,
                current_task,
                "✅ Implementation complete. Running local code review before PR.",
            )

            # Track implemented tasks
            return update_state_timestamp(
                {
                    **state,
                    "current_task_key": None,
                    "implemented_tasks": implemented,
                    "current_node": implementation_node,
                    "last_error": None,
                    "retry_count": 0,
                }
            )
        else:
            # Container failed - treat all failures the same
            # The container agent is responsible for running tests and only
            # committing when they pass. If we get here, implementation failed.
            error_msg = result.error_message or "Unknown container error"
            logger.error(f"Implementation failed for {current_task}: {error_msg}")
            raise RuntimeError(error_msg)

    except Exception as e:
        logger.error(f"Implementation failed for {current_task}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": implementation_node,
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()


def _implementation_node_name(state: WorkflowState) -> str:
    """Return the implementation node name for the active workflow graph."""
    return "implement_bug_fix" if state.get("ticket_type") == TicketType.BUG else "implement_task"


def _build_implementation_trace_context(
    state: WorkflowState,
    *,
    implementation_node: str,
    current_repo: str,
) -> dict[str, object]:
    """Build trace-only fields for the container's Langfuse labels/metadata."""
    return {
        "ticket_key": state.get("ticket_key"),
        "ticket_type": state.get("ticket_type"),
        "current_node": implementation_node,
        "current_repo": current_repo,
        "repo": current_repo,
        "current_pr_number": state.get("current_pr_number"),
        "pr_number": state.get("current_pr_number"),
        "retry_count": state.get("retry_count"),
    }


def _build_task_description(
    task_summary: str,
    task_description: str,
    guardrails: str,
) -> str:
    """Build the full task description for the container.

    Args:
        task_summary: Task title.
        task_description: Task details.
        guardrails: Project guardrails context.

    Returns:
        Full task description with all context.
    """
    parts = [
        f"# Task: {task_summary}",
        "",
        "## Description",
        task_description,
    ]

    if guardrails:
        parts.extend(
            [
                "",
                "## Project Guidelines",
                guardrails,
            ]
        )

    parts.extend(
        [
            "",
            "## Instructions",
            "1. Read and understand the existing codebase",
            "2. Implement the task following the repository's coding standards",
            "3. Write clean, well-documented code",
            "4. Run tests to verify your changes work",
            "5. Commit your changes with a descriptive message",
        ]
    )

    return "\n".join(parts)
