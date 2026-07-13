"""Qualitative review node for Task Takeover workflow."""

import logging
import re
from pathlib import Path
from typing import cast

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.sandbox.runner import ContainerConfig, ContainerRunner
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


def _get_git_diff(git: GitOperations) -> str:
    """Retrieve git diff of the implemented changes."""
    for args in [("diff", "HEAD~1", "HEAD"), ("diff", "HEAD~1"), ("diff",), ("show", "HEAD")]:
        try:
            res = git._run_git(*args, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return cast(str, res.stdout)
        except Exception:
            continue
    return "No changes detected or unable to retrieve git diff."


def _parse_qualitative_review(output: str) -> tuple[str, str]:
    """Parse qualitative review response to extract verdict and constructive feedback.

    Looks for a line matching 'verdict: <value>' (case-insensitive).
    Everything after a 'feedback:' line is treated as the constructive feedback.

    Defaults to 'tests_incomplete' if verdict is absent or unrecognized.
    """
    verdict = "tests_incomplete"
    feedback = ""

    verdict_match = re.search(r"verdict:\s*`?([a-zA-Z_]+)", output, re.IGNORECASE)
    if verdict_match:
        candidate = verdict_match.group(1).strip().lower()
        if candidate in {"adequate", "tests_incomplete"}:
            verdict = candidate
        else:
            logger.warning(
                f"Unrecognized verdict string '{candidate}', defaulting to tests_incomplete"
            )

    feedback_match = re.search(r"feedback:\s*(.*)", output, re.IGNORECASE | re.DOTALL)
    if feedback_match:
        feedback = feedback_match.group(1).strip()

    return verdict, feedback


async def run_qualitative_review(state: WorkflowState) -> WorkflowState:
    """Assess git diff against Jira ticket Acceptance Criteria using a review-only container.

    Args:
        state: Current workflow state.

    Returns:
        Updated workflow state with verdict, feedback, and retry metrics.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name", "")
    current_task = state.get("current_task_key") or ticket_key

    settings = get_settings()
    jira = JiraClient(settings)

    if not workspace_path:
        logger.error(f"No workspace for qualitative review on {ticket_key}")
        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **state,
                    "last_error": "Workspace not set up",
                    "current_node": "qualitative_review",
                }
            ),
        )

    try:
        # Fetch ticket details from Jira
        task_issue = await jira.get_issue(current_task)
        description = task_issue.description or ""
        acceptance_criteria = _extract_acceptance_criteria(description)

        # Initialize GitOperations to retrieve git diff
        workspace_obj = Workspace(
            path=Path(workspace_path),
            repo_name=current_repo or "",
            branch_name=branch_name or "",
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace_obj)
        git_diff = _get_git_diff(git)

        # Prepare the qualitative review prompt
        from forge.prompts import load_prompt

        prompt_content = load_prompt(
            "task-takeover-review",
            acceptance_criteria=acceptance_criteria,
            git_diff=git_diff,
            workspace_path=workspace_path,
        )

        runner = ContainerRunner(settings)
        result = await runner.run(
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
        response = "\n".join(part for part in (result.stdout, result.stderr) if part)
        verdict, feedback = _parse_qualitative_review(response)

        # Update retry metrics
        current_retry_count = state.get("qualitative_review_retry_count", 0)
        new_retry_count = current_retry_count + (0 if verdict == "adequate" else 1)
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
        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **state,
                    "last_error": str(e),
                    "current_node": "qualitative_review",
                }
            ),
        )
    finally:
        await jira.close()
