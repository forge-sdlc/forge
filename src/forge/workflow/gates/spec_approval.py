"""Specification approval gate for human-in-the-loop review.

The spec approval workflow uses labels:
- forge:spec-pending  - Spec awaiting approval
- forge:spec-approved - Spec approved (triggers epic decomposition)

To approve: Change label from forge:spec-pending to forge:spec-approved
To request revision: Add a comment starting with ! (keep forge:spec-pending)
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


def spec_approval_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for PM to review and approve the specification.

    This gate pauses the workflow until a human approves or rejects
    the generated specification. The workflow resumes when:
    - Label changes to forge:spec-approved -> continue to epic decomposition
    - Comment starting with ! -> regenerate spec with feedback

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True.
    """
    ticket_key = state["ticket_key"]
    logger.info(f"Spec approval gate: pausing workflow for {ticket_key}")

    request = project_approval(state, "spec")
    outcome = run_approval_station(request)
    updates = reduce_approval_gate(state, request, outcome, "spec_approval_gate", "generate_spec")
    return update_state_timestamp({**state, **updates})


def route_spec_approval(state: WorkflowState) -> str:
    """Route based on spec approval status.

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    outcome = run_approval_station(project_approval(state, "spec"))
    assert outcome.output is not None
    disposition = outcome.output.disposition
    if disposition is ApprovalDisposition.QUESTION:
        logger.info(f"Q&A mode: routing to answer_question for {state['ticket_key']}")
        return "answer_question"

    # YOLO mode: auto-approve without human input
    if disposition is ApprovalDisposition.APPROVED:
        logger.info(f"YOLO mode: auto-approving spec for {state['ticket_key']}")
        record_approval("spec")
        return "decompose_epics"

    # Check if revision was requested
    if disposition is ApprovalDisposition.REVISION:
        logger.info(f"Spec revision requested for {state['ticket_key']}")
        record_revision_requested("spec")
        return "regenerate_spec"

    # Check if still paused - END and wait for approval webhook
    if disposition is ApprovalDisposition.WAITING:
        logger.info(
            f"Spec approval gate: workflow paused for {state['ticket_key']}, "
            "waiting for approval webhook"
        )
        return END

    return END
