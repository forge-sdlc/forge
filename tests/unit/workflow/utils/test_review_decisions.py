from unittest.mock import AsyncMock, patch

import pytest

from forge.integrations.source_control.contracts import Provider, RepositoryRef, ReviewComment
from forge.workflow.utils.review_decisions import (
    decision_matches_comment,
    flatten_review_threads,
    merge_review_decisions,
    reply_to_review_decisions,
)


def _repo_ref(repo: str = "org/repo") -> RepositoryRef:
    return RepositoryRef(
        id=repo,
        provider=Provider.GITHUB,
        connection="c",
        namespace=repo,
        default_branch="main",
        change_request_mode="fork",
    )


def test_merge_keeps_latest_decision_per_thread() -> None:
    previous = [
        {"thread_id": "thread-a", "disposition": "reply"},
        {"thread_id": "thread-b", "disposition": "accept"},
    ]
    current = [{"thread_id": "thread-a", "disposition": "accept"}]

    assert merge_review_decisions(previous, current) == [
        {"thread_id": "thread-a", "disposition": "accept"},
        {"thread_id": "thread-b", "disposition": "accept"},
    ]


def test_merge_ignores_items_without_thread_identity() -> None:
    assert merge_review_decisions([{"text": "legacy"}], [{"disposition": "accept"}]) == []


def test_flatten_review_threads_skips_empty_threads() -> None:
    threads = [
        {"thread_id": "empty", "path": "a.py", "line": 1, "comments": []},
        {
            "thread_id": "valid",
            "path": "b.py",
            "line": 2,
            "comments": [{"body": "first"}, {"body": "latest"}],
        },
    ]

    assert flatten_review_threads(threads) == [{"path": "b.py", "line": 2, "body": "latest"}]


def test_decision_matches_original_or_forge_reply_comment() -> None:
    decision = {"comment_id": 10, "forge_reply_id": 11}

    assert decision_matches_comment(decision, 10)
    assert decision_matches_comment(decision, 11)
    assert not decision_matches_comment(decision, 12)


def test_decision_matches_comment_across_int_and_str_ids() -> None:
    """forge_reply_id now comes back as a str from the adapter; comment_id may
    still be int from older persisted state. Comparison must not care which."""
    decision = {"comment_id": 10, "forge_reply_id": "11"}

    assert decision_matches_comment(decision, "10")
    assert decision_matches_comment(decision, 11)
    assert not decision_matches_comment(decision, 12)


@pytest.mark.asyncio
async def test_reply_records_forge_comment_id() -> None:
    adapter = AsyncMock()
    adapter.reply_to_comment = AsyncMock(
        return_value=ReviewComment(id="11", body="ok", author="forge")
    )
    decision = {"comment_id": 10, "response": "Please confirm."}

    with patch(
        "forge.workflow.utils.review_decisions.get_adapter",
        return_value=(_repo_ref(), adapter),
    ):
        await reply_to_review_decisions(current_repo="org/repo", pr_number=7, decisions=[decision])

    assert decision["forge_reply_id"] == "11"
    adapter.reply_to_comment.assert_awaited_once()
    call_args = adapter.reply_to_comment.call_args[0]
    assert call_args[1].native_id == 7
    assert call_args[2] == "10"
    assert call_args[3] == "Please confirm."
