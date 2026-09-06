"""Bug workflow graph construction.

This module builds the LangGraph StateGraph for the Bug workflow.
"""

import logging
from typing import Literal

from langgraph.graph import END, StateGraph

from forge.workflow.bug.state import BugState
from forge.workflow.nodes.human_review import route_human_review
from forge.workflow.nodes.local_reviewer import local_review_changes
from forge.workflow.nodes.plan_bug_fix import (
    _MAX_PLAN_RETRIES,
)
from forge.workflow.nodes.qa_handler import answer_question
from forge.workflow.utils import resolve_shared_resume_node

logger = logging.getLogger(__name__)

_MAX_REFLECTION_COUNT = 3


# LangGraph filters state channels based on each node function's type annotation.
# local_review_changes is typed as FeatureState, which lacks bug-specific fields
# like qualitative_retry_count, so this wrapper preserves the full bug state.
async def _local_review_bug(state: BugState) -> BugState:
    return await local_review_changes(state)  # type: ignore[return-value]


async def _answer_question_bug(state: BugState) -> BugState:
    """Run shared Q&A without filtering bug-specific artifact fields."""
    return await answer_question(state)  # type: ignore[arg-type, return-value]


def route_entry(state: BugState) -> str:
    """Route workflow based on current progress for resume/retry.

    New bugs start at triage_check. In-flight tickets with a saved current_node
    resume at the appropriate point. The old rca_approval_gate value maps to
    rca_option_gate for backward compatibility.

    Args:
        state: Current workflow state.

    Returns:
        Next node name based on current progress.
    """
    current_node = state.get("current_node", "")

    if current_node and current_node not in ("entry", "route_entry", "__end__", "", "start"):
        logger.info(f"Resuming bug workflow at node: {current_node}")

        # Shared nodes: same resume mapping across all workflow types
        shared = resolve_shared_resume_node(current_node)
        if shared is not None:
            if shared is END:
                logger.info(f"Workflow at terminal state '{current_node}', returning END")
            return shared

        # Bug-specific resume mapping
        if current_node == "triage_check":
            return "triage_check"
        elif current_node == "triage_gate":
            return "triage_gate"
        elif current_node == "analyze_bug":
            return "analyze_bug"
        elif current_node == "regenerate_rca":
            return "regenerate_rca"
        elif current_node == "reflect_rca":
            return "reflect_rca"
        elif current_node in ("rca_option_gate", "rca_approval_gate"):
            return "rca_option_gate"
        elif current_node == "plan_bug_fix":
            return "plan_bug_fix"
        elif current_node == "plan_approval_gate":
            return "plan_approval_gate"
        elif current_node == "regenerate_plan":
            return "regenerate_plan"
        elif current_node == "decompose_plan":
            return "decompose_plan"
        elif current_node == "post_merge_summary":
            return "post_merge_summary"
        elif current_node in (
            "complete_tasks",
            "aggregate_epic_status",
            "aggregate_feature_status",
        ):
            return END
        elif current_node == "setup_workspace":
            return "setup_workspace"
        elif current_node == "implement_work":
            return "implement_work"
        elif current_node == "create_pr":
            return "create_pr"
        elif current_node == "teardown_workspace":
            return "teardown_workspace"
        elif current_node == "ai_review":
            return "human_review_gate"
        elif current_node == "escalate_blocked":
            return "escalate_blocked"
        else:
            logger.warning(f"Unrecognized current_node '{current_node}', restarting from triage")

    # New bugs and unrecognized states start at triage
    return "triage_check"


def _route_after_triage_check(state: BugState) -> str:
    """Route after triage_check based on what triage_check set as current_node."""
    node = state.get("current_node", "triage_gate")
    if node in ("triage_check", "analyze_bug", "triage_gate", "escalate_blocked"):
        return node
    return "triage_gate"


def _route_after_analyze_bug(state: BugState) -> str:
    """Route after analyze_bug: proceed to reflect_rca on success, or terminate on failure.

    analyze_bug sets current_node to reflect what happened:
    - "reflect_rca"      → success, proceed within same invocation
    - "escalate_blocked" → too many failures, escalate
    - "analyze_bug"      → container failed, terminate this invocation so the next
                           queue event or forge:retry triggers a fresh retry via route_entry

    Args:
        state: Current bug workflow state.

    Returns:
        Next node name or END.
    """
    current_node = state.get("current_node", "reflect_rca")
    if current_node == "reflect_rca":
        return "reflect_rca"
    if current_node == "escalate_blocked":
        return "escalate_blocked"
    # analyze_bug failed and wants to retry — terminate this invocation
    return END


def _route_after_reflect_rca(state: BugState) -> str:
    """Route after reflect_rca based on reflection loop state.

    Checks for failure state first (current_node set by reflect_rca's error handler),
    then applies the standard reflection loop logic.

    Returns "analyze_bug" if reflection_count < 3 and reflection_critique is non-empty.
    Returns "rca_option_gate" if reflection_count >= 3 or reflection_critique is absent.

    Args:
        state: Current bug workflow state.

    Returns:
        Next node name or END.
    """
    current_node = state.get("current_node", "rca_option_gate")

    # Respect failure state set by reflect_rca's error handler
    if current_node == "escalate_blocked":
        return "escalate_blocked"
    if current_node == "reflect_rca":
        # Container failed, wants to retry — terminate this invocation
        return END

    # Normal reflection loop logic
    reflection_count = state.get("reflection_count", 0)
    reflection_critique = state.get("reflection_critique") or ""

    if reflection_count >= _MAX_REFLECTION_COUNT:
        return "rca_option_gate"

    if reflection_critique.strip():
        return "analyze_bug"

    return "rca_option_gate"


def _route_human_review_bug(state: BugState) -> str:
    """Route after human_review_gate for bug workflow.

    Intercepts the merge path: if pr_merged is True, routes to post_merge_summary
    instead of END. All other routing (paused/implement_review) passes through.

    Note: route_human_review has a fallthrough `return "complete_tasks"` for non-merged,
    non-paused, non-revision states. We do NOT intercept that case — only an explicit
    pr_merged=True triggers post_merge_summary routing.

    Args:
        state: Current bug workflow state.

    Returns:
        Next node name or END.
    """
    if state.get("pr_merged"):
        return "post_merge_summary"

    return route_human_review(state)


def _route_after_answer_bug(state: BugState) -> str:
    """Route back to the correct gate after answering a question.

    Reads current_node from state to decide which gate to return to.
    Handles triage_gate, rca_option_gate, plan_approval_gate.
    Falls back to rca_option_gate for unknown values.

    Args:
        state: Current bug workflow state.

    Returns:
        Gate node name.
    """
    current_node = state.get("current_node", "")
    if current_node in ("triage_gate", "rca_option_gate", "plan_approval_gate"):
        return current_node
    return "rca_option_gate"


def _route_after_plan_bug_fix(state: BugState) -> str:
    """Route after initial bug-fix planning without approving failed plans."""
    current_node = state.get("current_node", "plan_approval_gate")
    if current_node == "plan_bug_fix" and state.get("last_error"):
        if state.get("retry_count", 0) >= _MAX_PLAN_RETRIES:
            return "escalate_blocked"
        return "plan_bug_fix"
    if current_node in ("plan_approval_gate", "escalate_blocked"):
        return current_node
    logger.error(f"Bug plan generation returned unexpected node {current_node!r}")
    return END


def _route_after_regenerate_plan(state: BugState) -> str:
    """Route after plan regeneration without approving failed revisions."""
    current_node = state.get("current_node", "plan_approval_gate")
    if current_node == "regenerate_plan" and state.get("last_error"):
        if state.get("retry_count", 0) >= _MAX_PLAN_RETRIES:
            return "escalate_blocked"
        return "regenerate_plan"
    if current_node in ("plan_approval_gate", "escalate_blocked"):
        return current_node
    logger.error(f"Bug plan regeneration returned unexpected node {current_node!r}")
    return END


def _route_after_decompose_plan(state: BugState) -> str:
    """Route after decomposition while preserving the failed node for retry."""
    current_node = state.get("current_node", "setup_workspace")
    if current_node in ("setup_workspace", "escalate_blocked"):
        return current_node
    if current_node == "decompose_plan" and state.get("last_error"):
        return "escalate_blocked"
    logger.error(f"Bug plan decomposition returned unexpected node {current_node!r}")
    return END


def _route_after_local_review(state: BugState) -> str:
    """Route after local_review considering qualitative verdict and retry count."""
    from forge.workflow.nodes.local_reviewer import _QUALITATIVE_CAP, MAX_REVIEW_ATTEMPTS

    verdict = state.get("local_review_verdict")
    retry_count = state.get("qualitative_retry_count", 0)
    current_node = state.get("current_node", "update_documentation")

    if current_node == "escalate_blocked":
        return "escalate_blocked"
    if state.get("last_error"):
        return current_node

    if verdict == "adequate" or retry_count >= _QUALITATIVE_CAP:
        return "update_documentation"
    if verdict in ("tests_incomplete", "symptom_only"):
        return "implement_work"
    # Fallback: mechanical review uses current_node, but cap at MAX_REVIEW_ATTEMPTS
    # to prevent infinite loops if current_node is "local_review".
    if state.get("local_review_attempts", 0) >= MAX_REVIEW_ATTEMPTS:
        return "update_documentation"
    return current_node


def _route_after_workspace_setup(
    state: BugState,
) -> Literal["implement_work", "escalate_blocked"]:
    """Route based on workspace setup success."""
    workspace_path = state.get("workspace_path")
    last_error = state.get("last_error")

    if workspace_path and not last_error:
        return "implement_work"

    logger.error(f"Workspace setup failed: {last_error}")
    return "escalate_blocked"


def _route_after_implementation(
    state: BugState,
) -> Literal["local_review", "implement_work", "escalate_blocked"]:
    """Route based on bug fix implementation status.

    Uses last_error as the failure signal. Success is indicated by last_error=None.
    """
    retry_count = state.get("retry_count", 0)
    max_retries = 3
    last_error = state.get("last_error")

    if last_error and state.get("persistence_retry_count", 0) >= 3:
        logger.error(f"Git persistence retry limit exceeded: {last_error}")
        return "escalate_blocked"

    if last_error:
        if retry_count >= max_retries:
            logger.error(f"Implementation retry limit ({max_retries}) exceeded: {last_error}")
            return "escalate_blocked"
        # Transient failure within retry budget — loop back so the same node retries
        return "implement_work"

    # No error → implementation succeeded
    return "local_review"


def build_bug_graph() -> StateGraph:
    """Build the governed graph from its versioned process definition."""
    from forge.workflow.declarative.builtins import builtin_bug_definition
    from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler

    return DeclarativeWorkflowCompiler(builtin_bug_definition()).build_graph()
