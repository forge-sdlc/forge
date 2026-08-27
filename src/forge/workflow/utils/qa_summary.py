"""Utility for posting Q&A summary to Jira on approval."""

import logging
from typing import Any

from forge.workflow.effect_runtime import JiraClient

logger = logging.getLogger(__name__)


async def post_qa_summary_if_needed(
    ticket_key: str,
    qa_history: list[dict[str, Any]],
    artifact_type: str,
) -> None:
    """Post a summary of Q&A exchanges when an artifact is approved.

    Args:
        ticket_key: Jira ticket key.
        qa_history: List of Q&A exchanges from state.
        artifact_type: Type of artifact that was approved (prd, spec, etc).
    """
    # Filter Q&A for this artifact type
    relevant_qa = [qa for qa in qa_history if qa.get("artifact_type") == artifact_type]

    if not relevant_qa:
        return

    logger.info(f"Posting Q&A summary for {ticket_key} ({len(relevant_qa)} exchanges)")

    jira = JiraClient()
    try:
        lines = [f"*Q&A Summary for {artifact_type.upper()}*\n"]
        for i, qa in enumerate(relevant_qa, 1):
            lines.append(f"*Q{i}:* {qa['question']}")
            lines.append(f"*A{i}:* {qa['answer']}\n")

        summary = "\n".join(lines)
        await jira.add_comment(ticket_key, summary)
        logger.info(f"Posted Q&A summary to {ticket_key}")
    except Exception as e:
        logger.warning(f"Failed to post Q&A summary for {ticket_key}: {e}")
    finally:
        await jira.close()
