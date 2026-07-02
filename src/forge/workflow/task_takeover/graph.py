"""Task Takeover workflow graph construction.

This module builds the LangGraph StateGraph for the Task Takeover workflow.
"""

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from forge.workflow.gates.task_plan_approval import (
    route_task_plan_approval,
    task_plan_approval_gate,
)
from forge.workflow.nodes import (
    answer_question,
    create_task_takeover_pr,
    escalate_to_blocked,
    execute_task_changes,
    generate_plan,
    route_triage_gate,
    run_qualitative_review,
    setup_workspace,
    triage_gate,
    triage_task,
)
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import resolve_shared_resume_node

logger = logging.getLogger(__name__)
QUALITATIVE_REVIEW_MAX_ATTEMPTS = 2


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
        elif current_node == "create_task_takeover_pr":
            return "create_task_takeover_pr"
        elif current_node == "escalate_blocked":
            return "escalate_blocked"
        else:
            logger.warning(f"Unrecognized current_node '{current_node}', restarting from triage")

    # New tasks start at triage
    return "triage_check"


def _route_after_triage_check(state: TaskTakeoverState) -> str:
    """Route after triage_check based on what triage_check set as current_node."""
    node = state.get("current_node", "triage_gate")
    if node in ("analyze_bug", "generate_plan"):
        return "generate_plan"
    if node in ("triage_gate", "escalate_blocked"):
        return node
    return "triage_gate"


def _route_after_answer(state: TaskTakeoverState) -> str:
    """Route back to the original gate after answering a question.

    The answer_question node preserves current_node as the gate to return to.
    """
    current_node = state.get("current_node", "")
    if current_node and "gate" in current_node:
        return current_node
    return "task_plan_approval_gate"


def _route_after_qualitative_review(state: TaskTakeoverState) -> str:
    """Route after run_qualitative_review considering qualitative verdict and retry count.

    If the review is adequate (success), proceed to create_task_takeover_pr.
    If the review is failed or incomplete:
      - Check if we've reached the configured retry limit.
      - If limit reached: transition to escalate_blocked.
      - Otherwise: transition back to execute_task_changes.
    """
    verdict = state.get("review_verdict")
    retry_count = state.get("qualitative_review_retry_count", 0)

    if verdict == "adequate":
        return "create_task_takeover_pr"

    limit = QUALITATIVE_REVIEW_MAX_ATTEMPTS

    if retry_count >= limit:
        logger.warning(
            f"Qualitative review cap ({limit}) reached on task takeover workflow, transitioning to escalate_blocked"
        )
        return "escalate_blocked"

    logger.info(
        f"Qualitative review verdict is {verdict!r}, retry attempt {retry_count}/{limit}, "
        "routing back to execute_task_changes"
    )
    return "execute_task_changes"


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
    graph.add_node("create_task_takeover_pr", create_task_takeover_pr)

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
            "create_task_takeover_pr": "create_task_takeover_pr",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )

    # Triage flow
    graph.add_conditional_edges(
        "triage_check",
        _route_after_triage_check,
        {
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
    graph.add_edge("generate_plan", "task_plan_approval_gate")
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
    graph.add_edge("setup_workspace", "execute_task_changes")
    graph.add_edge("execute_task_changes", "run_qualitative_review")
    graph.add_conditional_edges(
        "run_qualitative_review",
        _route_after_qualitative_review,
        {
            "execute_task_changes": "execute_task_changes",
            "create_task_takeover_pr": "create_task_takeover_pr",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_edge("create_task_takeover_pr", END)

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
