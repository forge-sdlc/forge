"""Human review handling node for processing review feedback and merges."""

import logging
from typing import Any

from langgraph.graph import END

from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel, JiraStatus
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import (
    post_status_comment,
    remove_implementing_label,
    set_ci_pending_label,
)

logger = logging.getLogger(__name__)


async def human_review_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for CI and human review.

    On the first entry after PR creation, posts a PR creation status comment
    and transitions Jira labels from forge:implementing to forge:ci-pending.
    On subsequent entries (CI cycle or review cycle re-entries), skips the
    Jira comment to avoid duplicate noise. The one-time post is tracked with
    pr_created_comment_posted rather than ci_status, because the first CI
    webhook re-enters this gate while ci_status is still None (ci_evaluator
    has not run yet).
    """
    ticket_key = state["ticket_key"]
    ci_status = state.get("ci_status")

    if state.get("pr_merged"):
        logger.info(f"PR already merged for {ticket_key}, skipping pause at human_review_gate")
        return update_state_timestamp(
            {**state, "current_node": "human_review_gate", "is_paused": False}
        )

    updates: dict[str, Any] = {}
    if ci_status is None and not state.get("pr_created_comment_posted"):
        jira = JiraClient()
        try:
            pr_number = state.get("current_pr_number")
            if pr_number is not None:
                message = (
                    f"🚀 Pull request #{pr_number} created and submitted. "
                    "Waiting for CI checks and human review."
                )
            else:
                message = (
                    "🚀 Pull request created and submitted. Waiting for CI checks and human review."
                )
            await post_status_comment(jira, ticket_key, message)
            await remove_implementing_label(jira, ticket_key)
            await set_ci_pending_label(jira, ticket_key)
        finally:
            await jira.close()
        updates["pr_created_comment_posted"] = True
        logger.info(f"Pausing {ticket_key} at human_review_gate after PR creation")
    else:
        logger.info(f"Pausing {ticket_key} at human_review_gate (ci_status={ci_status!r})")

    return update_state_timestamp(
        {
            **state,
            **updates,
            "is_paused": True,
            "current_node": "human_review_gate",
        }
    )


def route_human_review(state: WorkflowState) -> str:
    """Route based on human review status.

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    # CI webhook arrived while paused at this gate — route through CI cycle
    if state.get("pending_ci_event"):
        logger.info(f"CI event pending for {state['ticket_key']}, routing to ci_evaluator")
        return "ci_evaluator"

    # Check if changes were requested — route to dedicated review implementation node
    if state.get("revision_requested") and state.get("feedback_comment"):
        logger.info(f"Changes requested for {state['ticket_key']}")
        return "implement_review"

    # Check if merged
    if state.get("pr_merged"):
        logger.info(f"PR merged for {state['ticket_key']}")
        return "complete_tasks"

    # Still waiting for review - END and wait for webhook
    if state.get("is_paused"):
        logger.info(
            f"Human review: workflow paused for {state['ticket_key']}, waiting for review webhook"
        )
        return END

    return "complete_tasks"


async def complete_tasks(state: WorkflowState) -> WorkflowState:
    """Complete Tasks after successful PR merge.

    This node transitions Tasks to Done status.

    Args:
        state: Current workflow state.

    Returns:
        Updated state with Tasks completed.
    """
    ticket_key = state["ticket_key"]
    implemented_tasks = state.get("implemented_tasks", [])

    logger.info(f"Completing {len(implemented_tasks)} Tasks for {ticket_key}")

    jira = JiraClient()

    try:
        for task_key in implemented_tasks:
            try:
                # Transition to Closed status and remove forge workflow labels
                await jira.transition_issue(task_key, JiraStatus.CLOSED.value)
                await jira.set_workflow_label(task_key, ForgeLabel.TASK_REVIEW_APPROVED)
                logger.info(f"Task {task_key} marked as Done")
            except Exception as e:
                logger.warning(f"Failed to complete Task {task_key}: {e}")

        return update_state_timestamp(
            {
                **state,
                "tasks_completed": True,
                "current_node": "aggregate_epic_status",
                "ci_fix_attempt": 0,
            }
        )

    except Exception as e:
        logger.error(f"Task completion failed for {ticket_key}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": "complete_tasks",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()


async def aggregate_epic_status(state: WorkflowState) -> WorkflowState:
    """Check if all Tasks in Epics are done, update Epic status.

    Args:
        state: Current workflow state.

    Returns:
        Updated state with Epic aggregation.
    """
    ticket_key = state["ticket_key"]
    epic_keys = state.get("epic_keys", [])
    implemented_tasks = state.get("implemented_tasks", [])

    logger.info(f"Aggregating Epic status for {ticket_key}")

    jira = JiraClient()

    try:
        if not epic_keys:
            epic_keys = await _derive_epic_keys_from_tasks(jira, implemented_tasks)

        all_epics_done = True

        for epic_key in epic_keys:
            # Check if all Tasks under this Epic are done
            # In practice, would query Jira for child issues
            epic_done = await _check_epic_completion(jira, epic_key)

            if epic_done:
                # Transition Epic to Closed status
                await jira.transition_issue(epic_key, JiraStatus.CLOSED.value)
                logger.info(f"Epic {epic_key} marked as Done")
            else:
                all_epics_done = False

        if all_epics_done:
            return update_state_timestamp(
                {
                    **state,
                    "epic_keys": epic_keys,
                    "epics_completed": True,
                    "current_node": "aggregate_feature_status",
                }
            )

        return update_state_timestamp(
            {
                **state,
                "current_node": "complete",
            }
        )

    except Exception as e:
        logger.error(f"Epic aggregation failed for {ticket_key}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": "aggregate_epic_status",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()


async def aggregate_feature_status(state: WorkflowState) -> WorkflowState:
    """Check if all Epics are done, update Feature status.

    Args:
        state: Current workflow state.

    Returns:
        Updated state with Feature completed.
    """
    ticket_key = state["ticket_key"]

    logger.info(f"Aggregating Feature status for {ticket_key}")

    jira = JiraClient()

    try:
        # Transition Feature to Closed status
        await jira.transition_issue(ticket_key, JiraStatus.CLOSED.value)
        logger.info(f"Feature {ticket_key} marked as Done")

        # Add completion comment
        await post_status_comment(
            jira, ticket_key, "All Epics and Tasks completed. Feature implementation done."
        )

        return update_state_timestamp(
            {
                **state,
                "feature_completed": True,
                "current_node": "complete",
                "generation_context": {},  # Clear - no longer needed
                "qa_history": [],  # Clear - already posted as summary
            }
        )

    except Exception as e:
        logger.error(f"Feature completion failed for {ticket_key}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": "aggregate_feature_status",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()


async def _check_epic_completion(jira: JiraClient, epic_key: str) -> bool:
    """Check if all Tasks under an Epic are done.

    Args:
        jira: Jira client.
        epic_key: Epic to check.

    Returns:
        True if all Tasks are done.
    """
    try:
        children = await jira.get_epic_children(epic_key)

        if not children:
            # Edge case: Epic with no Tasks is considered complete
            logger.warning(f"Epic {epic_key} has no child Tasks - treating as complete")
            return True

        done_statuses = {"Done", "Closed", "Resolved"}
        incomplete = [child for child in children if child.status not in done_statuses]

        if incomplete:
            logger.info(
                f"Epic {epic_key} has {len(incomplete)} incomplete Tasks: "
                f"{[c.key for c in incomplete]}"
            )
            return False

        logger.info(f"All {len(children)} Tasks under Epic {epic_key} are done")
        return True

    except Exception as e:
        logger.error(f"Failed to check Epic completion for {epic_key}: {e}")
        # On error, don't falsely report completion
        return False


async def _derive_epic_keys_from_tasks(
    jira: JiraClient,
    task_keys: list[str],
) -> list[str]:
    """Derive Epic keys from implemented Task parents when state lost them."""
    epic_keys: list[str] = []
    seen: set[str] = set()

    for task_key in task_keys:
        try:
            issue = await jira.get_issue(task_key)
        except Exception as e:
            logger.warning(f"Failed to fetch Task {task_key} while deriving Epics: {e}")
            continue

        if not issue.parent_key or issue.parent_key in seen:
            continue

        seen.add(issue.parent_key)
        epic_keys.append(issue.parent_key)

    if epic_keys:
        logger.info(f"Derived Epic keys from implemented Tasks: {epic_keys}")
    else:
        logger.warning("No Epic keys available or derivable from implemented Tasks")

    return epic_keys
