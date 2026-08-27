"""Plan approval gate for human-in-the-loop review of Epic decomposition.

The plan approval workflow uses labels:
- forge:plan-pending  - Plan awaiting approval
- forge:plan-approved - Plan approved (triggers task generation)

To approve: Change label from forge:plan-pending to forge:plan-approved
To request revision: Add a comment starting with ! (keep forge:plan-pending)
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


def plan_approval_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for Tech Lead to review Epic decomposition and plans.

    This gate pauses the workflow until a human approves or rejects
    the generated Epics and their implementation plans. The workflow resumes when:
    - Label changes to forge:plan-approved -> proceed to task generation
    - Comment starting with ! -> regenerate Epics with feedback

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True, or error state if no epics.
    """
    ticket_key = state["ticket_key"]
    epic_keys = state.get("epic_keys", [])
    epic_count = len(epic_keys)

    request = project_approval(state, "plan", item_count=epic_count)
    outcome = run_approval_station(request)
    updates = reduce_approval_gate(state, request, outcome, "plan_approval_gate", "decompose_epics")
    logger.info(f"Plan approval gate: pausing workflow for {ticket_key} ({epic_count} Epics)")

    return update_state_timestamp({**state, **updates})


def route_plan_approval(state: WorkflowState) -> str:
    """Route based on plan approval status.

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    outcome = run_approval_station(
        project_approval(state, "plan", item_count=len(state.get("epic_keys") or []))
    )
    assert outcome.output is not None
    disposition = outcome.output.disposition
    if disposition is ApprovalDisposition.QUESTION:
        logger.info(f"Q&A mode: routing to answer_question for {state['ticket_key']}")
        return "answer_question"

    # YOLO mode: auto-approve without human input
    if disposition is ApprovalDisposition.APPROVED:
        logger.info(f"YOLO mode: auto-approving plan for {state['ticket_key']}")
        record_approval("plan")
        return "generate_tasks"

    # Check if revision requested
    if disposition is ApprovalDisposition.REVISION:
        if outcome.output.revision_scope == "item":
            # Single Epic update
            logger.info(
                "Single Epic revision requested for %s", state.get("current_epic_key")
            )
            record_revision_requested("plan")
            return "update_single_epic"
        else:
            # Feature-level regeneration
            logger.info(f"Full Epic regeneration requested for {state['ticket_key']}")
            record_revision_requested("plan")
            return "regenerate_all_epics"

    # Check if still paused - END and wait for approval webhook
    if disposition is ApprovalDisposition.WAITING:
        logger.info(
            f"Plan approval gate: workflow paused for {state['ticket_key']}, "
            "waiting for approval webhook"
        )
        return END

    return END
