"""Tests for spec PR event handling in the worker."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    Review,
    ReviewComment,
    ReviewState,
)
from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage, normalized_event_to_dict
from forge.workflow.utils.automated_review_triage import AutomatedReviewDecision


def _repo_ref_for(namespace: str) -> RepositoryRef:
    return RepositoryRef(
        id=namespace,
        provider=Provider.GITHUB,
        connection="default-github",
        namespace=namespace,
        default_branch="main",
        change_request_mode="fork",
    )


def _patch_adapter(repo_ref: RepositoryRef, adapter):
    """Patch worker.get_adapter to resolve to the given (repo_ref, adapter) pair."""
    return patch("forge.orchestrator.worker.get_adapter", return_value=(repo_ref, adapter))


_REVIEW_STATES = {
    "approved": ReviewState.APPROVED,
    "changes_requested": ReviewState.CHANGES_REQUESTED,
    "commented": ReviewState.COMMENTED,
    "pending": ReviewState.PENDING,
}


def _normalized_from_payload(event_type: str, payload: dict) -> NormalizedEvent:
    """Build the NormalizedEvent a GitHub webhook payload would produce.

    Mirrors GitHubAdapter.parse_webhook for the event types exercised here so the
    typed detection in _handle_resume_event runs against realistic data while the
    raw payload is still carried for the triage blocks that read it.
    """
    base_type = event_type.split(":", 1)[0]
    repo_full = payload.get("repository", {}).get("full_name", "")
    sender = payload.get("sender", {})
    sender_login = sender.get("login", "")
    actor = Actor(
        login=sender_login,
        is_bot=sender.get("type") == "Bot" or "[bot]" in sender_login,
    )
    repo_ref = RepositoryRef(
        id=repo_full,
        provider=Provider.GITHUB,
        connection="default-github",
        namespace=repo_full,
        default_branch="main",
        change_request_mode="fork",
    )

    change_request = None
    pr = payload.get("pull_request")
    issue = payload.get("issue")
    if pr is not None:
        if pr.get("merged", False):
            state = ChangeRequestState.MERGED
        elif pr.get("state") == "closed":
            state = ChangeRequestState.CLOSED
        else:
            state = ChangeRequestState.OPEN
        change_request = ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="default-github",
                repository_id=repo_full,
                native_id=pr.get("number"),
            ),
            url=pr.get("html_url", ""),
            title=pr.get("title", ""),
            body=pr.get("body", "") or "",
            state=state,
            source_branch="",
            target_branch="",
            draft=False,
        )
    elif issue is not None:
        change_request = ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="default-github",
                repository_id=repo_full,
                native_id=issue.get("number"),
            ),
            url=issue.get("html_url", ""),
            title=issue.get("title", ""),
            body=issue.get("body", "") or "",
            state=ChangeRequestState.OPEN,
            source_branch="",
            target_branch="",
            draft=False,
        )

    comment = None
    review = None
    if base_type in ("issue_comment", "pull_request_review_comment"):
        kind = EventKind.COMMENT_CREATED
        raw_comment = payload.get("comment", {})
        in_reply = raw_comment.get("in_reply_to_id")
        comment = ReviewComment(
            id=str(raw_comment.get("id", "")),
            body=raw_comment.get("body", "") or "",
            author=(raw_comment.get("user") or {}).get("login", ""),
            path=raw_comment.get("path"),
            line=raw_comment.get("line"),
            in_reply_to=str(in_reply) if in_reply is not None else None,
        )
    elif base_type == "pull_request_review":
        kind = EventKind.REVIEW_SUBMITTED
        raw_review = payload.get("review", {})
        review = Review(
            id=str(raw_review.get("id", "")),
            state=_REVIEW_STATES.get(
                (raw_review.get("state") or "").lower(), ReviewState.COMMENTED
            ),
            body=raw_review.get("body", "") or "",
            author=(raw_review.get("user") or {}).get("login", ""),
            comments=[],
        )
    elif base_type == "pull_request":
        if change_request is not None and change_request.state == ChangeRequestState.MERGED:
            kind = EventKind.CR_MERGED
        elif change_request is not None and change_request.state == ChangeRequestState.CLOSED:
            kind = EventKind.CR_CLOSED
        else:
            kind = EventKind.CR_UPDATED
    else:
        kind = EventKind.CR_UPDATED

    return NormalizedEvent(
        id="evt-1",
        kind=kind,
        repo_ref=repo_ref,
        actor=actor,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        change_request=change_request,
        comment=comment,
        review=review,
        raw=payload,
    )


def _make_message(event_type: str, payload: dict, ticket_key: str = "TEST-123") -> QueueMessage:
    return QueueMessage(
        message_id="msg-1",
        event_id="evt-1",
        source=EventSource.SOURCE_CONTROL,
        event_type=event_type,
        ticket_key=ticket_key,
        payload=payload,
        normalized_event=normalized_event_to_dict(_normalized_from_payload(event_type, payload)),
    )


def _make_normalized_event(**overrides) -> NormalizedEvent:
    """A canned NormalizedEvent (repo acme/payments, PR 42) for typed-field tests."""
    repo_ref = RepositoryRef(
        id="acme/payments",
        provider=Provider.GITHUB,
        connection="default-github",
        namespace="acme/payments",
        default_branch="main",
        change_request_mode="fork",
    )
    change_request = ChangeRequest(
        identity=ChangeRequestIdentity(
            connection="default-github", repository_id="acme/payments", native_id=42
        ),
        url="https://github.com/acme/payments/pull/42",
        title="t",
        body="",
        state=ChangeRequestState.OPEN,
        source_branch="feature",
        target_branch="main",
        draft=False,
    )
    defaults = {
        "id": "delivery-1",
        "kind": EventKind.CR_OPENED,
        "repo_ref": repo_ref,
        "actor": Actor(login="octocat", is_bot=False),
        "received_at": datetime(2026, 1, 1, tzinfo=UTC),
        "change_request": change_request,
        "raw": {},
    }
    defaults.update(overrides)
    return NormalizedEvent(**defaults)


def _spec_gate_state(**overrides) -> dict:
    base = {
        "ticket_key": "TEST-123",
        "current_node": "spec_approval_gate",
        "is_paused": True,
        "spec_content": "# Spec",
        "spec_pr_number": 12,
        "spec_pr_repo": "org/proposals",
        "spec_pr_branch": "forge/spec/test-123",
        "spec_pr_url": "https://github.com/org/proposals/pull/12",
        "context": {},
        "last_error": None,
        "revision_requested": False,
        "feedback_comment": None,
        "is_question": False,
        "retry_count": 0,
        "is_blocked": False,
    }
    base.update(overrides)
    return base


@pytest.fixture
def worker():
    with patch("forge.orchestrator.worker.get_checkpointer"):
        w = OrchestratorWorker.__new__(OrchestratorWorker)
        w._post_terminal_error_comment = AsyncMock()
        w._post_resume_ack_comment = AsyncMock()
        w._forge_github_logins = {}
        return w


class TestHandleSpecPrMerge:
    @pytest.mark.asyncio
    async def test_pr_merge_uses_configured_custom_field_storage(self, worker):
        msg = _make_message(
            "pull_request:closed",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 12, "merged": True},
            },
        )
        state = _spec_gate_state()
        settings = MagicMock(
            jira_store_in_comments=False,
            jira_spec_custom_field="customfield_12345",
        )

        with (
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
            patch("forge.orchestrator.worker.JiraClient") as MockJira,
        ):
            mock_jira = MagicMock()
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.update_custom_field = AsyncMock()
            mock_jira.add_structured_comment = AsyncMock()
            mock_jira.add_attachment = AsyncMock()
            mock_jira.delete_attachments_by_name = AsyncMock()
            mock_jira.close = AsyncMock()
            MockJira.return_value = mock_jira

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        mock_jira.set_workflow_label.assert_called_once()
        mock_jira.update_custom_field.assert_called_once_with(
            "TEST-123",
            "customfield_12345",
            "# Spec",
        )
        mock_jira.add_structured_comment.assert_not_called()
        mock_jira.add_attachment.assert_not_called()


@pytest.mark.asyncio
async def test_satisfied_bot_spec_review_stays_paused(worker):
    msg = _make_message(
        "issue_comment:created",
        {
            "repository": {"full_name": "org/proposals"},
            "issue": {"number": 12},
            "comment": {"body": "!The specification passes. Optional suggestions follow."},
            "sender": {"login": "reviewer[bot]", "type": "Bot"},
        },
    )
    state = _spec_gate_state()

    mock_adapter = AsyncMock()
    mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
    with (
        _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
        patch(
            "forge.orchestrator.worker.triage_automated_review",
            new=AsyncMock(return_value=AutomatedReviewDecision("satisfied")),
        ) as triage,
    ):
        result = await worker._handle_resume_event(msg, state)

    assert result == state
    triage.assert_awaited_once()


class TestSpecPrReviewTypedFields:
    """Brief Step 1 (spec mirror): spec-PR review/merge read typed event fields."""

    @pytest.mark.asyncio
    async def test_review_with_changes_requested_sets_feedback(self, worker):
        event = _make_normalized_event(kind=EventKind.REVIEW_SUBMITTED)
        event.review = Review(
            id="1",
            state=ReviewState.CHANGES_REQUESTED,
            body="please fix X",
            author="reviewer1",
        )
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "spec_approval_gate",
            "is_paused": True,
            "spec_pr_number": 42,
            "spec_pr_repo": "acme/payments",
        }

        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = []
        with _patch_adapter(_repo_ref_for("acme/payments"), mock_adapter):
            updated = await worker._handle_resume_event(message, current_state)

        assert updated["revision_requested"] is True
        assert "please fix X" in updated["feedback_comment"]

    @pytest.mark.asyncio
    async def test_pr_merged_sets_approved(self, worker):
        event = _make_normalized_event(kind=EventKind.CR_MERGED)
        event.change_request.state = ChangeRequestState.MERGED
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_merged",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "spec_approval_gate",
            "is_paused": True,
            "spec_pr_number": 42,
            "spec_pr_repo": "acme/payments",
        }

        with patch("forge.orchestrator.worker.JiraClient") as MockJira:
            MockJira.return_value.set_workflow_label = AsyncMock()
            MockJira.return_value.close = AsyncMock()
            updated = await worker._handle_resume_event(message, current_state)

        assert updated["is_paused"] is False
