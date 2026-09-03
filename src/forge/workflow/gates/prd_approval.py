"""PRD approval gate for human-in-the-loop review.

The PRD approval workflow uses labels:
- forge:prd-pending  - PRD awaiting approval
- forge:prd-approved - PRD approved (triggers spec generation)

To approve: Change label from forge:prd-pending to forge:prd-approved
To request revision: Add a comment starting with ! (keep forge:prd-pending)
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


def prd_approval_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for PM to review and approve the PRD.

    This gate pauses the workflow until a human approves or rejects
    the generated PRD. The workflow resumes when:
    - Label changes to forge:prd-approved -> continue to spec generation
    - Comment starting with ! -> regenerate PRD with feedback

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True.
    """
    ticket_key = state["ticket_key"]
    logger.info(f"PRD approval gate: pausing workflow for {ticket_key}")

    request = project_approval(state, "prd")
    outcome = run_approval_station(request)
    updates = reduce_approval_gate(state, request, outcome, "prd_approval_gate", "generate_prd")
    return update_state_timestamp({**state, **updates})


def route_prd_approval(state: WorkflowState) -> str:
    """Route based on PRD approval status.

    This routing function determines the next node after PRD approval gate:
    - If question (Q&A mode) -> answer_question
    - If yolo_mode enabled -> auto-approve without human input
    - If ! feedback provided (revision requested) -> regenerate PRD
    - If still paused -> END (wait for next webhook to resume)
    - Otherwise (approved) -> proceed to spec generation

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    outcome = run_approval_station(project_approval(state, "prd"))
    assert outcome.output is not None
    disposition = outcome.output.disposition
    if disposition is ApprovalDisposition.QUESTION:
        logger.info(f"Q&A mode: routing to answer_question for {state['ticket_key']}")
        return "answer_question"

    # YOLO mode: auto-approve without human input
    if disposition is ApprovalDisposition.APPROVED:
        logger.info(f"YOLO mode: auto-approving PRD for {state['ticket_key']}")
        record_approval("prd")
        return "generate_spec"

    # Check if revision was requested via ! comment
    if disposition is ApprovalDisposition.REVISION:
        logger.info(f"PRD revision requested for {state['ticket_key']}")
        record_revision_requested("prd")
        return "regenerate_prd"

    # Check if we should stay paused - END the workflow and wait for resume
    if disposition is ApprovalDisposition.WAITING:
        logger.info(
            f"PRD approval gate: workflow paused for {state['ticket_key']}, "
            "waiting for approval webhook"
        )
        return END

    return END
