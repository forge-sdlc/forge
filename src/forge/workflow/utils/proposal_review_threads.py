"""Per-thread semantic triage for generated proposal reviews."""

import json
import logging
from typing import Any

from forge.api.routes.metrics import record_proposal_review_decision
from forge.prompts import load_prompt
from forge.workflow.projections.agent_operation import project_agent_operation
from forge.workflow.stations.agent_operation import (
    AgentOperation,
    AgentOperationInput,
)
from forge.workflow.stations.runner import invoke_builtin_station
from forge.workflow.utils.review_decisions import reply_to_review_decisions

logger = logging.getLogger(__name__)

_DISPOSITIONS = {"accept", "reply", "uncertain", "ignore"}


def normalize_proposal_thread_decisions(
    output: list[dict[str, Any]], threads: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Match validated decisions to source threads and fill missing decisions safely."""
    expected = {
        thread["thread_id"]: thread
        for thread in threads
        if thread.get("thread_id") and thread.get("comments")
    }
    decisions: dict[str, dict[str, Any]] = {}
    for item in output:
        if not isinstance(item, dict) or item.get("thread_id") not in expected:
            continue
        disposition = item.get("disposition")
        if disposition not in _DISPOSITIONS:
            continue
        thread = expected[item["thread_id"]]
        comment_id = thread["comments"][-1].get("comment_id")
        decisions[item["thread_id"]] = {
            "thread_id": item["thread_id"],
            "comment_id": comment_id,
            "disposition": disposition,
            "feedback": str(item.get("feedback", "")).strip(),
            "response": str(item.get("response", "")).strip(),
            "reason": str(item.get("reason", "")).strip(),
        }

    for thread_id, thread in expected.items():
        if thread_id in decisions:
            continue
        latest = thread["comments"][-1]
        decisions[thread_id] = {
            "thread_id": thread_id,
            "comment_id": latest.get("comment_id"),
            "disposition": "uncertain",
            "feedback": latest.get("body", ""),
            "response": "",
            "reason": "Thread triage was unavailable or inconclusive",
        }
    return list(decisions.values())


async def triage_proposal_review_threads(
    *, artifact_type: str, artifact_content: str, threads: list[dict[str, Any]], ticket_key: str
) -> list[dict[str, Any]]:
    """Classify proposal review threads in one tool-free agent invocation."""
    rendered_threads = json.dumps(threads, indent=2)
    prompt = load_prompt(
        "triage-proposal-review-threads",
        artifact_type=artifact_type,
        artifact_content=artifact_content,
        review_threads=rendered_threads,
    )
    try:
        outcome = await invoke_builtin_station(
            project_agent_operation(
                {"ticket_key": ticket_key},
                AgentOperationInput(
                    operation=AgentOperation.RUN_TASK,
                    task="triage-proposal-review-threads",
                    policy_key="proposal_review_triage",
                    prompt=prompt,
                    context={"ticket_key": ticket_key},
                    include_tools=False,
                    response_schema="proposal_review_triage",
                ),
                discriminator=f"proposal-review:{artifact_type}",
            )
        )
        assert outcome.output is not None
        structured = outcome.output.structured
        if not isinstance(structured, dict) or not isinstance(structured.get("decisions"), list):
            raise ValueError("Proposal review triage returned no structured decisions")
        output = structured["decisions"]
    except Exception as exc:
        logger.warning("Proposal thread triage failed for %s: %s", ticket_key, exc)
        output = []
    decisions = normalize_proposal_thread_decisions(output, threads)
    for decision in decisions:
        record_proposal_review_decision(artifact_type.lower(), decision["disposition"])
        logger.info(
            "Proposal review decision for %s thread %s: %s (%s)",
            artifact_type,
            decision["thread_id"],
            decision["disposition"],
            decision.get("reason", ""),
        )
    return decisions


async def reply_to_proposal_decisions(
    *,
    repo_full_name: str,
    pr_number: int,
    decisions: list[dict[str, Any]],
    dispositions: set[str],
    default_response: str = "",
) -> None:
    """Reply to selected proposal decisions in their originating threads."""
    for decision in decisions:
        if not str(decision.get("response", "")).strip():
            decision["response"] = default_response
    await reply_to_review_decisions(
        current_repo=repo_full_name,
        pr_number=pr_number,
        decisions=decisions,
        dispositions=dispositions,
        skip_addressed=True,
    )
