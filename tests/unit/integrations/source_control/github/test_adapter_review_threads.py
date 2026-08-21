from unittest.mock import AsyncMock

import pytest

from forge.integrations.source_control.contracts import (
    ChangeRequestIdentity,
    Connection,
    Provider,
    RepositoryRef,
)
from forge.integrations.source_control.github.adapter import GitHubAdapter


def _conn():
    return Connection(
        name="c",
        provider=Provider.GITHUB,
        base_url="",
        credential_env="GITHUB_TOKEN",
        webhook_secret_env="",
    )


def _repo_ref():
    return RepositoryRef(
        id="acme/widgets",
        provider=Provider.GITHUB,
        connection="c",
        namespace="acme/widgets",
        default_branch="main",
        change_request_mode="fork",
    )


@pytest.mark.asyncio
async def test_get_review_thread_comments_maps_threads():
    client = AsyncMock()
    client.get_pull_request_review_threads.return_value = [
        {
            "thread_id": "T1",
            "path": "a.py",
            "line": 12,
            "comments": [{"comment_id": 99, "body": "fix this", "author": "rev"}],
        },
    ]
    adapter = GitHubAdapter(connection=_conn(), client=client)
    identity = ChangeRequestIdentity(connection="c", repository_id="acme/widgets", native_id=7)

    reviews = await adapter.get_review_thread_comments(_repo_ref(), identity)

    assert len(reviews) == 1
    review = reviews[0]
    assert review.id == "T1"
    assert len(review.comments) == 1
    c = review.comments[0]
    assert (c.id, c.body, c.author, c.path, c.line) == ("99", "fix this", "rev", "a.py", 12)
    client.get_pull_request_review_threads.assert_awaited_once_with("acme", "widgets", 7)


@pytest.mark.asyncio
async def test_get_review_comments_for_submission_falls_back_to_original_line():
    client = AsyncMock()
    client.get_review_comments.return_value = [
        {
            "id": 101,
            "body": "outdated diff comment",
            "user": {"login": "rev"},
            "path": "a.py",
            "line": None,
            "original_line": 42,
            "position": None,
        },
        {
            "id": 102,
            "body": "current diff comment",
            "user": {"login": "rev"},
            "path": "b.py",
            "line": 7,
            "original_line": 3,
        },
    ]
    adapter = GitHubAdapter(connection=_conn(), client=client)
    identity = ChangeRequestIdentity(connection="c", repository_id="acme/widgets", native_id=7)

    comments = await adapter.get_review_comments_for_submission(_repo_ref(), identity, "55")

    assert [(c.id, c.line) for c in comments] == [("101", 42), ("102", 7)]
    client.get_review_comments.assert_awaited_once_with("acme", "widgets", 7, 55)
