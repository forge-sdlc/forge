"""Semantic triage for automated proposal reviews."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from forge.prompts import load_prompt

logger = logging.getLogger(__name__)

AutomatedReviewVerdict = Literal["blocking", "satisfied", "uncertain"]


@dataclass(frozen=True)
class AutomatedReviewDecision:
    """Normalized decision returned by the automated-review triage agent."""

    verdict: AutomatedReviewVerdict
    blocking_feedback: str = ""
    reason: str = ""


def is_bot_sender(payload: dict) -> bool:
    """Return whether a GitHub webhook was sent by a bot account."""
    sender = payload.get("sender", {})
    review_user = payload.get("review", {}).get("user", {})
    return sender.get("type", "").lower() == "bot" or review_user.get("type", "").lower() == "bot"


def parse_automated_review_decision(output: str) -> AutomatedReviewDecision:
    """Parse triage output, falling back to an uncertain revision decision."""
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        return AutomatedReviewDecision("uncertain", reason="Triage returned no JSON object")

    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return AutomatedReviewDecision("uncertain", reason="Triage returned invalid JSON")

    verdict = data.get("verdict")
    if verdict not in ("blocking", "satisfied", "uncertain"):
        return AutomatedReviewDecision("uncertain", reason="Triage returned an invalid verdict")

    feedback = data.get("blocking_feedback", "")
    reason = data.get("reason", "")
    if not isinstance(feedback, str) or not isinstance(reason, str):
        return AutomatedReviewDecision("uncertain", reason="Triage returned invalid fields")
    if verdict == "blocking" and not feedback.strip():
        return AutomatedReviewDecision(
            "uncertain", reason="Triage marked the review blocking without feedback"
        )

    return AutomatedReviewDecision(verdict, feedback.strip(), reason.strip())


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
    # Keep the comparatively heavy agent integration out of webhook worker imports.
    from forge.integrations.agents.agent import ForgeAgent

    prompt = load_prompt(
        "triage-automated-review",
        artifact_type=artifact_type,
        artifact_content=artifact_content,
        review_state=review_state or "comment",
        review_author=review_author,
        review_content=review_content,
    )
    try:
        output = await ForgeAgent().run_task(
            task="triage-automated-review",
            policy_key="automated_review_triage",
            prompt=prompt,
            context={"ticket_key": ticket_key},
            include_tools=False,
        )
    except Exception as exc:
        logger.warning("Automated review triage failed for %s: %s", ticket_key, exc)
        return AutomatedReviewDecision("uncertain", reason=f"Triage failed: {exc}")
    return parse_automated_review_decision(output)
