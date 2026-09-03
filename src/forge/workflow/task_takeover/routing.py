"""Task Takeover workflow graph construction.

This module builds the LangGraph StateGraph for the Task Takeover workflow.
"""

import logging

from langgraph.graph import END, StateGraph

from forge.models.workflow import ForgeLabel, JiraStatus
from forge.workflow.effect_runtime import JiraClient
from forge.workflow.nodes import (
    route_human_review,
)
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import resolve_shared_resume_node, update_state_timestamp

logger = logging.getLogger(__name__)
QUALITATIVE_REVIEW_MAX_ATTEMPTS = 2
PLAN_MAX_ATTEMPTS = 3


def route_entry(state: TaskTakeoverState) -> str:
    """Route workflow based on current progress for resume/retry.

    New tickets start at triage_check. In-flight tickets with a saved current_node
    resume at the appropriate point.

    Args:
        state: Current workflow state.

    Returns:
        Next node name based on current progress.
    """
    current_node = state.get("current_node", "")

    if current_node and current_node not in ("entry", "route_entry", "__end__", "", "start"):
        logger.info(f"Resuming task takeover workflow at node: {current_node}")

        # Shared nodes: same resume mapping across all workflow types
        shared = resolve_shared_resume_node(current_node)
        if shared is not None:
            if shared is END:
                logger.info(f"Workflow at terminal state '{current_node}', returning END")
            return shared

        # Task takeover-specific resume mapping
        if current_node == "triage_check":
            return "triage_check"
        elif current_node == "triage_gate":
            return "triage_gate"
        elif current_node == "generate_plan":
            return "generate_plan"
        elif current_node == "task_plan_approval_gate":
            return "task_plan_approval_gate"
        elif current_node == "setup_workspace":
            return "setup_workspace"
        elif current_node == "implement_work":
            return "implement_work"
        elif current_node == "qualitative_review":
            return "run_qualitative_review"
        elif current_node == "create_pr":
            return "create_pr"
        elif current_node == "teardown_workspace":
            return "teardown_workspace"
        elif current_node == "escalate_blocked":
            return "escalate_blocked"
        else:
            logger.warning(f"Unrecognized current_node '{current_node}', restarting from triage")

    # New tasks start at triage
    return "triage_check"


def _route_after_triage_check(state: TaskTakeoverState) -> str:
    """Route after triage_check based on what triage_check set as current_node."""
    node = state.get("current_node", "triage_gate")
    if node == "triage_check":
        return "triage_check"
    if node in ("analyze_bug", "generate_plan"):
        return "generate_plan"
    if node in ("triage_gate", "escalate_blocked"):
        return node
    return "triage_gate"


def _route_after_generate_plan(state: TaskTakeoverState) -> str:
    """Route after planning without pausing for approval when no plan was generated."""
    current_node = state.get("current_node", "task_plan_approval_gate")
    if current_node == "generate_plan" and state.get("last_error"):
        if state.get("retry_count", 0) >= PLAN_MAX_ATTEMPTS:
            return "escalate_blocked"
        return "generate_plan"
    if current_node in ("task_plan_approval_gate", "escalate_blocked"):
        return current_node
    logger.error(f"Task takeover plan generation returned unexpected node {current_node!r}")
    return "escalate_blocked"


def _route_after_answer(state: TaskTakeoverState) -> str:
    """Route back to the original gate after answering a question.

    The answer_question node preserves current_node as the gate to return to.
    """
    current_node = state.get("current_node", "")
    if current_node and "gate" in current_node:
        return current_node
    return "task_plan_approval_gate"


def _route_after_workspace_setup(state: TaskTakeoverState) -> str:
    """Route to execution only after workspace setup completed successfully."""
    workspace_path = state.get("workspace_path")
    last_error = state.get("last_error")

    if workspace_path and not last_error:
        return "implement_work"

    logger.error(f"Workspace setup failed: {last_error}")
    return "escalate_blocked"


def _route_after_execution(state: TaskTakeoverState) -> str:
    """Never review an implementation whose branch was not persisted."""
    last_error = state.get("last_error")
    if not last_error:
        return "run_qualitative_review"
    if state.get("persistence_retry_count", 0) >= 3 or state.get("retry_count", 0) >= 3:
        return "escalate_blocked"
    return "implement_work"


def _route_after_qualitative_review(state: TaskTakeoverState) -> str:
    """Route after run_qualitative_review considering qualitative verdict and retry count.

    The routing logic is state-driven:
      - If there is an active error (last_error is set), always route to escalate_blocked if we've reached or exceeded the retry cap limit, or retry the review if under the limit.
      - If review is adequate, proceed to create_pr.
      - If we reached the retry cap and there are no active errors, we can proceed to create_pr only if commits were successfully made (commit_info.committed is True).
      - Otherwise, escalate or loop back to implement_work.
    """
    verdict = state.get("review_verdict")
    retry_count = state.get("qualitative_review_retry_count", 0)
    last_error = state.get("last_error")

    limit = QUALITATIVE_REVIEW_MAX_ATTEMPTS

    if last_error:
        if retry_count >= limit:
            logger.error(
                "Qualitative review retry limit reached with active error: %s. Escalating.",
                last_error,
            )
            return "escalate_blocked"
        else:
            logger.warning(
                "Qualitative review execution failed; retrying review (%s/%s): %s",
                retry_count,
                limit,
                last_error,
            )
            return "run_qualitative_review"

    if verdict == "adequate":
        return "create_pr"

    if retry_count >= limit:
        commit_info = state.get("commit_info") or {}
        committed = commit_info.get("committed", False)

        if not committed:
            logger.warning(
                "Qualitative review retry limit reached with no committed changes. Escalating."
            )
            return "escalate_blocked"

        logger.warning(
            f"Qualitative review cap ({limit}) reached on task takeover workflow, "
            "proceeding to PR creation with review state retained"
        )
        return "create_pr"

    logger.info(
        f"Qualitative review verdict is {verdict!r}, retry attempt {retry_count}/{limit}, "
        "routing back to implement_work"
    )
    return "implement_work"


def _route_human_review_task_takeover(state: TaskTakeoverState) -> str:
    """Route after human_review_gate for a standalone Task/Epic PR."""
    if state.get("pr_merged"):
        return "complete_task_takeover"
    next_node = route_human_review(state)
    if next_node == "complete_tasks":
        return "complete_task_takeover"
    return next_node


async def complete_task_takeover(state: TaskTakeoverState) -> TaskTakeoverState:
    """Mark Task Takeover workflow complete after PR merge and transition Jira ticket."""
    ticket_key = state["ticket_key"]
    logger.info(f"Completing task takeover workflow for ticket: {ticket_key}")

    jira = JiraClient()
    try:
        try:
            await jira.transition_issue(ticket_key, JiraStatus.CLOSED.value)
            await jira.set_workflow_label(ticket_key, ForgeLabel.TASK_REVIEW_APPROVED)
            logger.info(f"Task {ticket_key} successfully transitioned to Closed/Done")
        except Exception as e:
            logger.warning(f"Failed to transition Jira status/label for {ticket_key}: {e}")
    finally:
        await jira.close()

    return update_state_timestamp(
        {
            **state,
            "current_node": "complete",
            "is_paused": False,
            "ci_fix_attempt": 0,
        }
    )


def build_task_takeover_graph() -> StateGraph:
    """Build the governed graph from its versioned process definition."""
    from forge.workflow.declarative.builtins import builtin_task_takeover_definition
    from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler

    return DeclarativeWorkflowCompiler(builtin_task_takeover_definition()).build_graph()
