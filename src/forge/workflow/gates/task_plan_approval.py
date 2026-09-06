"""Task plan approval gate for standalone task-takeover workflow review.

The task plan approval workflow uses labels:
- forge:plan-pending  - Task plan awaiting approval
- forge:plan-approved - Task plan approved (triggers isolated execution workspace setup)

To approve: Change label to forge:plan-approved
To request revision: Add a comment with prefix '!' (keep forge:plan-pending)
To ask clarifying questions: Add a comment with prefix '?' or '@forge ask'
"""

import logging
from typing import Any, cast

from langgraph.graph import END

from forge.api.routes.metrics import record_approval, record_revision_requested
from forge.workflow.projections.approval import project_approval
from forge.workflow.reducers.approval import reduce_approval_gate
from forge.workflow.stations.approval import ApprovalDisposition, run_approval_station
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.comment_classifier import CommentType, classify_comment

logger = logging.getLogger(__name__)


def task_plan_approval_gate(state: TaskTakeoverState) -> TaskTakeoverState:
    """Pause task takeover workflow for human review of the generated plan.

    Args:
        state: Current task takeover workflow state.

    Returns:
        State with is_paused=True and current_node="task_plan_approval_gate".
    """
    ticket_key = state.get("ticket_key", "unknown")
    logger.info(f"Task plan approval gate: pausing workflow for {ticket_key}")
    raw = cast(dict[str, Any], state)
    request = project_approval(raw, "task_plan")
    outcome = run_approval_station(request)
    updates = reduce_approval_gate(
        raw, request, outcome, "task_plan_approval_gate", "generate_plan"
    )
    return cast(TaskTakeoverState, update_state_timestamp({**raw, **updates}))


def route_task_plan_approval(state: TaskTakeoverState) -> str:
    """Route after task plan approval gate resumes.

    Args:
        state: Current TaskTakeoverState.

    Returns:
        Name of the next node or END.
    """
    ticket_key = state.get("ticket_key", "unknown")
    feedback = state.get("feedback_comment")
    is_question = state.get("is_question", False)
    revision_requested = state.get("revision_requested", False)

    # Classify comment text if available
    if feedback:
        comment_type = classify_comment(feedback)
        if comment_type == CommentType.QUESTION:
            is_question = True
        elif comment_type == CommentType.FEEDBACK:
            revision_requested = True

    evaluation_state = cast(dict[str, Any], state) | {
        "is_question": is_question,
        "revision_requested": revision_requested,
    }
    outcome = run_approval_station(project_approval(evaluation_state, "task_plan"))
    assert outcome.output is not None
    disposition = outcome.output.disposition
    if disposition is ApprovalDisposition.QUESTION:
        logger.info(f"Q&A mode: routing to answer_question for {ticket_key}")
        return "answer_question"

    # 2. Revision/Feedback requested (comment starting with !)
    if disposition is ApprovalDisposition.REVISION:
        logger.info(f"Revision requested for {ticket_key}: routing to regenerate_plan")
        record_revision_requested("task_plan")
        return "regenerate_plan"

    # 3. YOLO Mode
    if disposition is ApprovalDisposition.APPROVED:
        logger.info(f"YOLO mode: auto-approving task plan for {ticket_key}")
        record_approval("task_plan")
        return "setup_workspace"

    # 4. If still paused, remain in paused state
    if disposition is ApprovalDisposition.WAITING:
        logger.info(
            f"Task plan approval gate: workflow paused for {ticket_key}, "
            "waiting for approval webhook/label update"
        )
        return END

    return END
