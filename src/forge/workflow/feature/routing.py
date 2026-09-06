"""Feature workflow graph construction.

This module builds the LangGraph StateGraph for the Feature workflow.
"""

import logging
from typing import Literal

from langgraph.graph import END, StateGraph

from forge.workflow.feature.state import FeatureState
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
        elif current_node == "implement_work":
            return "implement_work"
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
) -> Literal["implement_work", "escalate_blocked"]:
    """Route based on workspace setup success."""
    workspace_path = state.get("workspace_path")
    last_error = state.get("last_error")

    if workspace_path and not last_error:
        return "implement_work"

    logger.error(f"Workspace setup failed: {last_error}")
    return "escalate_blocked"


def _route_implementation(
    state: FeatureState,
) -> Literal["implement_work", "local_review", "escalate_blocked"]:
    """Route based on task implementation status.

    Checks for:
    - All tasks completed -> local_review (pre-PR code review)
    - Retry limit exceeded -> escalate_blocked
    - Tasks remaining -> implement_work
    """
    # Check retry limit to prevent infinite loops
    retry_count = state.get("retry_count", 0)
    max_retries = 3  # Max retries per task
    last_error = state.get("last_error")

    if last_error and state.get("persistence_retry_count", 0) >= 3:
        logger.error(f"Git persistence retry limit exceeded: {last_error}")
        return "escalate_blocked"

    if last_error and retry_count >= max_retries:
        logger.error(f"Implementation retry limit ({max_retries}) exceeded: {last_error}")
        return "escalate_blocked"

    if last_error:
        return "implement_work"

    current_repo = state.get("current_repo", "")
    repo_tasks = state.get("tasks_by_repo", {}).get(current_repo, [])
    implemented = state.get("implemented_tasks", [])

    # Check if all tasks for this repo are done
    remaining = [t for t in repo_tasks if t not in implemented]
    if not remaining:
        return "local_review"
    return "implement_work"


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
    """Build the governed graph from its versioned process definition."""
    from forge.workflow.declarative.builtins import builtin_feature_definition
    from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler

    return DeclarativeWorkflowCompiler(builtin_feature_definition()).build_graph()
