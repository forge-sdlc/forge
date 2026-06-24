"""Bug workflow graph construction.

This module builds the LangGraph StateGraph for the Bug workflow.
"""

import logging
from typing import Literal

from langgraph.graph import END, StateGraph

from forge.workflow.bug.state import BugState
from forge.workflow.nodes.ci_evaluator import (
    attempt_ci_fix,
    escalate_to_blocked,
    evaluate_ci_status,
    wait_for_ci_gate,
)
from forge.workflow.nodes.docs_updater import update_documentation
from forge.workflow.nodes.human_review import (
    human_review_gate,
    route_human_review,
)
from forge.workflow.nodes.implement_review import (
    implement_review,
    review_response_gate,
    route_review_response,
)
from forge.workflow.nodes.implementation import implement_task
from forge.workflow.nodes.local_reviewer import local_review_changes
from forge.workflow.nodes.plan_bug_fix import (
    _MAX_PLAN_RETRIES,
    decompose_plan,
    plan_approval_gate,
    plan_bug_fix,
    regenerate_plan,
    route_plan_approval,
)
from forge.workflow.nodes.post_merge_summary import post_merge_summary
from forge.workflow.nodes.pr_creation import create_pull_request, teardown_and_route
from forge.workflow.nodes.qa_handler import answer_question
from forge.workflow.nodes.rca_analysis import analyze_bug, reflect_rca
from forge.workflow.nodes.rca_option_gate import (
    rca_option_gate,
    regenerate_rca,
    route_rca_option,
)
from forge.workflow.nodes.rebase import rebase_pr
from forge.workflow.nodes.triage import route_triage_gate, triage_check, triage_gate
from forge.workflow.nodes.workspace_setup import setup_workspace
from forge.workflow.utils import resolve_shared_resume_node

logger = logging.getLogger(__name__)

_MAX_REFLECTION_COUNT = 3

# LangGraph filters state channels based on each node function's type annotation.
# implement_task and local_review_changes are typed as FeatureState, which lacks
# bug-specific fields like qualitative_retry_count. These wrappers are typed as
# BugState so LangGraph passes and records the full bug state for these nodes.


async def _implement_task_bug(state: BugState) -> BugState:
    return await implement_task(state)  # type: ignore[return-value]


async def _local_review_bug(state: BugState) -> BugState:
    return await local_review_changes(state)  # type: ignore[return-value]


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
        elif current_node == "implement_bug_fix":
            return "implement_bug_fix"
        elif current_node == "create_pr":
            return "create_pr"
        elif current_node == "teardown_workspace":
            return "teardown_workspace"
        elif current_node == "wait_for_ci_gate":
            return "wait_for_ci_gate"
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
    if node in ("analyze_bug", "triage_gate", "escalate_blocked"):
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

    if verdict == "adequate" or retry_count >= _QUALITATIVE_CAP:
        return "update_documentation"
    if verdict in ("tests_incomplete", "symptom_only"):
        return "implement_bug_fix"
    # Fallback: mechanical review uses current_node, but cap at MAX_REVIEW_ATTEMPTS
    # to prevent infinite loops if current_node is "local_review".
    if state.get("local_review_attempts", 0) >= MAX_REVIEW_ATTEMPTS:
        return "update_documentation"
    return state.get("current_node", "update_documentation")


def _route_after_workspace_setup(
    state: BugState,
) -> Literal["implement_bug_fix", "escalate_blocked"]:
    """Route based on workspace setup success."""
    workspace_path = state.get("workspace_path")
    last_error = state.get("last_error")

    if workspace_path and not last_error:
        return "implement_bug_fix"

    logger.error(f"Workspace setup failed: {last_error}")
    return "escalate_blocked"


def _route_after_implementation(
    state: BugState,
) -> Literal["local_review", "implement_bug_fix", "escalate_blocked"]:
    """Route based on bug fix implementation status.

    Uses last_error as the failure signal — implement_task (ContainerRunner)
    does not set bug_fix_implemented; success is indicated by last_error=None.
    """
    retry_count = state.get("retry_count", 0)
    max_retries = 3
    last_error = state.get("last_error")

    if last_error:
        if retry_count >= max_retries:
            logger.error(f"Implementation retry limit ({max_retries}) exceeded: {last_error}")
            return "escalate_blocked"
        # Transient failure within retry budget — loop back so the same node retries
        return "implement_bug_fix"

    # No error → implementation succeeded
    return "local_review"


def _route_after_pr_creation(
    state: BugState,
) -> Literal["teardown_workspace", "escalate_blocked"]:
    """Route after PR creation attempt."""
    last_error = state.get("last_error")
    pr_urls = state.get("pr_urls", [])

    if last_error and not pr_urls:
        return "escalate_blocked"

    return "teardown_workspace"


def _route_after_teardown(state: BugState) -> str:
    """Route after workspace teardown.

    If more repos remain in repos_to_process, loop back to setup_workspace.
    Otherwise proceed to wait_for_ci_gate (matching feature workflow pattern)
    so that Jira labels are updated and the workflow pauses until CI fires.
    """
    repos_to_process = state.get("repos_to_process", [])
    repos_completed = state.get("repos_completed", [])
    remaining = [r for r in repos_to_process if r not in repos_completed]
    if remaining:
        return "setup_workspace"
    return "wait_for_ci_gate"


def _route_ci_evaluation(
    state: BugState,
) -> Literal["human_review_gate", "attempt_ci_fix", "escalate_blocked", "__end__"]:
    """Route based on CI evaluation results."""
    ci_status = state.get("ci_status", "")
    routes = {
        "passed": "human_review_gate",
        "fixing": "attempt_ci_fix",
        "pending": "__end__",
    }
    return routes.get(ci_status, "escalate_blocked")


def build_bug_graph() -> StateGraph:
    """Create the Bug workflow graph.

    Implements the new five-stage pipeline:
    1. Triage: triage_check → triage_gate (pause) or → analyze_bug
    2. Analysis + reflection: analyze_bug ↔ reflect_rca → rca_option_gate (pause)
    3. Planning: plan_bug_fix → plan_approval_gate (pause) → decompose_plan → END
    4. (Spawned tasks are handled by the task workflow)
    5. Post-merge: human_review_gate → post_merge_summary → END

    Backward-compat implementation/CI/review nodes are preserved for in-flight tickets.

    Returns:
        Configured StateGraph ready for compilation.
    """
    graph = StateGraph(BugState)

    # Entry routing
    graph.add_node("route_entry", lambda state: state)

    # ── Triage ──
    graph.add_node("triage_check", triage_check)
    graph.add_node("triage_gate", triage_gate)

    # ── Analysis + reflection ──
    graph.add_node("analyze_bug", analyze_bug)
    graph.add_node("reflect_rca", reflect_rca)

    # ── RCA option gate ──
    graph.add_node("rca_option_gate", rca_option_gate)
    graph.add_node("regenerate_rca", regenerate_rca)

    # ── Planning ──
    graph.add_node("plan_bug_fix", plan_bug_fix)
    graph.add_node("plan_approval_gate", plan_approval_gate)
    graph.add_node("regenerate_plan", regenerate_plan)
    graph.add_node("decompose_plan", decompose_plan)

    # ── Post-merge ──
    graph.add_node("post_merge_summary", post_merge_summary)

    # ── Q&A ──
    graph.add_node("answer_question", answer_question)

    # ── Implementation stage ──
    graph.add_node("setup_workspace", setup_workspace)
    # Use the container-based implement_task (same as feature workflow) so the
    # fix runs inside an isolated Podman container with full tool access.
    # implement_bug_fix (ForgeAgent-based) is kept only for route_entry backward compat.
    graph.add_node("implement_bug_fix", _implement_task_bug)
    graph.add_node("local_review", _local_review_bug)
    graph.add_node("update_documentation", update_documentation)
    graph.add_node("create_pr", create_pull_request)
    graph.add_node("teardown_workspace", teardown_and_route)

    # ── CI/CD ──
    graph.add_node("wait_for_ci_gate", wait_for_ci_gate)
    graph.add_node("ci_evaluator", evaluate_ci_status)
    graph.add_node("attempt_ci_fix", attempt_ci_fix)
    graph.add_node("escalate_blocked", escalate_to_blocked)

    # ── Review ──
    graph.add_node("human_review_gate", human_review_gate)
    graph.add_node("implement_review", implement_review)
    graph.add_node("review_response_gate", review_response_gate)

    # ── Set entry point ──
    graph.set_entry_point("route_entry")

    # ── Entry routing edges ──
    graph.add_conditional_edges(
        "route_entry",
        route_entry,
        {
            "triage_check": "triage_check",
            "triage_gate": "triage_gate",
            "analyze_bug": "analyze_bug",
            "reflect_rca": "reflect_rca",
            "rca_option_gate": "rca_option_gate",
            "plan_bug_fix": "plan_bug_fix",
            "plan_approval_gate": "plan_approval_gate",
            "regenerate_plan": "regenerate_plan",
            "decompose_plan": "decompose_plan",
            "post_merge_summary": "post_merge_summary",
            "setup_workspace": "setup_workspace",
            "implement_bug_fix": "implement_bug_fix",
            "local_review": "local_review",
            "update_documentation": "update_documentation",
            "create_pr": "create_pr",
            "teardown_workspace": "teardown_workspace",
            "wait_for_ci_gate": "wait_for_ci_gate",
            "ci_evaluator": "ci_evaluator",
            "human_review_gate": "human_review_gate",
            "implement_review": "implement_review",
            "review_response_gate": "review_response_gate",
            "escalate_blocked": "escalate_blocked",
            "rebase_pr": "rebase_pr",
            END: END,
        },
    )

    # ── Triage flow ──
    graph.add_conditional_edges(
        "triage_check",
        _route_after_triage_check,
        {
            "triage_gate": "triage_gate",
            "analyze_bug": "analyze_bug",
            "escalate_blocked": "escalate_blocked",
        },
    )
    # triage_gate pauses; on resume route_entry routes back to triage_gate
    # which uses route_triage_gate to decide: END (still waiting) or triage_check (re-evaluate)
    graph.add_conditional_edges(
        "triage_gate",
        route_triage_gate,
        {
            END: END,
            "triage_check": "triage_check",
        },
    )

    # ── Analysis + reflection loop ──
    # Conditional: analyze_bug failure terminates the invocation (END) so the next
    # queue event retries via route_entry; success proceeds to reflect_rca.
    graph.add_conditional_edges(
        "analyze_bug",
        _route_after_analyze_bug,
        {
            "reflect_rca": "reflect_rca",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "reflect_rca",
        _route_after_reflect_rca,
        {
            "analyze_bug": "analyze_bug",
            "rca_option_gate": "rca_option_gate",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )

    # ── RCA option gate ──
    graph.add_conditional_edges(
        "rca_option_gate",
        route_rca_option,
        {
            "plan_bug_fix": "plan_bug_fix",
            "regenerate_rca": "regenerate_rca",
            "answer_question": "answer_question",
            END: END,
        },
    )
    graph.add_edge("regenerate_rca", "analyze_bug")

    # ── Planning ──
    graph.add_conditional_edges(
        "plan_bug_fix",
        _route_after_plan_bug_fix,
        {
            "plan_approval_gate": "plan_approval_gate",
            "plan_bug_fix": "plan_bug_fix",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "plan_approval_gate",
        route_plan_approval,
        {
            "decompose_plan": "decompose_plan",
            "regenerate_plan": "regenerate_plan",
            "answer_question": "answer_question",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "regenerate_plan",
        _route_after_regenerate_plan,
        {
            "plan_approval_gate": "plan_approval_gate",
            "regenerate_plan": "regenerate_plan",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )
    # decompose_plan sets current_node in state; route accordingly
    graph.add_conditional_edges(
        "decompose_plan",
        _route_after_decompose_plan,
        {
            "setup_workspace": "setup_workspace",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )

    # ── Q&A routing (multi-gate return) ──
    graph.add_conditional_edges(
        "answer_question",
        _route_after_answer_bug,
        {
            "triage_gate": "triage_gate",
            "rca_option_gate": "rca_option_gate",
            "plan_approval_gate": "plan_approval_gate",
        },
    )

    # ── Backward-compat: implementation flow ──
    graph.add_conditional_edges(
        "setup_workspace",
        _route_after_workspace_setup,
        {
            "implement_bug_fix": "implement_bug_fix",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "implement_bug_fix",
        _route_after_implementation,
        {
            "local_review": "local_review",
            "implement_bug_fix": "implement_bug_fix",  # retry loop
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "local_review",
        _route_after_local_review,
        {
            "local_review": "local_review",
            "update_documentation": "update_documentation",
            "create_pr": "create_pr",
            "implement_bug_fix": "implement_bug_fix",
        },
    )
    graph.add_edge("update_documentation", "create_pr")
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
            "setup_workspace": "setup_workspace",  # multi-repo loop-back
            "wait_for_ci_gate": "wait_for_ci_gate",
        },
    )

    # ── CI/CD flow ──
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
    graph.add_edge("escalate_blocked", END)

    # ── Review flow (merge path → post_merge_summary) ──
    # "complete_tasks" is the feature-workflow merge return from route_human_review;
    # in the bug graph it should never be reached (pr_merged check intercepts first),
    # but map it to post_merge_summary defensively.
    graph.add_conditional_edges(
        "human_review_gate",
        _route_human_review_bug,
        {
            "implement_review": "implement_review",
            "post_merge_summary": "post_merge_summary",
            "complete_tasks": "post_merge_summary",
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

    # ── Rebase (merge conflict resolution, triggered by /forge rebase) ──
    graph.add_node("rebase_pr", rebase_pr)
    graph.add_conditional_edges(
        "rebase_pr",
        lambda s: s.get("current_node", END),
        {
            "triage_gate": "triage_gate",
            "rca_option_gate": "rca_option_gate",
            "plan_approval_gate": "plan_approval_gate",
            "setup_workspace": "setup_workspace",
            "implement_bug_fix": "implement_bug_fix",
            "local_review": "local_review",
            "update_documentation": "update_documentation",
            "create_pr": "create_pr",
            "teardown_workspace": "teardown_workspace",
            "wait_for_ci_gate": "wait_for_ci_gate",
            "ci_evaluator": "ci_evaluator",
            "attempt_ci_fix": "ci_evaluator",
            "human_review_gate": "human_review_gate",
            "implement_review": "implement_review",
            "review_response_gate": "review_response_gate",
            "post_merge_summary": "post_merge_summary",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )

    # ── Post-merge terminal ──
    graph.add_edge("post_merge_summary", END)

    return graph
