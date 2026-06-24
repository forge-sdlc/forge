"""Human review handling node for processing review feedback and merges."""

import logging

from langgraph.graph import END

from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel, JiraStatus
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import set_paused, update_state_timestamp

logger = logging.getLogger(__name__)


def human_review_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for human code review.

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True.
    """
    ticket_key = state["ticket_key"]
    pr_urls = state.get("pr_urls", [])

    logger.info(f"Human review gate: pausing for {ticket_key} ({len(pr_urls)} PRs)")

    return set_paused(state, "human_review_gate")


def route_human_review(state: WorkflowState) -> str:
    """Route based on human review status.

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
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
                "current_attempt": 0,  # Reset CI attempt counter on completion
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

    logger.info(f"Aggregating Epic status for {ticket_key}")

    jira = JiraClient()

    try:
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
        await jira.add_comment(
            ticket_key, "All Epics and Tasks completed. Feature implementation done."
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
