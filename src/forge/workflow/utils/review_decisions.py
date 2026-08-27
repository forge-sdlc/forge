"""Shared helpers for persisted per-thread review decisions."""

import logging
from typing import Any

from forge.workflow.utils.source_control import get_adapter, identity_for

logger = logging.getLogger(__name__)


def merge_review_decisions(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep the latest decision per thread while retaining processed history."""
    merged = {
        item["thread_id"]: item
        for item in previous
        if isinstance(item, dict) and item.get("thread_id")
    }
    merged.update(
        {
            item["thread_id"]: item
            for item in current
            if isinstance(item, dict) and item.get("thread_id")
        }
    )
    return list(merged.values())


def flatten_review_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the latest comment from each non-empty review thread."""
    return [
        {
            "path": thread.get("path", ""),
            "line": thread.get("line"),
            "body": thread["comments"][-1].get("body", ""),
        }
        for thread in threads
        if thread.get("comments")
    ]


def decision_matches_comment(decision: dict[str, Any], comment_id: str | int) -> bool:
    """Match either the reviewer comment or Forge's reply in the same thread."""
    target = str(comment_id)
    candidates = {
        str(value)
        for value in (decision.get("comment_id"), decision.get("forge_reply_id"))
        if value is not None
    }
    return target in candidates


async def reply_to_review_decisions(
    *,
    current_repo: str,
    pr_number: int | None,
    decisions: list[dict[str, Any]],
    dispositions: set[str] | None = None,
    skip_addressed: bool = False,
) -> None:
    """Reply consistently and retain Forge's reply ID for later correlation."""
    if not current_repo or not pr_number or not decisions:
        return

    try:
        repo_ref, adapter = get_adapter(current_repo)
        identity = identity_for(repo_ref, pr_number)
    except Exception as exc:
        logger.warning("Could not resolve adapter for %s: %s", current_repo, exc)
        return

    for decision in decisions:
        if dispositions is not None and decision.get("disposition") not in dispositions:
            continue
        if skip_addressed and decision.get("status") == "addressed":
            continue
        comment_id = decision.get("comment_id")
        response = str(decision.get("response", "")).strip()
        if comment_id is None or not response:
            continue
        try:
            reply = await adapter.reply_to_comment(repo_ref, identity, str(comment_id), response)
            decision["forge_reply_id"] = reply.id
        except Exception as exc:
            logger.warning("Failed replying to review comment %s: %s", comment_id, exc)
