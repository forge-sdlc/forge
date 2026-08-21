from unittest.mock import AsyncMock

import pytest

from forge.integrations.github.client import GitHubClient


@pytest.mark.asyncio
async def test_review_threads_preserve_ids_and_skip_closed_threads() -> None:
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "THREAD_open",
                                "isResolved": False,
                                "isOutdated": False,
                                "path": "src/app.py",
                                "line": 12,
                                "originalLine": 10,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "COMMENT_node",
                                            "databaseId": 77,
                                            "body": "Handle the empty case.",
                                            "createdAt": "2026-07-29T00:00:00Z",
                                            "author": {"login": "reviewer"},
                                            "commit": {"oid": "abc123"},
                                        }
                                    ]
                                },
                            },
                            {
                                "id": "THREAD_closed",
                                "isResolved": True,
                                "isOutdated": False,
                                "comments": {"nodes": []},
                            },
                        ]
                    }
                }
            }
        }
    }
    http = AsyncMock()
    http.post.return_value = response
    github = GitHubClient()
    github._get_client = AsyncMock(return_value=http)

    threads = await github.get_pull_request_review_threads("org", "repo", 9)

    assert threads == [
        {
            "thread_id": "THREAD_open",
            "path": "src/app.py",
            "line": 12,
            "is_resolved": False,
            "is_outdated": False,
            "comments": [
                {
                    "node_id": "COMMENT_node",
                    "comment_id": 77,
                    "body": "Handle the empty case.",
                    "author": "reviewer",
                    "created_at": "2026-07-29T00:00:00Z",
                    "commit_sha": "abc123",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_reply_to_review_comment_uses_thread_reply_endpoint() -> None:
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {"id": 88}
    http = AsyncMock()
    http.post.return_value = response
    github = GitHubClient()
    github._get_client = AsyncMock(return_value=http)

    result = await github.reply_to_review_comment("org", "repo", 9, 77, "Addressed.")

    assert result == {"id": 88}
    http.post.assert_awaited_once_with(
        "/repos/org/repo/pulls/9/comments/77/replies",
        json={"body": "Addressed."},
    )


@pytest.mark.asyncio
async def test_review_threads_fall_back_to_rest() -> None:
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: [
        {
            "id": 77,
            "path": "src/app.py",
            "line": 12,
            "body": "Handle the empty case.",
            "user": {"login": "reviewer"},
            "created_at": "2026-07-29T00:00:00Z",
            "commit_id": "abc123",
        }
    ]
    http = AsyncMock()
    http.post.side_effect = RuntimeError("GraphQL unavailable")
    http.get.return_value = response
    github = GitHubClient()
    github._get_client = AsyncMock(return_value=http)

    threads = await github.get_pull_request_review_threads("org", "repo", 9)

    assert threads[0]["thread_id"] == "rest-77"
    assert threads[0]["comments"][0]["comment_id"] == 77
    http.get.assert_awaited_once_with("/repos/org/repo/pulls/9/comments", params={"per_page": 100})


@pytest.mark.asyncio
async def test_get_reviews_returns_review_submissions() -> None:
    payload = [
        {"id": 1, "state": "APPROVED", "body": "LGTM", "user": {"login": "reviewer"}},
        {"id": 2, "state": "CHANGES_REQUESTED", "body": "", "user": {"login": "other"}},
    ]
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: payload
    http = AsyncMock()
    http.get.return_value = response
    github = GitHubClient()
    github._get_client = AsyncMock(return_value=http)

    reviews = await github.get_reviews("org", "repo", 9)

    assert reviews == payload
    http.get.assert_awaited_once_with("/repos/org/repo/pulls/9/reviews")
