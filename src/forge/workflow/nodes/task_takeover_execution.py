"""Task execution node for Task Takeover workflow."""

import logging
from pathlib import Path
from typing import cast

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.sandbox.runner import ContainerRunner
from forge.workflow.nodes.git_persistence import (
    PushPersistenceError,
    build_persistence_error_state,
    push_to_fork_with_retry,
    use_fork_remote,
)
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import merge_review_exhaustion, update_state_timestamp
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
    current_repo = state.get("current_repo", "")
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
                            state,
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

        # Get details from Jira for task implementation context
        task_issue = await jira.get_issue(current_task)
        task_description = task_issue.description or ""
        plan_content = state.get("plan_content") or ""

        # Build task description with requirements injected
        review_feedback = state.get("review_feedback")
        feedback_section = ""
        if review_feedback:
            feedback_section = f"## Previous Qualitative Review Feedback\nPlease address the following feedback from the qualitative review:\n{review_feedback}\n\n"

        task_prompt = (
            f"You are implementing changes for task takeover [{current_task}].\n\n"
            f"## Repository Execution Scope\n"
            f"Current repository: `{current_repo}`\n"
            f"Implement and validate only the approved-plan steps that belong to "
            f"`{current_repo}`. Do not search for, create, or modify files assigned to "
            f"other repositories in the plan. Those repositories are handled in separate "
            f"workspaces. Completion for this run is evaluated only against the current "
            f"repository's scope.\n\n"
            f"{feedback_section}"
            f"## Approved Implementation Plan\n{plan_content}\n\n"
            f"## Task Description\n{task_description}\n\n"
            f"## Critical Instructions\n"
            f"1. Read and understand the existing codebase.\n"
            f"2. Apply code modifications according to the approved plan.\n"
            f"3. You MUST inject at least one new or modified test file inside the workspace to verify the changes.\n"
            f"4. Run compilation and local test suite commands inside the container workspace.\n"
            f"5. Feed any build/test error and failure logs directly back to your reasoning process to enable iterative self-correction.\n"
            f"6. Make sure all compilation and local tests pass successfully before finishing.\n"
        )

        task_prompt = await fetch_and_inject_references(state, jira, task_prompt)

        # Let ContainerRunner derive container limits from application settings.
        runner = ContainerRunner(settings)

        # Run task execution inside the container
        container_started = True
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Execute task takeover changes for {current_task}",
            task_description=task_prompt,
            ticket_key=ticket_key,
            task_key=current_task,
            repo_name=current_repo,
            step_name="task_takeover_execution",
            policy_key="task_takeover_execution",
            skill_name="implement-task",
        )

        # Collect review exhaustion data (if auto-review ran and exhausted)
        state = merge_review_exhaustion(state, result, current_task, "task_takeover_execution")
        state = capture_handoff(workspace_path, current_repo, current_task, state)
        container_started = False

        # Initialize GitOperations on the host to stage and commit
        committed = False
        commit_message = (
            f"[{current_task}] feat: implement task takeover execution changes and tests"
        )

        # Check for uncommitted changes on host and stage/commit
        if git.has_uncommitted_changes():
            git.stage_all()
            committed = git.commit(commit_message)

        # Preserve the cumulative committed state if we've already committed in a previous attempt
        prev_commit_info = state.get("commit_info") or {}
        prev_committed = prev_commit_info.get("committed", False)
        has_ever_committed = prev_committed or committed

        current_sha = git.get_current_sha()
        execution_state = {
            **state,
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
                "sha": current_sha,
                "message": commit_message,
                "committed": has_ever_committed,
            },
            "current_node": "execute_task_changes",
            "last_error": None if result.success else result.error_message,
            "retry_count": 0 if result.success else state.get("retry_count", 0) + 1,
        }

        # Review may be consumed by another worker with a different local
        # filesystem. Persist the exact commit before checkpointing this node.
        try:
            await push_to_fork_with_retry(git, use_fork=use_fork_remote(state))
        except PushPersistenceError as exc:
            pending_state = {
                **execution_state,
                "implementation_push_pending": True,
                "implementation_push_pending_task": current_task,
            }
            return cast(
                TaskTakeoverState,
                update_state_timestamp(
                    build_persistence_error_state(
                        pending_state,
                        exc,
                        retry_node="execute_task_changes",
                    )
                ),
            )

        # Store results, logs, and commit info in state
        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
                    **execution_state,
                    "implementation_push_pending": False,
                    "implementation_push_pending_task": None,
                    "persistence_retry_count": 0,
                }
            ),
        )

    except Exception as e:
        logger.error(f"execute_task_changes failed for {ticket_key}: {e}")
        if container_started:
            state = capture_handoff(workspace_path, current_repo, current_task, state)
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
