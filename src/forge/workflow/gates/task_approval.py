"""Task approval gate for human-in-the-loop review before implementation.

The task approval workflow uses labels:
- forge:task-pending  - Tasks awaiting approval before implementation
- forge:task-approved - Tasks approved (triggers implementation)

To approve: Change label from forge:task-pending to forge:task-approved
To request revision: Add a comment starting with ! (keeps forge:task-pending)
"""

import logging

from langgraph.graph import END

from forge.api.routes.metrics import record_approval, record_revision_requested
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.projections.approval import project_approval
from forge.workflow.reducers.approval import reduce_approval_gate
from forge.workflow.stations.approval import ApprovalDisposition, run_approval_station
from forge.workflow.utils import update_state_timestamp

logger = logging.getLogger(__name__)


def task_approval_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for human to review generated Tasks before implementation.

    This gate pauses the workflow after task generation, allowing humans to:
    - Review the generated tasks for accuracy and completeness
    - Modify tasks manually in Jira if needed
    - Approve when ready for AI implementation

    The workflow resumes when:
    - Label changes to forge:task-approved -> proceed to implementation
    - Comment starting with ! -> regenerate tasks

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True, or error state if no tasks.
    """
    ticket_key = state["ticket_key"]
    task_keys = state.get("task_keys", [])
    task_count = len(task_keys)

    request = project_approval(state, "task", item_count=task_count)
    outcome = run_approval_station(request)
    updates = reduce_approval_gate(state, request, outcome, "task_approval_gate", "generate_tasks")
    logger.info(
        f"Task approval gate: pausing workflow for {ticket_key} "
        f"({task_count} Tasks pending implementation approval)"
    )

    return update_state_timestamp({**state, **updates})


def route_task_approval(state: WorkflowState) -> str:
    """Route based on task approval status.

    Routing logic:
    - Question (Q&A mode) -> answer_question
    - YOLO mode enabled -> auto-approve without human input
    - ! comment on specific Task ticket -> update_single_task
    - ! comment on Feature ticket -> regenerate_all_tasks
    - Label changed to approved -> task_router
    - Still paused -> END (wait for webhook)

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    ticket_key = state["ticket_key"]

    outcome = run_approval_station(
        project_approval(state, "task", item_count=len(state.get("task_keys") or []))
    )
    assert outcome.output is not None
    disposition = outcome.output.disposition
    if disposition is ApprovalDisposition.QUESTION:
        logger.info(f"Q&A mode: routing to answer_question for {ticket_key}")
        return "answer_question"

    # YOLO mode: auto-approve without human input
    if disposition is ApprovalDisposition.APPROVED:
        logger.info(f"YOLO mode: auto-approving tasks for {ticket_key}")
        record_approval("task")
        return "task_router"

    # Check if revision requested (! feedback comment added)
    if disposition is ApprovalDisposition.REVISION:
        feedback = state.get("feedback_comment", "")
        current_task = state.get("current_task_key")
        current_epic = state.get("current_epic_key")

        if outcome.output.revision_scope == "task":
            # Single Task update - comment was on a specific Task
            logger.info(f"Single Task revision requested for {current_task}")
            record_revision_requested("task")
            return "update_single_task"
        elif outcome.output.revision_scope == "epic":
            # Epic-level regeneration - comment was on a specific Epic
            logger.info(f"Epic Task regeneration requested for {current_epic} on {ticket_key}")
            record_revision_requested("task")
            return "regenerate_epic_tasks"
        else:
            # Feature-level regeneration - comment was on Feature
            logger.info(f"Full Task regeneration requested for {ticket_key}: {feedback[:100]}...")
            record_revision_requested("task")
            return "regenerate_all_tasks"

    # Check if still paused - END and wait for approval webhook
    if disposition is ApprovalDisposition.WAITING:
        logger.info(
            f"Task approval gate: workflow paused for {ticket_key}, "
            "waiting for forge:task-approved label"
        )
        return END

    return END
