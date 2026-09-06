"""Utility functions for workflow state management and comment classification."""

from datetime import datetime
from typing import Any

from langgraph.graph import END

from forge.workflow.utils.comment_classifier import (
    CommentType,
    classify_comment,
    parse_comment_command,
)
from forge.workflow.utils.draft_manager import DraftManager
from forge.workflow.utils.jira_status import (
    post_status_comment,
    remove_implementing_label,
    set_ci_pending_label,
    set_implementing_label,
    set_review_pending_label,
    transition_tasks_to_in_progress,
)
from forge.workflow.utils.qa_summary import post_qa_summary_if_needed
from forge.workflow.utils.review_report import (
    collect_review_exhaustion as collect_review_exhaustion,
)
from forge.workflow.utils.review_report import (
    merge_review_exhaustion as merge_review_exhaustion,
)

# Nodes whose resume mapping is identical across all workflow types.
# Used by route_entry / route_by_ticket_type to avoid copy-pasting the same
# elif branches in every workflow graph.
_SHARED_RESUME_MAP: dict[str, str] = {
    "local_review": "local_review",
    "update_documentation": "update_documentation",
    "human_review_gate": "human_review_gate",
    "implement_review": "implement_review",
    "review_response_gate": "review_response_gate",
    "ci_evaluator": "ci_evaluator",
    "attempt_ci_fix": "ci_evaluator",
    # wait_for_ci_gate was merged into human_review_gate so CI and review run
    # concurrently; this compatibility alias lets a ticket already
    # checkpointed at wait_for_ci_gate before that merge resume correctly
    # instead of silently restarting its whole workflow from the beginning.
    "wait_for_ci_gate": "human_review_gate",
}

_TERMINAL_NODES: frozenset[str] = frozenset({"complete"})


def resolve_shared_resume_node(current_node: str) -> str | None:
    """Return the resume target for nodes common to all workflow types, or None.

    Returns END for terminal nodes, the mapped node name for shared intermediate
    nodes, and None for workflow-specific nodes that each graph must handle itself.
    """
    if current_node in _TERMINAL_NODES:
        return END
    return _SHARED_RESUME_MAP.get(current_node)


def update_state_timestamp(state: dict[str, Any]) -> dict[str, Any]:
    """Update the state timestamp."""
    return {**state, "updated_at": datetime.utcnow().isoformat()}


def set_paused(state: dict[str, Any], node_name: str) -> dict[str, Any]:
    """Set the state to paused at a specific node."""
    return {
        **state,
        "current_node": node_name,
        "is_paused": True,
        "updated_at": datetime.utcnow().isoformat(),
    }


def resume_state(state: dict[str, Any]) -> dict[str, Any]:
    """Resume a paused state."""
    return {
        **state,
        "is_paused": False,
        "updated_at": datetime.utcnow().isoformat(),
    }


def set_error(state: dict[str, Any], error: str) -> dict[str, Any]:
    """Record an error in the state."""
    return {
        **state,
        "last_error": error,
        "retry_count": state.get("retry_count", 0) + 1,
        "updated_at": datetime.utcnow().isoformat(),
    }


def check_yolo_mode(state: Any, labels: list[str] | None = None) -> bool:
    """Check if YOLO mode is enabled based on labels or state.

    The components are:
    1. 'forge:yolo' label in the provided labels or the state context labels.
    2. State yolo_mode.
    """
    # 1. Label check
    has_label = False
    if (labels and "forge:yolo" in labels) or "forge:yolo" in state.get("context", {}).get(
        "labels", []
    ):
        has_label = True

    # 2. State check
    return has_label or bool(state.get("yolo_mode", False))


def check_direct_mode(state: Any, labels: list[str] | None = None) -> bool:
    """Check if direct ticket creation mode is enabled based on labels or state.

    The components are:
    1. ForgeLabel.DIRECT_MODE label in the provided labels or the state context labels.
    2. State direct_mode.
    """
    from forge.models.workflow import ForgeLabel

    has_label = False
    if (labels and ForgeLabel.DIRECT_MODE in labels) or ForgeLabel.DIRECT_MODE in state.get(
        "context", {}
    ).get("labels", []):
        has_label = True

    return has_label or bool(state.get("direct_mode", False))


__all__ = [
    "check_yolo_mode",
    "check_direct_mode",
    "CommentType",
    "classify_comment",
    "collect_review_exhaustion",
    "merge_review_exhaustion",
    "parse_comment_command",
    "post_qa_summary_if_needed",
    "post_status_comment",
    "remove_implementing_label",
    "resolve_shared_resume_node",
    "resume_state",
    "set_ci_pending_label",
    "set_error",
    "set_implementing_label",
    "set_paused",
    "set_review_pending_label",
    "transition_tasks_to_in_progress",
    "update_state_timestamp",
    "DraftManager",
]
