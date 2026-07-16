"""Qualitative review node for Task Takeover workflow."""

import logging
from pathlib import Path
from typing import cast

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.sandbox.runner import ContainerConfig, ContainerRunner
from forge.workflow.nodes.review_utils import (
    collect_git_diff,
    next_review_attempt,
    parse_review_verdict,
    run_review_container,
)
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.task_takeover.state import TaskTakeoverState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


def _extract_acceptance_criteria(description: str) -> str:
    """Extract Acceptance Criteria section from description, or fall back to the entire description."""
    if not description:
        return "No description or acceptance criteria provided."
    # Look for "Acceptance Criteria" case-insensitively
    lower_desc = description.lower()
    index = lower_desc.find("acceptance criteria")
    if index != -1:
        # Return everything from the found heading to the end
        return description[index:].strip()
    return description.strip()


def _parse_qualitative_review(output: str) -> tuple[str, str]:
    """Parse qualitative review response to extract verdict and constructive feedback.

    Looks for a line matching 'verdict: <value>' (case-insensitive).
    Everything after a 'feedback:' line is treated as the constructive feedback.

    Defaults to 'tests_incomplete' if verdict is absent or unrecognized.
    """
    return parse_review_verdict(
        output,
        valid_verdicts={"adequate", "tests_incomplete"},
    )


async def run_qualitative_review(state: WorkflowState) -> WorkflowState:
    """Assess git diff against Jira ticket Acceptance Criteria using a review-only container.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state with verdict, feedback, and retry metrics.
    """
    ticket_key = state["ticket_key"]
    current_repo = state.get("current_repo", "")
    current_task = state.get("current_task_key") or ticket_key

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        # A workflow can resume on a different worker from the one that ran
        # implementation.  Never trust the checkpointed local path: recover
        # the branch from the fork when that path is not visible here.
        workspace_path, _ = prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}

        # Fetch ticket details from Jira
        task_issue = await jira.get_issue(current_task)
        description = task_issue.description or ""
        acceptance_criteria = _extract_acceptance_criteria(description)

        # Initialize GitOperations to retrieve git diff
        git = GitOperations(
            Workspace(
                path=Path(workspace_path),
                repo_name=current_repo,
                branch_name=state.get("context", {}).get("branch_name", ""),
                ticket_key=ticket_key,
            )
        )
        git_diff = collect_git_diff(git)

        # Prepare the qualitative review prompt
        from forge.prompts import load_prompt

        prompt_content = load_prompt(
            "task-takeover-review",
            acceptance_criteria=acceptance_criteria,
            git_diff=git_diff,
            workspace_path=workspace_path,
        )

        runner = ContainerRunner(settings)
        _, response = await run_review_container(
            runner,
            workspace_path=Path(workspace_path),
            task_summary=f"Review task takeover changes for {current_task}",
            task_description=prompt_content,
            config=ContainerConfig(),
            ticket_key=ticket_key,
            task_key=f"{current_task}-review",
            repo_name=current_repo,
            previous_task_keys=state.get("implemented_tasks", []),
        )

        # Parse verdict and feedback
        verdict, feedback = _parse_qualitative_review(response)

        # Update retry metrics
        current_retry_count = state.get("qualitative_review_retry_count", 0)
        new_retry_count = next_review_attempt(
            current_retry_count,
            passed=verdict == "adequate",
        )
        failed = verdict != "adequate"

        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **state,
                    "review_verdict": verdict,
                    "review_feedback": feedback,
                    "qualitative_review_retry_count": new_retry_count,
                    "qualitative_review_failed": failed,
                    "current_node": "qualitative_review",
                    "last_error": None,
                }
            ),
        )

    except Exception as e:
        logger.error(f"run_qualitative_review failed for {ticket_key}: {e}")
        new_retry_count = state.get("qualitative_review_retry_count", 0) + 1
        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **state,
                    "review_verdict": None,
                    "review_feedback": f"Review execution failed: {e}",
                    "qualitative_review_retry_count": new_retry_count,
                    "qualitative_review_failed": True,
                    "last_error": str(e),
                    "current_node": "qualitative_review",
                }
            ),
        )
    finally:
        await jira.close()
