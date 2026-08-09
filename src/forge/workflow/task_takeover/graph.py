"""Task Takeover workflow graph construction.

This module builds the LangGraph StateGraph for the Task Takeover workflow.
"""

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel, JiraStatus
from forge.workflow.gates.task_plan_approval import (
    route_task_plan_approval,
    task_plan_approval_gate,
)
from forge.workflow.nodes import (
    answer_question,
    attempt_ci_fix,
    create_pull_request,
    escalate_to_blocked,
    evaluate_ci_status,
    execute_task_changes,
    generate_plan,
    human_review_gate,
    implement_review,
    review_response_gate,
    route_human_review,
    route_review_response,
    route_triage_gate,
    run_qualitative_review,
    setup_workspace,
    teardown_and_route,
    triage_gate,
    triage_task,
    wait_for_ci_gate,
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
        elif current_node == "execute_task_changes":
            return "execute_task_changes"
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
        return "execute_task_changes"

    logger.error(f"Workspace setup failed: {last_error}")
    return "escalate_blocked"


def _route_after_execution(state: TaskTakeoverState) -> str:
    """Never review an implementation whose branch was not persisted."""
    last_error = state.get("last_error")
    if not last_error:
        return "run_qualitative_review"
    if state.get("persistence_retry_count", 0) >= 3 or state.get("retry_count", 0) >= 3:
        return "escalate_blocked"
    return "execute_task_changes"


def _route_after_qualitative_review(state: TaskTakeoverState) -> str:
    """Route after run_qualitative_review considering qualitative verdict and retry count.

    The routing logic is state-driven:
      - If there is an active error (last_error is set), always route to escalate_blocked if we've reached or exceeded the retry cap limit, or retry the review if under the limit.
      - If review is adequate, proceed to create_pr.
      - If we reached the retry cap and there are no active errors, we can proceed to create_pr only if commits were successfully made (commit_info.committed is True).
      - Otherwise, escalate or loop back to execute_task_changes.
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
        "routing back to execute_task_changes"
    )
    return "execute_task_changes"


def _route_after_pr_creation(state: TaskTakeoverState) -> str:
    """Route after PR creation: teardown on success, escalate on failure."""
    last_error = state.get("last_error")
    pr_urls = state.get("pr_urls", [])
    if last_error and not pr_urls:
        return "escalate_blocked"
    return "teardown_workspace"


def _route_after_teardown(state: TaskTakeoverState) -> str:
    """Route after teardown: next repo or wait for CI."""
    repos_to_process = state.get("repos_to_process", [])
    repos_completed = state.get("repos_completed", [])
    remaining = [r for r in repos_to_process if r not in repos_completed]
    if remaining:
        return "setup_workspace"
    return "wait_for_ci_gate"


def _route_ci_evaluation(state: TaskTakeoverState) -> str:
    """Route based on CI evaluation results."""
    ci_status = state.get("ci_status", "")
    routes = {
        "passed": "human_review_gate",
        "fixing": "attempt_ci_fix",
        "pending": END,
    }
    return routes.get(ci_status, "escalate_blocked")


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


def build_task_takeover_graph() -> StateGraph[TaskTakeoverState, Any, Any]:
    """Create the Task Takeover workflow graph.

    Returns:
        Configured StateGraph ready for compilation.
    """
    graph = StateGraph(TaskTakeoverState)

    # Entry routing
    graph.add_node("route_entry", lambda state: state)

    # Nodes
    graph.add_node("triage_check", triage_task)
    graph.add_node("triage_gate", triage_gate)
    graph.add_node("generate_plan", generate_plan)
    graph.add_node("task_plan_approval_gate", task_plan_approval_gate)
    graph.add_node("escalate_blocked", escalate_to_blocked)
    graph.add_node("answer_question", answer_question)
    graph.add_node("setup_workspace", setup_workspace)
    graph.add_node("execute_task_changes", execute_task_changes)
    graph.add_node("run_qualitative_review", run_qualitative_review)
    graph.add_node("create_pr", create_pull_request)
    graph.add_node("teardown_workspace", teardown_and_route)
    graph.add_node("wait_for_ci_gate", wait_for_ci_gate)
    graph.add_node("ci_evaluator", evaluate_ci_status)
    graph.add_node("attempt_ci_fix", attempt_ci_fix)
    graph.add_node("human_review_gate", human_review_gate)
    graph.add_node("implement_review", implement_review)
    graph.add_node("review_response_gate", review_response_gate)
    graph.add_node("complete_task_takeover", complete_task_takeover)

    # Set entry point
    graph.set_entry_point("route_entry")

    # Entry routing edges
    graph.add_conditional_edges(
        "route_entry",
        route_entry,
        {
            "triage_check": "triage_check",
            "triage_gate": "triage_gate",
            "generate_plan": "generate_plan",
            "task_plan_approval_gate": "task_plan_approval_gate",
            "setup_workspace": "setup_workspace",
            "execute_task_changes": "execute_task_changes",
            "run_qualitative_review": "run_qualitative_review",
            "create_pr": "create_pr",
            "teardown_workspace": "teardown_workspace",
            "wait_for_ci_gate": "wait_for_ci_gate",
            "ci_evaluator": "ci_evaluator",
            "attempt_ci_fix": "ci_evaluator",
            "human_review_gate": "human_review_gate",
            "implement_review": "implement_review",
            "review_response_gate": "review_response_gate",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )

    # Triage flow
    graph.add_conditional_edges(
        "triage_check",
        _route_after_triage_check,
        {
            "triage_check": "triage_check",
            "triage_gate": "triage_gate",
            "generate_plan": "generate_plan",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "triage_gate",
        route_triage_gate,
        {
            END: END,
            "triage_check": "triage_check",
        },
    )

    # Planning flow
    graph.add_conditional_edges(
        "generate_plan",
        _route_after_generate_plan,
        {
            "generate_plan": "generate_plan",
            "task_plan_approval_gate": "task_plan_approval_gate",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "task_plan_approval_gate",
        route_task_plan_approval,
        {
            "regenerate_plan": "generate_plan",
            "answer_question": "answer_question",
            "setup_workspace": "setup_workspace",
            END: END,
        },
    )

    # Execution flow
    graph.add_conditional_edges(
        "setup_workspace",
        _route_after_workspace_setup,
        {
            "execute_task_changes": "execute_task_changes",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "execute_task_changes",
        _route_after_execution,
        {
            "execute_task_changes": "execute_task_changes",
            "run_qualitative_review": "run_qualitative_review",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "run_qualitative_review",
        _route_after_qualitative_review,
        {
            "run_qualitative_review": "run_qualitative_review",
            "execute_task_changes": "execute_task_changes",
            "create_pr": "create_pr",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "create_pr",
        _route_after_pr_creation,
        {
            "teardown_workspace": "teardown_workspace",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "teardown_workspace",
        _route_after_teardown,
        {
            "setup_workspace": "setup_workspace",
            "wait_for_ci_gate": "wait_for_ci_gate",
        },
    )
    graph.add_conditional_edges(
        "wait_for_ci_gate",
        lambda s: END if s.get("is_paused") else "ci_evaluator",
        {END: END, "ci_evaluator": "ci_evaluator"},
    )
    graph.add_conditional_edges(
        "ci_evaluator",
        _route_ci_evaluation,
        {
            "human_review_gate": "human_review_gate",
            "attempt_ci_fix": "attempt_ci_fix",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "attempt_ci_fix",
        lambda s: s.get("current_node", "wait_for_ci_gate"),
        {
            "wait_for_ci_gate": "wait_for_ci_gate",
            "escalate_blocked": "escalate_blocked",
            "ci_evaluator": "ci_evaluator",
            "attempt_ci_fix": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "human_review_gate",
        _route_human_review_task_takeover,
        {
            "implement_review": "implement_review",
            "complete_task_takeover": "complete_task_takeover",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "implement_review",
        lambda s: s.get("current_node", "wait_for_ci_gate"),
        {
            "wait_for_ci_gate": "wait_for_ci_gate",
            "review_response_gate": "review_response_gate",
            "implement_review": "implement_review",
            "human_review_gate": "human_review_gate",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "review_response_gate",
        route_review_response,
        {
            "implement_review": "implement_review",
            "human_review_gate": "human_review_gate",
            END: END,
        },
    )
    graph.add_edge("complete_task_takeover", END)

    # Q&A routing
    graph.add_conditional_edges(
        "answer_question",
        _route_after_answer,
        {
            "task_plan_approval_gate": "task_plan_approval_gate",
        },
    )

    graph.add_edge("escalate_blocked", END)

    return graph
