"""Triage node for bug workflow.

Evaluates whether a bug ticket contains sufficient information
for codebase analysis before any exploration begins.
"""

import logging

from langgraph.graph import END

from forge.config import get_settings
from forge.models.workflow import ForgeLabel
from forge.workflow.bug.state import BugState
from forge.workflow.effect_runtime import JiraClient
from forge.workflow.projections.triage import project_triage
from forge.workflow.stations.triage import TriageKind, run_triage_station
from forge.workflow.utils import set_paused, update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3

__all__ = ["triage_check", "triage_gate", "route_triage_gate"]


async def triage_check(state: BugState) -> BugState:
    """Evaluate a bug ticket for completeness before analysis.

    Posts an acknowledgement comment immediately, then evaluates the
    ticket against a seven-field checklist. If sufficient, transitions
    to analyze_bug. If missing fields, posts a targeted comment naming
    only absent fields, sets forge:triage-pending, and pauses.

    On resume (ticket updated), re-evaluates the full updated ticket.
    Error handling: retry up to 3 times, then route to escalate_blocked.

    Args:
        state: Current BugState.

    Returns:
        Updated BugState.
    """
    ticket_key = state["ticket_key"]
    retry_count = state.get("retry_count", 0)
    is_resume = state.get("current_node") == "triage_gate"

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        if retry_count >= _MAX_RETRIES:
            logger.error("triage_check exceeded max retries for %s", ticket_key)
            return {**state, "current_node": "escalate_blocked"}

        # Step 1: Post acknowledgement on first invocation only (not on resume)
        if not is_resume:
            await post_status_comment(
                jira,
                ticket_key,
                "Received this bug report — checking ticket completeness before starting analysis.",
            )

        # Step 2: Fetch full ticket content
        issue = await jira.get_issue(ticket_key)
        comments = await jira.get_comments(ticket_key)
        comment_text = "\n\n".join(c.body for c in comments if c.body)

        # Step 3: Invoke triage prompt
        outcome = await run_triage_station(
            project_triage(
                state,
                kind=TriageKind.BUG,
                summary=issue.summary or "",
                description=issue.description or "",
                comments=comment_text,
            )
        )
        assert outcome.output is not None

        # Step 4: Parse result
        if outcome.output.sufficient:
            pass_msg = (
                "Thanks for the update — ticket now has enough information to proceed. "
                "Starting root cause analysis — results will be posted here."
                if is_resume
                else "Ticket has enough information to proceed. Starting root cause analysis — results will be posted here."
            )
            await post_status_comment(jira, ticket_key, pass_msg)
            return update_state_timestamp(
                {
                    **state,
                    "triage_passed": True,
                    "triage_missing_fields": [],
                    "current_node": "analyze_bug",
                    "is_paused": False,
                    "is_question": False,
                    "revision_requested": False,
                    "feedback_comment": None,
                    "last_error": None,
                    "retry_count": 0,
                }
            )

        # Step 5: Missing fields path
        # Strip markdown code fences that LLMs sometimes add despite instructions
        missing_fields = list(outcome.output.missing_fields)

        fields_listed = "\n".join(f"- {f}" for f in missing_fields)
        await post_status_comment(
            jira,
            ticket_key,
            "To proceed with analysis, please reply with a comment starting with `!` "
            f"and provide the following information:\n\n{fields_listed}",
        )
        await jira.set_workflow_label(ticket_key, ForgeLabel.TRIAGE_PENDING)

        return update_state_timestamp(
            {
                **state,
                "triage_passed": False,
                "triage_missing_fields": missing_fields,
                "current_node": "triage_gate",
                "last_error": None,
                "retry_count": 0,
            }
        )

    except Exception as e:
        logger.error("triage_check failed for %s: %s", ticket_key, e)
        new_retry = retry_count + 1
        return {
            **state,
            "last_error": str(e),
            "retry_count": new_retry,
            "current_node": "escalate_blocked" if new_retry >= _MAX_RETRIES else "triage_check",
        }
    finally:
        await jira.close()


def triage_gate(state: BugState) -> BugState:
    """Pause gate after triage_check when ticket is incomplete.

    Sets is_paused=True and current_node='triage_gate'. The workflow
    resumes when the ticket is updated and the webhook re-triggers
    triage_check.

    Args:
        state: Current BugState.

    Returns:
        State with is_paused=True.
    """
    return set_paused(state, "triage_gate")


def route_triage_gate(state: BugState) -> str:
    """Route from triage_gate based on pause status.

    Args:
        state: Current BugState.

    Returns:
        END if still paused; 'triage_check' on resume.
    """
    if state.get("is_paused"):
        return END
    return "triage_check"
