"""Semantic triage for automated proposal reviews."""

import logging
from dataclasses import dataclass
from typing import Any, Literal

from forge.prompts import load_prompt
from forge.workflow.projections.agent_operation import project_agent_operation
from forge.workflow.stations.agent_operation import (
    AgentOperation,
    AgentOperationInput,
)
from forge.workflow.stations.runner import invoke_builtin_station

logger = logging.getLogger(__name__)

AutomatedReviewVerdict = Literal["blocking", "satisfied", "uncertain"]


@dataclass(frozen=True)
class AutomatedReviewDecision:
    """Normalized decision returned by the automated-review triage agent."""

    verdict: AutomatedReviewVerdict
    blocking_feedback: str = ""
    reason: str = ""


def is_bot_sender(payload: dict[str, Any]) -> bool:
    """Return whether a GitHub webhook was sent by a bot account."""
    sender: dict[str, Any] = payload.get("sender", {}) or {}
    review: dict[str, Any] = payload.get("review", {}) or {}
    review_user: dict[str, Any] = review.get("user", {}) or {}

    sender_type = str(sender.get("type", ""))
    review_user_type = str(review_user.get("type", ""))

    return bool(sender_type.lower() == "bot" or review_user_type.lower() == "bot")


async def triage_automated_review(
    *,
    artifact_type: str,
    artifact_content: str,
    review_state: str,
    review_author: str,
    review_content: str,
    ticket_key: str,
) -> AutomatedReviewDecision:
    """Ask a tool-free agent whether an automated review is still blocking."""
    prompt = load_prompt(
        "triage-automated-review",
        artifact_type=artifact_type,
        artifact_content=artifact_content,
        review_state=review_state or "comment",
        review_author=review_author,
        review_content=review_content,
    )
    try:
        outcome = await invoke_builtin_station(
            project_agent_operation(
                {"ticket_key": ticket_key},
                AgentOperationInput(
                    operation=AgentOperation.RUN_TASK,
                    task="triage-automated-review",
                    policy_key="automated_review_triage",
                    prompt=prompt,
                    context={"ticket_key": ticket_key},
                    include_tools=False,
                    response_schema="automated_review_triage",
                ),
                discriminator=f"automated-review:{artifact_type}:{review_author}",
            )
        )
        assert outcome.output is not None
        structured = outcome.output.structured
        if not isinstance(structured, dict):
            raise ValueError("Automated review triage returned no structured response")
        return AutomatedReviewDecision(
            verdict=structured["verdict"],
            blocking_feedback=str(structured.get("blocking_feedback", "")).strip(),
            reason=str(structured.get("reason", "")).strip(),
        )
    except Exception as exc:
        logger.warning("Automated review triage failed for %s: %s", ticket_key, exc)
        return AutomatedReviewDecision("uncertain", reason=f"Triage failed: {exc}")
