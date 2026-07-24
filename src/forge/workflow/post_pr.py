"""Shared post-PR lifecycle nodes and edges for all workflow types.

All three workflow types (feature, bug, task_takeover) share identical
CI/review node wiring after PR creation. This module provides that shared
wiring so each graph builder calls two functions instead of duplicating ~60
lines of node and edge declarations.
"""

import logging
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)


def _route_after_teardown(state: dict[str, Any]) -> str:
    """Route after workspace teardown: next repo or human_review_gate."""
    repos_to_process = state.get("repos_to_process", [])
    repos_completed = state.get("repos_completed", [])
    remaining = [r for r in repos_to_process if r not in repos_completed]
    return "setup_workspace" if remaining else "human_review_gate"


def route_after_pr_creation(state: dict[str, Any]) -> str:
    """Route after PR creation: teardown on success, escalate on failure."""
    last_error = state.get("last_error")
    pr_urls = state.get("pr_urls", [])
    if last_error and not pr_urls:
        return "escalate_blocked"
    return "teardown_workspace"


def _route_ci_evaluation(state: dict[str, Any]) -> str:
    """Route after ci_evaluator.

    'fixing' → attempt_ci_fix.
    'failed' or 'blocked' → escalate_blocked.
    All other statuses ('pending', 'passed', 'external_failure', etc.) → human_review_gate
    so the gate re-pauses and waits for the next CI or review webhook.
    Unknown/unexpected statuses → escalate_blocked as safety net.
    """
    ci_status = state.get("ci_status", "")
    if ci_status == "fixing":
        return "attempt_ci_fix"
    if ci_status in ("failed", "blocked", "no_prs"):
        return "escalate_blocked"
    # All known non-fix/non-fail statuses go back to gate
    if ci_status in ("pending", "passed", "external_failure"):
        return "human_review_gate"
    # Unknown status — safety escalation
    logger.warning(f"Unexpected ci_status: {ci_status!r}, routing to escalate_blocked")
    return "escalate_blocked"


def add_post_pr_nodes(graph: StateGraph) -> None:
    """Add all shared CI/review nodes to a workflow graph."""
    from forge.workflow.nodes.ci_evaluator import (
        attempt_ci_fix,
        escalate_to_blocked,
        evaluate_ci_status,
    )
    from forge.workflow.nodes.human_review import human_review_gate
    from forge.workflow.nodes.implement_review import implement_review, review_response_gate
    from forge.workflow.nodes.rebase import rebase_pr

    graph.add_node("ci_evaluator", evaluate_ci_status)
    graph.add_node("attempt_ci_fix", attempt_ci_fix)
    graph.add_node("escalate_blocked", escalate_to_blocked)
    graph.add_node("human_review_gate", human_review_gate)
    graph.add_node("implement_review", implement_review)
    graph.add_node("review_response_gate", review_response_gate)
    graph.add_node("rebase_pr", rebase_pr)


def add_post_pr_edges(
    graph: StateGraph,
    on_complete_node: str,
    human_review_routing_fn: Callable | None = None,
) -> None:
    """Add all shared CI/review edges to a workflow graph.

    Args:
        graph: The StateGraph to wire.
        on_complete_node: Node to route to when PR is merged (e.g. "complete_tasks").
            For the feature workflow pass "complete_tasks" (the default route_human_review
            return value). For bug pass "post_merge_summary" and supply _route_human_review_bug
            as human_review_routing_fn.
        human_review_routing_fn: Routing function for human_review_gate conditional edges.
            Defaults to route_human_review (used by feature workflow). Bug and task_takeover
            pass their workflow-specific wrapper that intercepts pr_merged.
    """
    from forge.workflow.nodes.human_review import route_human_review
    from forge.workflow.nodes.implement_review import route_review_response

    if human_review_routing_fn is None:
        human_review_routing_fn = route_human_review

    # teardown → human_review_gate or next repo
    graph.add_conditional_edges(
        "teardown_workspace",
        _route_after_teardown,
        {"setup_workspace": "setup_workspace", "human_review_gate": "human_review_gate"},
    )

    # CI cycle: ci_evaluator → fix or gate
    graph.add_conditional_edges(
        "ci_evaluator",
        _route_ci_evaluation,
        {
            "human_review_gate": "human_review_gate",
            "attempt_ci_fix": "attempt_ci_fix",
            "escalate_blocked": "escalate_blocked",
        },
    )

    # attempt_ci_fix returns via current_node in state
    graph.add_conditional_edges(
        "attempt_ci_fix",
        lambda s: s.get("current_node", "human_review_gate"),
        {
            "human_review_gate": "human_review_gate",
            "escalate_blocked": "escalate_blocked",
            "ci_evaluator": "ci_evaluator",
            "attempt_ci_fix": "escalate_blocked",  # safety: loop → escalate
        },
    )

    # Gate routing — accepts CI, review, and merge signals
    # on_complete_node is the workflow-specific merge destination.
    # "complete_tasks" is also mapped to on_complete_node as a defensive catch
    # for callers that pass a custom routing fn which still uses route_human_review
    # as a fallthrough.
    human_review_targets: dict[str, str] = {
        "ci_evaluator": "ci_evaluator",
        "implement_review": "implement_review",
        on_complete_node: on_complete_node,
        END: END,
    }
    if on_complete_node != "complete_tasks":
        human_review_targets["complete_tasks"] = on_complete_node
    graph.add_conditional_edges(
        "human_review_gate",
        human_review_routing_fn,
        human_review_targets,
    )

    # Review cycle
    graph.add_conditional_edges(
        "implement_review",
        lambda s: s.get("current_node", "human_review_gate"),
        {
            "human_review_gate": "human_review_gate",
            "review_response_gate": "review_response_gate",
            "implement_review": "implement_review",
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

    # Terminal
    graph.add_edge("escalate_blocked", END)
