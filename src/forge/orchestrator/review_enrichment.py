"""Narrow provider-enrichment boundary for source-control review commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forge.integrations.source_control.contracts import (
    RepositoryRef,
    Review,
    ReviewComment,
    SourceControlProvider,
)
from forge.workflow.utils.automated_review_triage import (
    AutomatedReviewDecision,
    triage_automated_review,
)
from forge.workflow.utils.proposal_review_threads import (
    reply_to_proposal_decisions,
    triage_proposal_review_threads,
)
from forge.workflow.utils.source_control import identity_for

AdapterResolver = Callable[[str], tuple[RepositoryRef, SourceControlProvider]]


class ReviewEnrichmentService:
    """Own provider reads and semantic review analysis outside the worker."""

    def __init__(self, adapter_resolver: AdapterResolver) -> None:
        self._adapter_resolver = adapter_resolver

    async def review_threads(self, repo_full_name: str, pr_number: int) -> list[Review]:
        repo_ref, adapter = self._adapter_resolver(repo_full_name)
        return await adapter.get_review_thread_comments(
            repo_ref, identity_for(repo_ref, pr_number)
        )

    async def review_comments(
        self, repo_full_name: str, pr_number: int, review_id: int | None
    ) -> list[ReviewComment]:
        repo_ref, adapter = self._adapter_resolver(repo_full_name)
        identity = identity_for(repo_ref, pr_number)
        if review_id is not None:
            return await adapter.get_review_comments_for_submission(
                repo_ref, identity, str(review_id)
            )
        threads = await adapter.get_review_thread_comments(repo_ref, identity)
        return [comment for thread in threads for comment in thread.comments]

    async def triage_threads(
        self,
        *,
        artifact_type: str,
        artifact_content: str,
        threads: list[dict[str, Any]],
        ticket_key: str,
    ) -> list[dict[str, Any]]:
        return await triage_proposal_review_threads(
            artifact_type=artifact_type,
            artifact_content=artifact_content,
            threads=threads,
            ticket_key=ticket_key,
        )

    async def reply_to_decisions(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        decisions: list[dict[str, Any]],
    ) -> None:
        await reply_to_proposal_decisions(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            decisions=decisions,
            dispositions={"reply", "ignore"},
        )

    async def triage_automated(
        self,
        *,
        artifact_type: str,
        artifact_content: str,
        review_state: str,
        review_author: str,
        review_content: str,
        ticket_key: str,
    ) -> AutomatedReviewDecision:
        return await triage_automated_review(
            artifact_type=artifact_type,
            artifact_content=artifact_content,
            review_state=review_state,
            review_author=review_author,
            review_content=review_content,
            ticket_key=ticket_key,
        )
