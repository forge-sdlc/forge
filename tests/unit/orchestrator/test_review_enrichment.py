from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.integrations.source_control.contracts import (
    Provider,
    RepositoryRef,
    Review,
    ReviewComment,
)
from forge.orchestrator.review_enrichment import ReviewEnrichmentService


def _repo() -> RepositoryRef:
    return RepositoryRef(
        id="repo-1",
        provider=Provider.GITHUB,
        connection="default",
        namespace="acme/repo",
        default_branch="main",
        change_request_mode="direct",
    )


@pytest.mark.asyncio
async def test_review_provider_reads_are_hidden_behind_service() -> None:
    repo = _repo()
    adapter = MagicMock()
    adapter.get_review_thread_comments = AsyncMock(return_value=[Review(id="1", state="commented", body="", author="a")])
    service = ReviewEnrichmentService(lambda _name: (repo, adapter))

    result = await service.review_threads("acme/repo", 7)

    assert len(result) == 1
    adapter.get_review_thread_comments.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_comments_fall_back_to_thread_projection() -> None:
    repo = _repo()
    comment = ReviewComment(id="2", body="fix", author="a")
    adapter = MagicMock()
    adapter.get_review_thread_comments = AsyncMock(
        return_value=[Review(id="1", state="commented", body="", author="a", comments=[comment])]
    )
    service = ReviewEnrichmentService(lambda _name: (repo, adapter))

    result = await service.review_comments("acme/repo", 7, None)

    assert result == [comment]
