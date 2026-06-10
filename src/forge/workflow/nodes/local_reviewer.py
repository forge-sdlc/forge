"""Local code review node — reviews and fixes breaking issues before PR creation."""

import logging
import re
from pathlib import Path

from forge.config import get_settings
from forge.models.workflow import TicketType
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)

MAX_REVIEW_ATTEMPTS = 2
_QUALITATIVE_CAP = 2
_VALID_VERDICTS = {"adequate", "tests_incomplete", "symptom_only"}


def _parse_bug_verdict(output: str) -> tuple[str, str]:
    """Parse verdict and feedback from bug local review output.

    Looks for a line matching 'verdict: <value>' (case-insensitive).
    Everything after a 'feedback:' line is treated as the feedback text.

    Defaults to 'tests_incomplete' (not 'adequate') when verdict is absent or
    unrecognized, so parse failure does not silently skip the quality gate.

    Args:
        output: Combined stdout from the container review run.

    Returns:
        Tuple of (verdict, feedback).
    """
    verdict = "tests_incomplete"
    feedback = ""

    verdict_match = re.search(r"verdict:\s*`?([a-zA-Z_]+)", output, re.IGNORECASE)
    if verdict_match:
        candidate = verdict_match.group(1).strip().lower()
        if candidate in _VALID_VERDICTS:
            verdict = candidate
        else:
            logger.warning(
                f"Unrecognized verdict string '{candidate}', defaulting to tests_incomplete"
            )

    feedback_match = re.search(r"feedback:\s*(.*)", output, re.IGNORECASE | re.DOTALL)
    if feedback_match:
        feedback = feedback_match.group(1).strip()

    return verdict, feedback


def route_local_review(state: WorkflowState) -> str:
    """Route from local_review based on bug verdict and retry count.

    For bug tickets, reads local_review_verdict and qualitative_retry_count
    from state (already set by _run_bug_review) to determine the edge.

    For feature tickets, reads current_node as set by _run_feature_review.

    Args:
        state: Current workflow state after local_review_changes ran.

    Returns:
        Next node name: 'create_pr' or 'implement_bug_fix'.
    """
    return state.get("current_node", "create_pr")


async def local_review_changes(state: WorkflowState) -> WorkflowState:
    """Review implemented changes locally and fix breaking issues before PR creation.

    For bug tickets: runs qualitative review (local-review-bug.md) that checks
    root-cause alignment and test coverage. Parses verdict; routes to
    implement_bug_fix on non-adequate verdicts (up to 2 retries), then create_pr.

    For other tickets: runs mechanical review (local-review prompt) to find and
    fix breaking issues in-place.

    Args:
        state: Current workflow state.

    Returns:
        Updated state routing to create_pr or implement_bug_fix.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")
    ticket_type = state.get("ticket_type")

    if not workspace_path:
        logger.info(f"No workspace for local review on {ticket_key}, skipping")
        return update_state_timestamp({**state, "current_node": "create_pr"})

    if ticket_type == TicketType.BUG:
        return await _run_bug_review(state)
    else:
        return await _run_feature_review(state)


async def _run_bug_review(state: WorkflowState) -> WorkflowState:
    """Run qualitative local review for bug tickets."""
    ticket_key = state["ticket_key"]
    workspace_path = state["workspace_path"]
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name", "")
    qualitative_retry_count = state.get("qualitative_retry_count", 0)

    rca_content = state.get("rca_content") or ""
    fix_approach = state.get("selected_fix_approach") or {}
    plan_content = state.get("plan_content") or ""

    settings = get_settings()

    task_description = load_prompt(
        "local-review-bug",
        rca_content=rca_content,
        fix_approach_title=fix_approach.get("title", ""),
        fix_approach_description=fix_approach.get("description", ""),
        plan_content=plan_content,
    )

    try:
        runner = ContainerRunner(settings)
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary="Qualitative bug review — root cause and test coverage",
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-qualreview",
            repo_name=current_repo,
        )

        git = GitOperations(
            Workspace(
                path=Path(workspace_path),
                repo_name=current_repo,
                branch_name=branch_name,
                ticket_key=ticket_key,
            )
        )

        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] fix: address review feedback")

        output = (result.stdout or "") + (result.stderr or "")
        verdict, feedback = _parse_bug_verdict(output)

        new_retry_count = qualitative_retry_count + (0 if verdict == "adequate" else 1)

        if verdict == "adequate":
            logger.info(f"Bug qualitative review passed for {ticket_key}")
            return update_state_timestamp(
                {
                    **state,
                    "local_review_verdict": verdict,
                    "qualitative_feedback": feedback or None,
                    "qualitative_retry_count": qualitative_retry_count,
                    "current_node": "create_pr",
                    "last_error": None,
                }
            )

        # Non-adequate verdict
        if new_retry_count >= _QUALITATIVE_CAP:
            logger.warning(
                f"Qualitative review cap ({_QUALITATIVE_CAP}) reached for {ticket_key}, "
                f"proceeding with warning"
            )
            return update_state_timestamp(
                {
                    **state,
                    "local_review_verdict": verdict,
                    "qualitative_feedback": feedback or None,
                    "qualitative_retry_count": new_retry_count,
                    "qualitative_review_failed": True,
                    "current_node": "create_pr",
                    "last_error": None,
                }
            )

        logger.info(
            f"Bug qualitative review: verdict={verdict} for {ticket_key}, "
            f"retry {new_retry_count}/{_QUALITATIVE_CAP}"
        )
        linked_task_keys = state.get("linked_task_keys") or state.get("task_keys") or []
        return update_state_timestamp(
            {
                **state,
                "local_review_verdict": verdict,
                "qualitative_feedback": feedback or None,
                "qualitative_retry_count": new_retry_count,
                "current_node": "implement_bug_fix",
                "last_error": None,
                # Reset so implement_task re-runs the container instead of seeing "all done"
                "implemented_tasks": [],
                "current_task_key": linked_task_keys[0] if linked_task_keys else None,
            }
        )

    except Exception as e:
        logger.error(f"Bug qualitative review failed for {ticket_key}: {e}")
        return update_state_timestamp(
            {
                **state,
                "local_review_verdict": None,
                "current_node": "create_pr",
                "last_error": str(e),
            }
        )


async def _run_feature_review(state: WorkflowState) -> WorkflowState:
    """Run mechanical local review for non-bug tickets (existing behavior)."""
    ticket_key = state["ticket_key"]
    workspace_path = state["workspace_path"]
    review_attempts = state.get("local_review_attempts", 0)
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name", "")

    if review_attempts >= MAX_REVIEW_ATTEMPTS:
        logger.warning(
            f"Max local review attempts ({MAX_REVIEW_ATTEMPTS}) reached for "
            f"{ticket_key}, proceeding to PR"
        )
        return update_state_timestamp(
            {
                **state,
                "local_review_attempts": 0,
                "current_node": "create_pr",
            }
        )

    logger.info(
        f"Running local code review for {ticket_key} "
        f"(attempt {review_attempts + 1}/{MAX_REVIEW_ATTEMPTS})"
    )

    settings = get_settings()
    spec_content = state.get("spec_content", "Not available")
    guardrails = state.get("context", {}).get("guardrails", "")

    task_description = load_prompt(
        "local-review",
        workspace_path=workspace_path,
        spec_content=spec_content[:3000] if spec_content else "Not available",
        guardrails=guardrails[:2000] if guardrails else "",
    )

    try:
        runner = ContainerRunner(settings)
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary="Local code review — fix breaking issues",
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-review",
            repo_name=current_repo,
        )

        git = GitOperations(
            Workspace(
                path=Path(workspace_path),
                repo_name=current_repo,
                branch_name=branch_name,
                ticket_key=ticket_key,
            )
        )

        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] fix: address breaking issues found in local review")
            logger.info(f"Committed local review fixes for {ticket_key}")

        output = (result.stdout or "") + (result.stderr or "")
        has_unfixed = _has_unfixed_breaking_issues(output)

        if has_unfixed and review_attempts + 1 < MAX_REVIEW_ATTEMPTS:
            logger.warning(
                f"Breaking issues remain after review attempt {review_attempts + 1}, retrying"
            )
            return update_state_timestamp(
                {
                    **state,
                    "local_review_attempts": review_attempts + 1,
                    "current_node": "local_review",
                }
            )

        if has_unfixed:
            logger.warning(
                f"Could not fix all breaking issues after {MAX_REVIEW_ATTEMPTS} attempts "
                f"for {ticket_key}, proceeding to PR"
            )
        else:
            logger.info(f"Local review passed for {ticket_key}")

        return update_state_timestamp(
            {
                **state,
                "local_review_attempts": 0,
                "current_node": "create_pr",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Local review failed for {ticket_key}: {e}")
        return update_state_timestamp(
            {
                **state,
                "local_review_attempts": 0,
                "current_node": "create_pr",
                "last_error": None,
            }
        )


def _has_unfixed_breaking_issues(output: str) -> bool:
    """Check if the review output indicates unfixed breaking issues remain."""
    lower = output.lower()
    return "unfixed" in lower and "breaking" in lower
