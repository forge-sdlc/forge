"""Local code review node — reviews implemented changes before PR creation."""

import logging
import re
from pathlib import Path

from forge.config import get_settings
from forge.integrations.jira import JiraClient
from forge.models.workflow import TicketType
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)

MAX_REVIEW_ATTEMPTS = 2
_QUALITATIVE_CAP = 2
_VALID_VERDICTS = {"adequate", "tests_incomplete", "symptom_only"}
_VALID_FEATURE_VERDICTS = {"adequate", "tests_incomplete"}


def _validate_pass_number(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning(f"Invalid pass_number type: bool, expected int; value: {value}")
        return None
    if not isinstance(value, int):
        logger.warning(
            f"Invalid pass_number type: {type(value).__name__}, expected int; value: {value}"
        )
        return None
    if value < 1:
        logger.warning(f"Invalid pass_number value: {value}, expected positive integer >= 1")
        return None
    return value


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


def _parse_feature_verdict(output: str) -> tuple[str, str]:
    """Parse verdict and feedback from feature local review output."""
    verdict = "tests_incomplete"
    feedback = ""

    verdict_match = re.search(r"verdict:\s*`?([a-zA-Z_]+)", output, re.IGNORECASE)
    if verdict_match:
        candidate = verdict_match.group(1).strip().lower()
        if candidate in _VALID_FEATURE_VERDICTS:
            verdict = candidate
        else:
            logger.warning(
                f"Unrecognized feature review verdict '{candidate}', "
                "defaulting to tests_incomplete"
            )

    feedback_match = re.search(r"feedback:\s*(.*)", output, re.IGNORECASE | re.DOTALL)
    if feedback_match:
        feedback = feedback_match.group(1).strip()

    return verdict, feedback


def _discard_reviewer_changes(git: GitOperations, ticket_key: str) -> None:
    """Discard any file changes made by a read-only review container."""
    if not git.has_uncommitted_changes():
        return

    logger.warning(
        f"Local review container modified files for {ticket_key}; discarding reviewer changes"
    )
    git.reset_hard()


def route_local_review(state: WorkflowState) -> str:
    """Route from local_review based on bug verdict and retry count.

    For bug tickets, reads local_review_verdict and qualitative_retry_count
    from state (already set by _run_bug_review) to determine the edge.

    For feature tickets, reads current_node as set by _run_feature_review.

    Args:
        state: Current workflow state after local_review_changes ran.

    Returns:
        Next node name recorded by the graph-specific local review router.
    """
    return state.get("current_node", "create_pr")


async def local_review_changes(state: WorkflowState) -> WorkflowState:
    """Review implemented changes locally before PR creation.

    For bug tickets: runs qualitative review (local-review-bug.md) that checks
    root-cause alignment and test coverage. Parses verdict and records retry
    metrics; graph routing decides whether to re-enter implementation.

    For other tickets: runs a read-only local review that evaluates the
    implementation and emits feedback. Graph routing decides whether to
    re-enter implementation for a fix pass.

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

        _discard_reviewer_changes(git, ticket_key)

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
                    "current_node": "local_review",
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
                    "current_node": "local_review",
                    "last_error": None,
                }
            )

        logger.info(
            f"Bug qualitative review: verdict={verdict} for {ticket_key}, "
            f"retry {new_retry_count}/{_QUALITATIVE_CAP}"
        )
        return update_state_timestamp(
            {
                **state,
                "local_review_verdict": verdict,
                "qualitative_feedback": feedback or None,
                "qualitative_retry_count": new_retry_count,
                "current_node": "local_review",
                "last_error": None,
                "current_task_key": None,
            }
        )

    except Exception as e:
        logger.error(f"Bug qualitative review failed for {ticket_key}: {e}")
        return update_state_timestamp(
            {
                **state,
                "local_review_verdict": None,
                "qualitative_feedback": None,
                "current_node": "local_review",
                "last_error": str(e),
            }
        )


async def _run_feature_review(state: WorkflowState) -> WorkflowState:
    """Run read-only local review for non-bug tickets."""
    ticket_key = state["ticket_key"]
    workspace_path = state["workspace_path"]
    review_attempts = state.get("local_review_attempts", 0)
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name", "")
    raw_pass_number = state.get("local_review_pass_number", 1)
    validated_pass = _validate_pass_number(raw_pass_number)

    if validated_pass is not None:
        logger.info(f"Starting local review pass {validated_pass} for {ticket_key}")

    settings = get_settings()
    jira = JiraClient(settings)
    try:
        if validated_pass is None:
            logger.warning(
                f"Pass number tracking unavailable or corrupted for {ticket_key} "
                f"(raw value: {raw_pass_number!r}), using generic status comment"
            )
            await post_status_comment(
                jira,
                ticket_key,
                "🔧 Local review found issues, applying fixes.",
            )
        elif validated_pass == 1:
            await post_status_comment(
                jira,
                ticket_key,
                "🔍 Running local code review on changes before creating PR.",
            )
        else:
            await post_status_comment(
                jira,
                ticket_key,
                f"🔧 Local review found issues, applying fixes (pass {validated_pass}).",
            )
    finally:
        await jira.close()

    if review_attempts >= MAX_REVIEW_ATTEMPTS:
        logger.warning(
            f"Max local review attempts ({MAX_REVIEW_ATTEMPTS}) reached for "
            f"{ticket_key}, proceeding to PR"
        )
        return update_state_timestamp(
            {
                **state,
                "local_review_has_unfixed_issues": False,
                "local_review_max_attempts_reached": True,
                "local_review_attempts": 0,
                "local_review_verdict": state.get("local_review_verdict"),
                "qualitative_feedback": state.get("qualitative_feedback"),
                "current_node": "local_review",
            }
        )

    logger.info(
        f"Running local code review for {ticket_key} "
        f"(attempt {review_attempts + 1}/{MAX_REVIEW_ATTEMPTS})"
    )

    spec_content = state.get("spec_content", "Not available")
    guardrails = state.get("context", {}).get("guardrails", "")

    task_description = load_prompt(
        "local-review-feature",
        workspace_path=workspace_path,
        spec_content=spec_content[:3000] if spec_content else "Not available",
        guardrails=guardrails[:2000] if guardrails else "",
    )

    try:
        runner = ContainerRunner(settings)
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary="Local code review",
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

        _discard_reviewer_changes(git, ticket_key)

        output = (result.stdout or "") + (result.stderr or "")
        verdict, feedback = _parse_feature_verdict(output)
        has_unfixed = verdict != "adequate"

        if has_unfixed:
            logger.warning(
                f"Local review found issues after attempt {review_attempts + 1}"
            )
            next_pass = (validated_pass or 1) + 1
            return update_state_timestamp(
                {
                    **state,
                    "local_review_has_unfixed_issues": True,
                    "local_review_max_attempts_reached": False,
                    "local_review_verdict": verdict,
                    "qualitative_feedback": feedback or None,
                    "local_review_attempts": review_attempts + 1,
                    "local_review_pass_number": next_pass,
                    "current_node": "local_review",
                    "last_error": None,
                }
            )

        logger.info(f"Local review passed for {ticket_key}")

        return update_state_timestamp(
            {
                **state,
                "local_review_has_unfixed_issues": False,
                "local_review_max_attempts_reached": False,
                "local_review_verdict": verdict,
                "qualitative_feedback": feedback or None,
                "local_review_attempts": 0,
                "current_node": "local_review",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Local review failed for {ticket_key}: {e}")
        return update_state_timestamp(
            {
                **state,
                "local_review_has_unfixed_issues": False,
                "local_review_max_attempts_reached": False,
                "local_review_attempts": 0,
                "local_review_verdict": None,
                "qualitative_feedback": None,
                "current_node": "local_review",
                "last_error": None,
            }
        )
