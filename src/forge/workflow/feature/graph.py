"""Feature workflow graph construction.

This module builds the LangGraph StateGraph for the Feature workflow.
"""

import logging
from typing import Literal

from langgraph.graph import END, StateGraph

from forge.workflow.feature.state import FeatureState
from forge.workflow.gates import (
    plan_approval_gate,
    prd_approval_gate,
    route_plan_approval,
    route_prd_approval,
    route_spec_approval,
    route_task_approval,
    spec_approval_gate,
    task_approval_gate,
)
from forge.workflow.nodes import (
    aggregate_epic_status,
    aggregate_feature_status,
    attempt_ci_fix,
    complete_tasks,
    create_pull_request,
    decompose_epics,
    escalate_to_blocked,
    evaluate_ci_status,
    generate_prd,
    generate_spec,
    generate_tasks,
    human_review_gate,
    implement_task,
    local_review_changes,
    regenerate_all_epics,
    regenerate_prd_with_feedback,
    regenerate_spec_with_feedback,
    route_human_review,
    route_tasks_by_repo,
    route_tasks_parallel,
    setup_workspace,
    teardown_and_route,
    update_documentation,
    update_single_epic,
    wait_for_ci_gate,
)
from forge.workflow.nodes.implement_review import (
    implement_review,
    review_response_gate,
    route_review_response,
)
from forge.workflow.nodes.qa_handler import answer_question
from forge.workflow.nodes.rebase import rebase_pr
from forge.workflow.nodes.task_generation import (
    regenerate_all_tasks,
    regenerate_epic_tasks,
    update_single_task,
)
from forge.workflow.utils import resolve_shared_resume_node

logger = logging.getLogger(__name__)


def route_by_ticket_type(state: FeatureState) -> str:
    """Route workflow based on ticket type or resume from current node.

    If the workflow is being resumed (current_node is set), route to the
    appropriate node based on where the workflow was. This enables retry
    from error states without going backwards.

    Args:
        state: Current workflow state.

    Returns:
        Next node name based on ticket type or current progress.
    """
    current_node = state.get("current_node", "")

    # If we have a current_node from a previous run, route based on progress
    # This enables retry from error states without going backwards
    if current_node and current_node not in ("entry", "__end__", ""):
        logger.info(f"Resuming workflow at node: {current_node}")

        # Shared nodes: same resume mapping across all workflow types
        shared = resolve_shared_resume_node(current_node)
        if shared is not None:
            if shared is END:
                logger.info(f"Workflow at terminal state '{current_node}', returning END")
            return shared

        # Feature-specific resume mapping
        if current_node == "generate_prd":
            return "generate_prd"
        elif current_node == "regenerate_prd":
            return "regenerate_prd"
        elif current_node == "prd_approval_gate":
            return "prd_approval_gate"
        elif current_node == "generate_spec":
            return "generate_spec"
        elif current_node == "regenerate_spec":
            return "regenerate_spec"
        elif current_node == "spec_approval_gate":
            return "spec_approval_gate"
        elif current_node == "decompose_epics":
            return "decompose_epics"
        elif current_node == "regenerate_all_epics":
            return "regenerate_all_epics"
        elif current_node == "update_single_epic":
            return "update_single_epic"
        elif current_node == "plan_approval_gate":
            return "plan_approval_gate"
        elif current_node == "generate_tasks":
            return "generate_tasks"
        elif current_node == "regenerate_all_tasks":
            return "regenerate_all_tasks"
        elif current_node == "update_single_task":
            return "update_single_task"
        elif current_node == "regenerate_epic_tasks":
            return "regenerate_epic_tasks"
        elif current_node == "task_approval_gate":
            return "task_approval_gate"
        elif current_node == "wait_for_ci_gate":
            return "wait_for_ci_gate"
        elif current_node in ("implement_task", "implementation", "implement_bug_fix"):
            return "implement_task"
        elif current_node == "setup_workspace":
            return "setup_workspace"
        elif current_node == "create_pr":
            return "create_pr"
        elif current_node == "teardown_workspace":
            return "teardown_workspace"
        elif current_node == "blocked":
            return "create_pr"
        elif current_node in (
            "complete_tasks",
            "aggregate_epic_status",
            "aggregate_feature_status",
        ):
            return current_node
        elif current_node in (
            "task_router",
            "escalate_blocked",
        ):
            return "task_router"
        else:
            logger.warning(f"Unrecognized current_node '{current_node}', using ticket type routing")

    # Start at PRD generation for Feature/Story tickets
    return "generate_prd"


def _route_after_generation(state: FeatureState) -> str:
    """Route based on PRD generation success.

    If generation failed (has error and no PRD content), don't advance to approval gate.

    Returns:
        "prd_approval_gate" on success, END on failure.
    """
    last_error = state.get("last_error")
    prd_content = state.get("prd_content", "")

    if last_error and not prd_content:
        logger.error(f"PRD generation failed, workflow paused: {last_error}")
        return END

    return "prd_approval_gate"


def _route_after_spec_generation(state: FeatureState) -> str:
    """Route based on spec generation success.

    If generation failed (has error and no spec content), don't advance to approval gate.

    Returns:
        "spec_approval_gate" on success, END on failure.
    """
    last_error = state.get("last_error")
    spec_content = state.get("spec_content", "")

    if last_error and not spec_content:
        logger.error(f"Spec generation failed, workflow paused: {last_error}")
        return END

    return "spec_approval_gate"


def _route_after_epic_decomposition(state: FeatureState) -> str:
    """Route based on epic decomposition success.

    If decomposition failed (has error and no epics), don't advance to approval gate.

    Returns:
        "plan_approval_gate" on success, END ("__end__") on failure.
    """
    last_error = state.get("last_error")
    epic_keys = state.get("epic_keys", [])

    if last_error and not epic_keys:
        logger.error(f"Epic decomposition failed, workflow paused: {last_error}")
        return END

    return "plan_approval_gate"


def _route_after_task_generation(state: FeatureState) -> str:
    """Route based on task generation success.

    If task generation failed (has error and no tasks), don't advance.

    Returns:
        "task_approval_gate" on success, END on failure.
    """
    last_error = state.get("last_error")
    task_keys = state.get("task_keys", [])

    if last_error and not task_keys:
        logger.error(f"Task generation failed, workflow paused: {last_error}")
        return END

    return "task_approval_gate"


def _route_after_epic_task_regeneration(state: FeatureState) -> str:
    """Route after regenerating tasks for a single Epic."""
    if state.get("last_error") and state.get("current_node") == "regenerate_epic_tasks":
        logger.error(f"Epic task regeneration failed, workflow paused: {state['last_error']}")
        return END

    return "task_approval_gate"


def _route_after_prd_regeneration(state: FeatureState) -> str:
    """Route after PRD regeneration, preserving failed regeneration checkpoints."""
    if state.get("current_node") == "regenerate_prd":
        logger.error(f"PRD regeneration failed, workflow paused: {state.get('last_error')}")
        return END
    return "prd_approval_gate"


def _route_after_spec_regeneration(state: FeatureState) -> str:
    """Route after spec regeneration, preserving failed regeneration checkpoints."""
    if state.get("current_node") == "regenerate_spec":
        logger.error(f"Spec regeneration failed, workflow paused: {state.get('last_error')}")
        return END
    return "spec_approval_gate"


def _route_after_epic_regeneration(state: FeatureState) -> str:
    """Route after full Epic regeneration without advancing failed decomposition."""
    if state.get("current_node") == "plan_approval_gate":
        return "plan_approval_gate"
    logger.error(
        f"Epic regeneration failed at {state.get('current_node')}: {state.get('last_error')}"
    )
    return END


def _route_after_single_epic_update(state: FeatureState) -> str:
    """Route after a single Epic update, preserving failed update checkpoints."""
    if state.get("current_node") == "plan_approval_gate":
        return "plan_approval_gate"
    logger.error(f"Epic update failed, workflow paused: {state.get('last_error')}")
    return END


def _route_after_task_regeneration(state: FeatureState) -> str:
    """Route after full Task regeneration without advancing failed generation."""
    if state.get("current_node") == "task_approval_gate":
        return "task_approval_gate"
    logger.error(
        f"Task regeneration failed at {state.get('current_node')}: {state.get('last_error')}"
    )
    return END


def _route_after_single_task_update(state: FeatureState) -> str:
    """Route after a single Task update, preserving failed update checkpoints."""
    if state.get("current_node") == "task_approval_gate":
        return "task_approval_gate"
    logger.error(f"Task update failed, workflow paused: {state.get('last_error')}")
    return END


def _route_after_workspace_setup(
    state: FeatureState,
) -> Literal["implement_task", "escalate_blocked"]:
    """Route based on workspace setup success."""
    workspace_path = state.get("workspace_path")
    last_error = state.get("last_error")

    if workspace_path and not last_error:
        return "implement_task"

    logger.error(f"Workspace setup failed: {last_error}")
    return "escalate_blocked"


def _route_implementation(
    state: FeatureState,
) -> Literal["implement_task", "local_review", "escalate_blocked"]:
    """Route based on task implementation status.

    Checks for:
    - All tasks completed -> local_review (pre-PR code review)
    - Retry limit exceeded -> escalate_blocked
    - Tasks remaining -> implement_task
    """
    # Check retry limit to prevent infinite loops
    retry_count = state.get("retry_count", 0)
    max_retries = 3  # Max retries per task
    last_error = state.get("last_error")

    if last_error and retry_count >= max_retries:
        logger.error(f"Implementation retry limit ({max_retries}) exceeded: {last_error}")
        return "escalate_blocked"

    current_repo = state.get("current_repo", "")
    repo_tasks = state.get("tasks_by_repo", {}).get(current_repo, [])
    implemented = state.get("implemented_tasks", [])

    # Check if all tasks for this repo are done
    remaining = [t for t in repo_tasks if t not in implemented]
    if not remaining:
        return "local_review"
    return "implement_task"


def _route_after_pr_creation(
    state: FeatureState,
) -> Literal["teardown_workspace", "escalate_blocked"]:
    """Route after PR creation attempt.

    On success: proceed to teardown workspace
    On failure: escalate to blocked status

    Args:
        state: Current workflow state.

    Returns:
        Next node name.
    """
    last_error = state.get("last_error")
    pr_urls = state.get("pr_urls", [])

    # If there's an error and no PR was created, escalate
    if last_error and not pr_urls:
        return "escalate_blocked"

    # Success or partial success - proceed to teardown
    return "teardown_workspace"


def _route_after_teardown(state: FeatureState) -> Literal["setup_workspace", "wait_for_ci_gate"]:
    """Route after workspace teardown."""
    repos_to_process = state.get("repos_to_process", [])
    repos_completed = state.get("repos_completed", [])

    remaining = [r for r in repos_to_process if r not in repos_completed]
    if remaining:
        return "setup_workspace"
    return "wait_for_ci_gate"


def _route_ci_evaluation(
    state: FeatureState,
) -> Literal["human_review_gate", "attempt_ci_fix", "escalate_blocked", "__end__"]:
    """Route based on CI evaluation results."""
    ci_status = state.get("ci_status", "")

    routes = {
        "passed": "human_review_gate",
        "fixing": "attempt_ci_fix",
        "pending": "__end__",  # Pause workflow until CI webhook
    }
    return routes.get(ci_status, "escalate_blocked")


def _route_after_answer(state: FeatureState) -> str:
    """Route back to the original gate after answering a question.

    The answer_question node preserves current_node as the gate to return to.
    """
    current_node = state.get("current_node", "")
    # current_node contains the gate we came from
    if current_node and "gate" in current_node:
        return current_node
    # Fallback to PRD gate
    return "prd_approval_gate"


def build_feature_graph() -> StateGraph:
    """Create the Feature workflow graph.

    The graph implements the following flow:
    1. Start -> Route by entry/resume state
    2. generate_prd -> prd_approval_gate (pause)
    3. On PRD approval: prd_approval_gate -> generate_spec
    4. On PRD rejection: prd_approval_gate -> regenerate_prd -> prd_approval_gate
    5. generate_spec -> spec_approval_gate (pause)
    6. On Spec approval: spec_approval_gate -> decompose_epics
    7. On Spec rejection: spec_approval_gate -> regenerate_spec -> spec_approval_gate
    8. decompose_epics -> plan_approval_gate (pause)
    9. On Plan approval: plan_approval_gate -> generate_tasks
    10. On Feature-level rejection: plan_approval_gate -> regenerate_all_epics
    11. On Epic-level rejection: plan_approval_gate -> update_single_epic
    12. generate_tasks -> task_approval_gate (pause)
    13. On Task approval: task_approval_gate -> task_router
    14. task_router -> setup_workspace (or parallel fan-out)
    15. setup_workspace -> implement_task
    16. implement_task (all tasks done) -> local_review
    17. local_review: reviews git diff vs main, fixes breaking issues in-place (up to 2 passes)
    18. local_review -> create_pr
    19. create_pr -> teardown_workspace
    20. teardown_workspace -> wait_for_ci_gate (pause) or next repo
    21. wait_for_ci_gate: resumes on GitHub CI webhook
    22. ci_evaluator: checks CI status, attempts autonomous fixes on failure (up to 5 retries)
    23. ci_evaluator (passed) -> human_review_gate
    24. human_review_gate -> complete_tasks
    25. complete_tasks -> aggregate_epic_status -> aggregate_feature_status -> END

    Returns:
        Configured StateGraph ready for compilation.
    """
    # Create graph with feature state schema
    graph = StateGraph(FeatureState)

    # Add entry point that routes by ticket type/resume state
    graph.add_node("route_entry", lambda state: state)

    # PRD Generation nodes (US1)
    graph.add_node("generate_prd", generate_prd)
    graph.add_node("prd_approval_gate", prd_approval_gate)
    graph.add_node("regenerate_prd", regenerate_prd_with_feedback)

    # Spec Generation nodes (US2)
    graph.add_node("generate_spec", generate_spec)
    graph.add_node("spec_approval_gate", spec_approval_gate)
    graph.add_node("regenerate_spec", regenerate_spec_with_feedback)

    # Epic Decomposition nodes (US3)
    graph.add_node("decompose_epics", decompose_epics)
    graph.add_node("plan_approval_gate", plan_approval_gate)
    graph.add_node("regenerate_all_epics", regenerate_all_epics)
    graph.add_node("update_single_epic", update_single_epic)

    # Task Generation nodes (US4)
    graph.add_node("generate_tasks", generate_tasks)
    graph.add_node("task_approval_gate", task_approval_gate)
    graph.add_node("regenerate_all_tasks", regenerate_all_tasks)
    graph.add_node("update_single_task", update_single_task)
    graph.add_node("regenerate_epic_tasks", regenerate_epic_tasks)

    # Execution nodes (US6)
    graph.add_node("task_router", route_tasks_by_repo)
    graph.add_node("setup_workspace", setup_workspace)
    graph.add_node("implement_task", implement_task)
    graph.add_node("create_pr", create_pull_request)
    graph.add_node("teardown_workspace", teardown_and_route)

    # Local code review node (pre-PR, fixes breaking issues in-place)
    graph.add_node("local_review", local_review_changes)

    # Documentation update node (pre-PR, updates stale docs)
    graph.add_node("update_documentation", update_documentation)

    # CI/CD Validation nodes (US7)
    graph.add_node("wait_for_ci_gate", wait_for_ci_gate)
    graph.add_node("ci_evaluator", evaluate_ci_status)
    graph.add_node("attempt_ci_fix", attempt_ci_fix)
    graph.add_node("escalate_blocked", escalate_to_blocked)

    # Human Review nodes (US9)
    graph.add_node("human_review_gate", human_review_gate)
    graph.add_node("implement_review", implement_review)
    graph.add_node("review_response_gate", review_response_gate)
    graph.add_node("complete_tasks", complete_tasks)
    graph.add_node("aggregate_epic_status", aggregate_epic_status)
    graph.add_node("aggregate_feature_status", aggregate_feature_status)

    # Q&A node
    graph.add_node("answer_question", answer_question)

    # Set entry point
    graph.set_entry_point("route_entry")

    # Route from entry based on resume state
    graph.add_conditional_edges(
        "route_entry",
        route_by_ticket_type,
        {
            # Initial routing
            "generate_prd": "generate_prd",
            # Resume routing for Feature workflow - planning stages
            "prd_approval_gate": "prd_approval_gate",
            "generate_spec": "generate_spec",
            "regenerate_prd": "regenerate_prd",
            "spec_approval_gate": "spec_approval_gate",
            "regenerate_spec": "regenerate_spec",
            "decompose_epics": "decompose_epics",
            "regenerate_all_epics": "regenerate_all_epics",
            "update_single_epic": "update_single_epic",
            "plan_approval_gate": "plan_approval_gate",
            "generate_tasks": "generate_tasks",
            "regenerate_all_tasks": "regenerate_all_tasks",
            "update_single_task": "update_single_task",
            "regenerate_epic_tasks": "regenerate_epic_tasks",
            "task_approval_gate": "task_approval_gate",
            # Resume routing for Feature workflow - execution stages
            "task_router": "task_router",
            "setup_workspace": "setup_workspace",
            "implement_task": "implement_task",
            "create_pr": "create_pr",
            "teardown_workspace": "teardown_workspace",
            # Resume routing for pre-PR and CI/review stages
            "local_review": "local_review",
            "update_documentation": "update_documentation",
            "wait_for_ci_gate": "wait_for_ci_gate",
            "ci_evaluator": "ci_evaluator",
            "human_review_gate": "human_review_gate",
            "implement_review": "implement_review",
            "review_response_gate": "review_response_gate",
            # Rebase (merge conflict resolution)
            "rebase_pr": "rebase_pr",
            # Terminal chain — retried individually on failure
            "complete_tasks": "complete_tasks",
            "aggregate_epic_status": "aggregate_epic_status",
            "aggregate_feature_status": "aggregate_feature_status",
            # True terminal state routes directly to END
            END: END,
        },
    )

    # PRD generation flow (US1)
    graph.add_conditional_edges(
        "generate_prd",
        _route_after_generation,
        {
            "prd_approval_gate": "prd_approval_gate",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "prd_approval_gate",
        route_prd_approval,
        {
            "generate_spec": "generate_spec",
            "regenerate_prd": "regenerate_prd",
            "answer_question": "answer_question",  # Q&A mode
            END: END,  # Pause workflow until next webhook
        },
    )
    graph.add_conditional_edges(
        "regenerate_prd",
        _route_after_prd_regeneration,
        {
            "prd_approval_gate": "prd_approval_gate",
            END: END,
        },
    )

    # Spec generation flow (US2)
    graph.add_conditional_edges(
        "generate_spec",
        _route_after_spec_generation,
        {
            "spec_approval_gate": "spec_approval_gate",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "spec_approval_gate",
        route_spec_approval,
        {
            "decompose_epics": "decompose_epics",
            "regenerate_spec": "regenerate_spec",
            "answer_question": "answer_question",  # Q&A mode
            END: END,  # Pause workflow until next webhook
        },
    )
    graph.add_conditional_edges(
        "regenerate_spec",
        _route_after_spec_regeneration,
        {
            "spec_approval_gate": "spec_approval_gate",
            END: END,
        },
    )

    # Epic decomposition flow (US3)
    graph.add_conditional_edges(
        "decompose_epics",
        _route_after_epic_decomposition,
        {
            "plan_approval_gate": "plan_approval_gate",
            END: END,  # Error state - don't advance
        },
    )
    graph.add_conditional_edges(
        "plan_approval_gate",
        route_plan_approval,
        {
            "generate_tasks": "generate_tasks",
            "regenerate_all_epics": "regenerate_all_epics",
            "update_single_epic": "update_single_epic",
            "answer_question": "answer_question",  # Q&A mode
            END: END,  # Pause workflow until next webhook
        },
    )
    graph.add_conditional_edges(
        "regenerate_all_epics",
        _route_after_epic_regeneration,
        {
            "plan_approval_gate": "plan_approval_gate",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "update_single_epic",
        _route_after_single_epic_update,
        {
            "plan_approval_gate": "plan_approval_gate",
            END: END,
        },
    )

    # Task generation flow (US4)
    graph.add_conditional_edges(
        "generate_tasks",
        _route_after_task_generation,
        {
            "task_approval_gate": "task_approval_gate",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "task_approval_gate",
        route_task_approval,
        {
            "task_router": "task_router",
            "regenerate_all_tasks": "regenerate_all_tasks",  # Feature-level rejection
            "regenerate_epic_tasks": "regenerate_epic_tasks",  # Epic-level rejection
            "update_single_task": "update_single_task",  # Task-level rejection
            "answer_question": "answer_question",  # Q&A mode
            END: END,  # Pause workflow until approval webhook
        },
    )
    graph.add_conditional_edges(
        "regenerate_all_tasks",
        _route_after_task_regeneration,
        {
            "task_approval_gate": "task_approval_gate",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "update_single_task",
        _route_after_single_task_update,
        {
            "task_approval_gate": "task_approval_gate",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "regenerate_epic_tasks",
        _route_after_epic_task_regeneration,
        {
            "task_approval_gate": "task_approval_gate",
            END: END,
        },
    )

    # Execution flow (US6) with parallel support (US10)
    # The routing function returns either "setup_workspace" or list[Send]
    graph.add_conditional_edges(
        "task_router",
        route_tasks_parallel,  # Returns Send objects for fan-out
    )
    graph.add_conditional_edges(
        "setup_workspace",
        _route_after_workspace_setup,
        {
            "implement_task": "implement_task",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "implement_task",
        _route_implementation,
        {
            "implement_task": "implement_task",
            "local_review": "local_review",
            "escalate_blocked": "escalate_blocked",
        },
    )
    graph.add_conditional_edges(
        "local_review",
        lambda s: s.get("current_node", "create_pr"),
        {"local_review": "local_review", "create_pr": "update_documentation"},
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
            "setup_workspace": "setup_workspace",
            "wait_for_ci_gate": "wait_for_ci_gate",
        },
    )

    # CI/CD flow (US7)
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
            END: END,  # Pause: CI still running, wait for next webhook
        },
    )
    graph.add_conditional_edges(
        "attempt_ci_fix",
        lambda s: s.get("current_node", "wait_for_ci_gate"),
        {
            "wait_for_ci_gate": "wait_for_ci_gate",
            "escalate_blocked": "escalate_blocked",
            "ci_evaluator": "ci_evaluator",
            # Exception handlers set current_node="attempt_ci_fix" so resume routing
            # can map it back to ci_evaluator. Escalate within the current invocation
            # so the graph doesn't crash on an unmapped key.
            "attempt_ci_fix": "escalate_blocked",
        },
    )
    graph.add_edge("escalate_blocked", END)

    # Human Review flow (US9)
    graph.add_conditional_edges(
        "human_review_gate",
        route_human_review,
        {
            "implement_review": "implement_review",
            "complete_tasks": "complete_tasks",
            END: END,  # Pause workflow until review webhook
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
    graph.add_edge("complete_tasks", "aggregate_epic_status")
    graph.add_edge("aggregate_epic_status", "aggregate_feature_status")
    graph.add_edge("aggregate_feature_status", END)

    # Q&A routing: answer_question returns to the gate it came from
    graph.add_conditional_edges(
        "answer_question",
        _route_after_answer,
        {
            "prd_approval_gate": "prd_approval_gate",
            "spec_approval_gate": "spec_approval_gate",
            "plan_approval_gate": "plan_approval_gate",
            "task_approval_gate": "task_approval_gate",
        },
    )

    # ── Rebase (merge conflict resolution, triggered by /forge rebase) ──
    graph.add_node("rebase_pr", rebase_pr)
    graph.add_conditional_edges(
        "rebase_pr",
        lambda s: s.get("current_node", END),
        {
            "prd_approval_gate": "prd_approval_gate",
            "spec_approval_gate": "spec_approval_gate",
            "plan_approval_gate": "plan_approval_gate",
            "task_approval_gate": "task_approval_gate",
            "task_router": "task_router",
            "setup_workspace": "setup_workspace",
            "implement_task": "implement_task",
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
            "complete_tasks": "complete_tasks",
            "aggregate_epic_status": "aggregate_epic_status",
            "aggregate_feature_status": "aggregate_feature_status",
            "escalate_blocked": "escalate_blocked",
            END: END,
        },
    )

    return graph
