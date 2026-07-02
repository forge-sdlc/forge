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
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import set_paused
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
    return cast(
        TaskTakeoverState,
        set_paused(cast(dict[str, Any], state), "task_plan_approval_gate"),
    )


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

    # 1. Q&A Mode
    if is_question:
        logger.info(f"Q&A mode: routing to answer_question for {ticket_key}")
        return "answer_question"

    # 2. Revision/Feedback requested (comment starting with !)
    if revision_requested:
        logger.info(f"Revision requested for {ticket_key}: routing to regenerate_plan")
        record_revision_requested("task_plan")
        return "regenerate_plan"

    # 3. YOLO Mode
    if state.get("yolo_mode"):
        logger.info(f"YOLO mode: auto-approving task plan for {ticket_key}")
        record_approval("task_plan")
        return "setup_workspace"

    # 4. If still paused, remain in paused state
    if state.get("is_paused"):
        logger.info(
            f"Task plan approval gate: workflow paused for {ticket_key}, "
            "waiting for approval webhook/label update"
        )
        return END

    # 5. Approved -> route to isolated execution setup node (setup_workspace)
    logger.info(f"Task plan approved for {ticket_key}, proceeding to workspace setup")
    record_approval("task_plan")
    return "setup_workspace"
