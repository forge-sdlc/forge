"""RCA option gate node and routing for bug workflow."""

import logging

from langgraph.graph import END

from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.workflow.bug.state import BugState
from forge.workflow.utils import set_paused, update_state_timestamp

logger = logging.getLogger(__name__)

_TRUNCATION_NOTE = "*(RCA truncated — full analysis available in the analysis container logs.)*"
_MAX_COMMENT_CHARS = 25_000

__all__ = ["rca_option_gate", "route_rca_option", "regenerate_rca"]


async def rca_option_gate(state: BugState) -> BugState:
    """Format and post RCA with fix options to Jira, then pause for option selection.

    Posts structured comment with RCA summary and numbered fix options (skipped if
    rca_comment_posted is already True, to avoid duplicate posts on gate re-entry).
    Applies 25k character truncation guard. Sets forge:rca-pending label. Pauses
    workflow for >option N comment or feedback.

    Args:
        state: Current bug workflow state. Expects rca_content and rca_options populated.

    Returns:
        Updated state with is_paused=True and current_node="rca_option_gate".
    """
    ticket_key = state["ticket_key"]
    rca_content = state.get("rca_content") or ""
    rca_options = state.get("rca_options") or []
    rca_comment_posted = state.get("rca_comment_posted", False)

    jira = JiraClient()

    try:
        if not rca_comment_posted:
            comment = _format_rca_comment(rca_content, rca_options)
            await jira.add_comment(ticket_key, comment)
        await jira.set_workflow_label(ticket_key, ForgeLabel.RCA_PENDING)
    finally:
        await jira.close()

    paused = set_paused(state, "rca_option_gate")
    return {**paused, "rca_comment_posted": True}


def _format_rca_comment(
    rca_content: str,
    rca_options: list[dict],
    max_chars: int = _MAX_COMMENT_CHARS,
) -> str:
    """Format RCA content and options into a Jira comment string.

    Applies a character limit guard: if the formatted comment exceeds max_chars,
    truncates rca_content at the last paragraph boundary before the limit and
    appends a truncation note.

    Args:
        rca_content: Full RCA text (may include metadata fields).
        rca_options: List of option dicts with title, description, tradeoffs.
        max_chars: Character limit before truncation is applied.

    Returns:
        Formatted comment string, truncated if necessary.
    """
    options_text = "\n\n".join(
        f"**Option {i + 1}: {opt.get('title', '')}**\n"
        f"{opt.get('description', '')}\n"
        f"*Tradeoffs:* {opt.get('tradeoffs', '')}"
        for i, opt in enumerate(rca_options)
    )

    footer = "\n\nReply with `>option N` to select an approach, or comment with feedback to revise the RCA."
    header = "## Root Cause Analysis\n\n"
    options_header = "\n\n## Fix Options\n\n"

    full_comment = header + rca_content + options_header + options_text + footer

    if len(full_comment) <= max_chars:
        return full_comment

    # Truncate rca_content at last paragraph boundary before limit
    overhead = (
        len(header)
        + len(options_header)
        + len(options_text)
        + len(footer)
        + len(_TRUNCATION_NOTE)
        + 4
    )
    available_for_rca = max(0, max_chars - overhead)
    truncated_rca = rca_content[:available_for_rca]
    last_para = truncated_rca.rfind("\n\n")
    if last_para > 0:
        truncated_rca = truncated_rca[:last_para]

    return (
        header + truncated_rca + "\n\n" + _TRUNCATION_NOTE + options_header + options_text + footer
    )


def route_rca_option(state: BugState) -> str:
    """Route from rca_option_gate based on workflow signals.

    Priority order:
    1. is_question=True → "answer_question"
    2. selected_fix_option is not None and not is_paused → "plan_bug_fix"
    3. revision_requested=True → "regenerate_rca"
    4. is_paused=True → END

    Args:
        state: Current bug workflow state.

    Returns:
        Next node name or END sentinel.
    """
    if state.get("is_question"):
        return "answer_question"

    if state.get("selected_fix_option") is not None and not state.get("is_paused"):
        return "plan_bug_fix"

    if state.get("revision_requested"):
        return "regenerate_rca"

    if state.get("is_paused"):
        return END

    return END


async def regenerate_rca(state: BugState) -> BugState:
    """Route back to analyze_bug with user feedback as the reflection critique.

    Passes the user's feedback comment directly as reflection_critique so that
    the next analyze_bug container run addresses it. No agent or container is
    needed here — analyze_bug already does all the investigation.

    Also resets reflection_count and retry_count so the new analysis gets a
    fresh reflection loop, and clears is_paused which was set by rca_option_gate.

    Args:
        state: Current bug workflow state with revision_requested=True
               and feedback_comment containing user feedback.

    Returns:
        Updated state with current_node="analyze_bug" and reflection_critique
        set to the user's feedback.
    """
    ticket_key = state["ticket_key"]
    feedback = state.get("feedback_comment") or ""

    jira = JiraClient()
    try:
        await jira.add_comment(
            ticket_key,
            "Revisiting the analysis based on your feedback — this will take a few minutes.",
        )
    except Exception as e:
        logger.warning(f"Could not post regenerate_rca acknowledgement for {ticket_key}: {e}")
    finally:
        await jira.close()

    return update_state_timestamp(
        {
            **state,
            "reflection_critique": feedback or None,
            "feedback_comment": None,
            "revision_requested": False,
            "selected_fix_option": None,
            "selected_fix_approach": None,
            "rca_comment_posted": False,
            "reflection_count": 0,
            "retry_count": 0,
            "is_paused": False,
            "current_node": "analyze_bug",
            "last_error": None,
        }
    )
