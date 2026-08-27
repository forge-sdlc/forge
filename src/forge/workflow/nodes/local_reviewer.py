"""Local code review node — reviews and fixes breaking issues before PR creation."""

import logging
from pathlib import Path

from forge.config import get_settings
from forge.integrations.jira import JiraClient
from forge.models.workflow import TicketType
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.git_persistence import (
    PushPersistenceError,
    build_persistence_error_state,
    push_to_fork_with_retry,
    use_fork_remote,
)
from forge.workflow.nodes.review_utils import (
    next_review_attempt,
    parse_review_verdict,
    review_attempts_exhausted,
    run_review_container,
)
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.utils import merge_review_exhaustion, update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workspace.git_ops import GitOperations

logger = logging.getLogger(__name__)

MAX_REVIEW_ATTEMPTS = 2
_QUALITATIVE_CAP = 2
_VALID_VERDICTS = {"adequate", "tests_incomplete", "symptom_only"}


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
    return parse_review_verdict(output, valid_verdicts=_VALID_VERDICTS)


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
    ticket_type = state.get("ticket_type")
    recorded_workspace = state.get("workspace_path")
    local_workspace_survived = bool(recorded_workspace and Path(recorded_workspace).exists())

    try:
        workspace_path, git = await prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}
    except Exception as exc:
        logger.error("Unable to prepare local-review workspace for %s: %s", ticket_key, exc)
        return update_state_timestamp(
            {**state, "current_node": "create_pr", "last_error": str(exc)}
        )

    same_workspace_survived = (
        local_workspace_survived
        and workspace_path == recorded_workspace
        and git.workspace_recreated is not True
    )
    if state.get("review_push_pending") and same_workspace_survived:
        try:
            await push_to_fork_with_retry(git, use_fork=use_fork_remote(state))
        except PushPersistenceError as exc:
            return _review_persistence_error_state(state, exc)
        updates = state.get("review_push_pending_updates", {})
        return update_state_timestamp(
            {
                **state,
                **updates,
                "review_push_pending": False,
                "review_push_pending_updates": {},
                "persistence_retry_count": 0,
            }
        )
    if state.get("review_push_pending"):
        logger.warning(
            "Pending review push for %s cannot be recovered on this worker; rerunning review",
            ticket_key,
        )
        state = {
            **state,
            "review_push_pending": False,
            "review_push_pending_updates": {},
            "last_error": None,
        }

    if ticket_type == TicketType.BUG:
        return await _run_bug_review(state, git)
    else:
        return await _run_feature_review(state, git)


async def _run_bug_review(state: WorkflowState, git: GitOperations) -> WorkflowState:
    """Run qualitative local review for bug tickets."""
    ticket_key = state["ticket_key"]
    workspace_path = state["workspace_path"]
    current_repo = state.get("current_repo", "")
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
        result, output = await run_review_container(
            runner,
            workspace_path=Path(workspace_path),
            task_summary="Qualitative bug review — root cause and test coverage",
            step_name="local_review",
            policy_key="bug_local_review",
            skill_name="local-review-bug",
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-qualreview",
            repo_name=current_repo,
        )

        state = merge_review_exhaustion(state, result, ticket_key, "local_review")

        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] fix: address review feedback")

        verdict, feedback = _parse_bug_verdict(output)

        new_retry_count = next_review_attempt(
            qualitative_retry_count,
            passed=verdict == "adequate",
        )

        if verdict == "adequate":
            logger.info(f"Bug qualitative review passed for {ticket_key}")
            return await _persist_review_result(
                state,
                git,
                {
                    "local_review_verdict": verdict,
                    "qualitative_feedback": feedback or None,
                    "qualitative_retry_count": qualitative_retry_count,
                    "current_node": "create_pr",
                    "last_error": None,
                },
            )

        # Non-adequate verdict
        if review_attempts_exhausted(new_retry_count, _QUALITATIVE_CAP):
            logger.warning(
                f"Qualitative review cap ({_QUALITATIVE_CAP}) reached for {ticket_key}, "
                f"proceeding with warning"
            )
            return await _persist_review_result(
                state,
                git,
                {
                    "local_review_verdict": verdict,
                    "qualitative_feedback": feedback or None,
                    "qualitative_retry_count": new_retry_count,
                    "qualitative_review_failed": True,
                    "current_node": "create_pr",
                    "last_error": None,
                },
            )

        logger.info(
            f"Bug qualitative review: verdict={verdict} for {ticket_key}, "
            f"retry {new_retry_count}/{_QUALITATIVE_CAP}"
        )
        linked_task_keys = state.get("linked_task_keys") or state.get("task_keys") or []
        return await _persist_review_result(
            state,
            git,
            {
                "local_review_verdict": verdict,
                "qualitative_feedback": feedback or None,
                "qualitative_retry_count": new_retry_count,
                "current_node": "implement_bug_fix",
                "last_error": None,
                # Reset so implement_task re-runs the container instead of seeing "all done"
                "implemented_tasks": [],
                "current_task_key": linked_task_keys[0] if linked_task_keys else None,
            },
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


async def _run_feature_review(state: WorkflowState, git: GitOperations) -> WorkflowState:
    """Run mechanical local review for non-bug tickets (existing behavior)."""
    ticket_key = state["ticket_key"]
    workspace_path = state["workspace_path"]
    review_attempts = state.get("local_review_attempts", 0)
    current_repo = state.get("current_repo", "")
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
                "local_review_attempts": 0,
                "current_node": "create_pr",
            }
        )

    logger.info(
        f"Running local code review for {ticket_key} "
        f"(attempt {review_attempts + 1}/{MAX_REVIEW_ATTEMPTS})"
    )

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
        result, output = await run_review_container(
            runner,
            workspace_path=Path(workspace_path),
            task_summary="Local code review — fix breaking issues",
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-review",
            repo_name=current_repo,
            step_name="local_review",
            policy_key="local_code_review",
            skill_name="local-code-review",
        )

        state = merge_review_exhaustion(state, result, ticket_key, "local_review")

        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] fix: address breaking issues found in local review")
            logger.info(f"Committed local review fixes for {ticket_key}")

        has_unfixed = _has_unfixed_breaking_issues(output)

        if has_unfixed and review_attempts + 1 < MAX_REVIEW_ATTEMPTS:
            logger.warning(
                f"Breaking issues remain after review attempt {review_attempts + 1}, retrying"
            )
            next_pass = (validated_pass or 1) + 1
            return await _persist_review_result(
                state,
                git,
                {
                    "local_review_attempts": review_attempts + 1,
                    "local_review_pass_number": next_pass,
                    "current_node": "local_review",
                    "last_error": None,
                },
            )

        if has_unfixed:
            logger.warning(
                f"Could not fix all breaking issues after {MAX_REVIEW_ATTEMPTS} attempts "
                f"for {ticket_key}, proceeding to PR"
            )
        else:
            logger.info(f"Local review passed for {ticket_key}")

        return await _persist_review_result(
            state,
            git,
            {
                "local_review_attempts": 0,
                "current_node": "create_pr",
                "last_error": None,
            },
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


async def _persist_review_result(
    state: WorkflowState,
    git: GitOperations,
    updates: dict,
) -> WorkflowState:
    """Persist review changes before applying the review's routing decision."""
    try:
        await push_to_fork_with_retry(git, use_fork=use_fork_remote(state))
    except PushPersistenceError as exc:
        pending_state = {
            **state,
            "review_push_pending": True,
            "review_push_pending_updates": updates,
        }
        return _review_persistence_error_state(pending_state, exc)
    return update_state_timestamp(
        {
            **state,
            **updates,
            "review_push_pending": False,
            "review_push_pending_updates": {},
            "persistence_retry_count": 0,
        }
    )


def _review_persistence_error_state(
    state: WorkflowState,
    error: PushPersistenceError,
) -> WorkflowState:
    return update_state_timestamp(
        build_persistence_error_state(
            state,
            error,
            retry_node="local_review",
            escalation_node="escalate_blocked",
        )
    )
