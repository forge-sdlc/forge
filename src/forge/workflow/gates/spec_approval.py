"""Specification approval gate for human-in-the-loop review.

The spec approval workflow uses labels:
- forge:spec-pending  - Spec awaiting approval
- forge:spec-approved - Spec approved (triggers epic decomposition)

To approve: Change label from forge:spec-pending to forge:spec-approved
To request revision: Add a comment with feedback (keep forge:spec-pending)
"""

import logging

from langgraph.graph import END

from forge.api.routes.metrics import record_approval, record_revision_requested
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import set_paused

logger = logging.getLogger(__name__)


def spec_approval_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for PM to review and approve the specification.

    This gate pauses the workflow until a human approves or rejects
    the generated specification. The workflow resumes when:
    - Label changes to forge:spec-approved -> continue to epic decomposition
    - Comment added with feedback -> regenerate spec with feedback

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True.
    """
    ticket_key = state["ticket_key"]
    logger.info(f"Spec approval gate: pausing workflow for {ticket_key}")

    return set_paused(state, "spec_approval_gate")


def route_spec_approval(state: WorkflowState) -> str:
    """Route based on spec approval status.

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    # Check if this is a question (Q&A mode) - check FIRST
    if state.get("is_question") and state.get("feedback_comment"):
        logger.info(f"Q&A mode: routing to answer_question for {state['ticket_key']}")
        return "answer_question"

    # YOLO mode: auto-approve without human input
    if state.get("yolo_mode"):
        logger.info(f"YOLO mode: auto-approving spec for {state['ticket_key']}")
        record_approval("spec")
        return "decompose_epics"

    # Check if revision was requested
    if state.get("revision_requested") and state.get("feedback_comment"):
        logger.info(f"Spec revision requested for {state['ticket_key']}")
        record_revision_requested("spec")
        return "regenerate_spec"

    # Check if still paused - END and wait for approval webhook
    if state.get("is_paused"):
        logger.info(
            f"Spec approval gate: workflow paused for {state['ticket_key']}, "
            "waiting for approval webhook"
        )
        return END

    # Spec approved, proceed to epic decomposition
    logger.info(f"Spec approved for {state['ticket_key']}, proceeding to epic decomposition")
    record_approval("spec")
    return "decompose_epics"
