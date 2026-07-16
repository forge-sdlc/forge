"""Task execution node for Task Takeover workflow."""

import logging
from pathlib import Path
from typing import cast

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.sandbox.runner import ContainerConfig, ContainerRunner
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import update_state_timestamp

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

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        # Resume safely when another worker cannot see the checkpointed local
        # workspace.  The implementation branch is persisted to the fork
        # below so a newly cloned workspace contains the reviewed changes.
        workspace_path, git = prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}

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

        # Initialize ContainerRunner matching sandbox configuration
        runner = ContainerRunner(settings)
        config = ContainerConfig()

        # Run task execution inside the container
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Execute task takeover changes for {current_task}",
            task_description=task_prompt,
            config=config,
            ticket_key=ticket_key,
            task_key=current_task,
            repo_name=current_repo,
            previous_task_keys=state.get("implemented_tasks", []),
        )

        # Initialize GitOperations on the host to stage and commit
        committed = False
        commit_message = (
            f"[{current_task}] feat: implement task takeover execution changes and tests"
        )

        # Check for uncommitted changes on host and stage/commit
        if git.has_uncommitted_changes():
            git.stage_all()
            committed = git.commit(commit_message)

        current_sha = git.get_current_sha()
        # Review may be consumed by another worker with a different local
        # filesystem.  Persist the exact commit before checkpointing this node.
        git.push_to_fork()

        # Store results, logs, and commit info in state
        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
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
                        "committed": committed,
                    },
                    "current_node": "execute_task_changes",
                    "last_error": None if result.success else result.error_message,
                    "retry_count": 0 if result.success else state.get("retry_count", 0) + 1,
                }
            ),
        )

    except Exception as e:
        logger.error(f"execute_task_changes failed for {ticket_key}: {e}")
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
