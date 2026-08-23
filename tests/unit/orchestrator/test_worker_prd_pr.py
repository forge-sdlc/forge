"""Tests for PRD PR event handling in the worker."""

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
from forge.workflow.utils.source_control import identity_for


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


def _prd_gate_state(**overrides) -> dict:
    base = {
        "ticket_key": "TEST-123",
        "current_node": "prd_approval_gate",
        "is_paused": True,
        "prd_pr_number": 7,
        "prd_pr_repo": "org/proposals",
        "prd_pr_branch": "forge/prd/test-123",
        "prd_pr_url": "https://github.com/org/proposals/pull/7",
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


class TestIsPrdPrEvent:
    def test_true_for_matching_repo_and_pr(self, worker):
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
            },
        )
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is True

    def test_false_for_wrong_repo(self, worker):
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/other-repo"},
                "pull_request": {"number": 7},
            },
        )
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is False

    def test_false_for_wrong_pr_number(self, worker):
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 99},
            },
        )
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is False

    def test_false_when_no_prd_pr_in_state(self, worker):
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
            },
        )
        state = _prd_gate_state(prd_pr_number=None, prd_pr_repo=None)
        assert worker._is_prd_pr_event(msg, state) is False

    def test_false_for_jira_events(self, worker):
        msg = QueueMessage(
            message_id="msg-1",
            event_id="evt-1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-123",
            payload={},
        )
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is False

    def test_matches_issue_comment_with_issue_number(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
            },
        )
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is True


class TestHandlePrdPrMerge:
    @pytest.mark.asyncio
    async def test_pr_merge_sets_approved(self, worker):
        msg = _make_message(
            "pull_request:closed",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7, "merged": True},
            },
        )
        state = _prd_gate_state(
            automated_review_revision_count=3,
            automated_review_revision_pending=True,
        )

        with patch("forge.orchestrator.worker.JiraClient") as MockJira:
            mock_jira = MagicMock()
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.close = AsyncMock()
            MockJira.return_value = mock_jira

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result["automated_review_revision_count"] == 0
        assert result["automated_review_revision_pending"] is False
        mock_jira.set_workflow_label.assert_called_once()

    @pytest.mark.asyncio
    async def test_pr_close_without_merge_is_ignored(self, worker):
        msg = _make_message(
            "pull_request:closed",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7, "merged": False},
            },
        )
        state = _prd_gate_state()

        result = await worker._handle_resume_event(msg, state)

        # Should remain paused -- closed without merge is not approval
        assert result.get("is_paused", True) is True


class TestHandlePrdPrReview:
    @pytest.mark.asyncio
    async def test_changes_requested_sets_feedback(self, worker):
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
                "review": {
                    "id": 101,
                    "state": "changes_requested",
                    "body": "Please add more detail",
                },
            },
        )
        state = _prd_gate_state()

        repo_ref = _repo_ref_for("org/proposals")
        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = []

        with _patch_adapter(repo_ref, mock_adapter):
            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "more detail" in result["feedback_comment"]
        mock_adapter.get_review_thread_comments.assert_called_once_with(
            repo_ref, identity_for(repo_ref, 7)
        )

    @pytest.mark.asyncio
    async def test_approved_review_is_ignored(self, worker):
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
                "review": {"state": "approved", "body": "LGTM"},
            },
        )
        state = _prd_gate_state()

        result = await worker._handle_resume_event(msg, state)

        # Should remain paused -- review approval is not an approval signal
        assert result.get("is_paused", True) is True

    @pytest.mark.asyncio
    async def test_mixed_threads_revise_accepts_and_reply_to_contested(self, worker):
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
                "review": {"id": 101, "state": "changes_requested", "body": "Mixed review"},
                "sender": {"login": "coderabbitai[bot]", "type": "Bot"},
            },
        )
        threads = [
            Review(
                id="accept-thread",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(
                        id="10", path="prd.md", line=10, body="Clarify authorization.", author=""
                    )
                ],
            ),
            Review(
                id="reply-thread",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(
                        id="20", path="prd.md", line=20, body="Rename the product.", author=""
                    )
                ],
            ),
        ]
        decisions = [
            {
                "thread_id": "accept-thread",
                "comment_id": 10,
                "disposition": "accept",
                "feedback": "Clarify authorization.",
                "response": "",
                "reason": "Valid",
            },
            {
                "thread_id": "reply-thread",
                "comment_id": 20,
                "disposition": "reply",
                "feedback": "",
                "response": "The product name is externally defined.",
                "reason": "Invalid",
            },
        ]
        state = _prd_gate_state(prd_content="# Current PRD")

        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = threads

        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch(
                "forge.orchestrator.worker.triage_proposal_review_threads",
                new=AsyncMock(return_value=decisions),
            ),
            patch(
                "forge.orchestrator.worker.reply_to_proposal_decisions",
                new=AsyncMock(),
            ) as reply_decisions,
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Clarify authorization."
        assert result["proposal_review_decisions"] == decisions
        reply_decisions.assert_awaited_once()


class TestHandlePrdPrComment:
    @pytest.mark.asyncio
    async def test_comment_sets_feedback(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {
                    "body": "!Please expand the scope section",
                    "user": {"login": "reviewer"},
                },
                "sender": {"login": "reviewer"},
            },
        )
        state = _prd_gate_state(
            automated_review_revision_count=2,
            automated_review_revision_pending=True,
        )

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter):
            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "scope section" in result["feedback_comment"]
        assert result["automated_review_revision_count"] == 0
        assert result["automated_review_revision_pending"] is False

    @pytest.mark.asyncio
    async def test_self_comment_is_ignored(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {
                    "body": "PRD has been revised based on feedback.",
                    "user": {"login": "forge-bot"},
                },
                "sender": {"login": "forge-bot"},
            },
        )
        state = _prd_gate_state()

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter):
            result = await worker._handle_resume_event(msg, state)

        # Should remain paused -- self-comment ignored
        assert result.get("is_paused", True) is True

    @pytest.mark.asyncio
    async def test_self_comment_with_signature_is_ignored(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {
                    "body": "<!-- my-signature -->\n\nSome automated message.",
                    "user": {"login": "forge-bot"},
                },
                "sender": {"login": "forge-bot"},
            },
        )
        state = _prd_gate_state()
        settings = MagicMock(forge_bot_comment_prefix="my-signature")

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
        ):
            result = await worker._handle_resume_event(msg, state)

        # Should remain paused -- self-comment with signature ignored
        assert result.get("is_paused", True) is True

    @pytest.mark.asyncio
    async def test_own_comment_without_signature_is_not_ignored(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {
                    "body": "!This is a comment without signature, treated as human comment.",
                    "user": {"login": "forge-bot"},
                },
                "sender": {"login": "forge-bot"},
            },
        )
        state = _prd_gate_state()
        settings = MagicMock(forge_bot_comment_prefix="my-signature")

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
        ):
            result = await worker._handle_resume_event(msg, state)

        # Should be processed and no longer paused
        assert result.get("is_paused") is False

    @pytest.mark.asyncio
    async def test_question_comment_sets_question_flag(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {
                    "body": "?Why did you choose REST over GraphQL?",
                    "user": {"login": "reviewer"},
                },
                "sender": {"login": "reviewer"},
            },
        )
        state = _prd_gate_state()

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter):
            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result.get("is_question") is True
        assert "REST" in result["feedback_comment"]

    @pytest.mark.asyncio
    async def test_inline_reply_resumes_only_matching_proposal_thread(self, worker):
        msg = _make_message(
            "pull_request_review_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
                "comment": {
                    "id": 12,
                    "in_reply_to_id": 11,
                    "path": "prd.md",
                    "line": 20,
                    "body": "Please make this change after all.",
                },
                "sender": {"login": "reviewer"},
            },
        )
        state = _prd_gate_state(
            proposal_review_decisions=[
                {
                    "thread_id": "thread-a",
                    "comment_id": 10,
                    "forge_reply_id": 11,
                    "disposition": "reply",
                    "feedback": "",
                    "response": "This conflicts with the API.",
                },
                {
                    "thread_id": "thread-b",
                    "comment_id": 20,
                    "disposition": "reply",
                    "feedback": "",
                    "response": "This is out of scope.",
                },
            ]
        )

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter):
            result = await worker._handle_resume_event(msg, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please make this change after all."
        assert result["proposal_review_decisions"][0]["disposition"] == "accept"
        assert result["proposal_review_decisions"][0]["comment_id"] == 12
        assert result["proposal_review_decisions"][1] == state["proposal_review_decisions"][1]

    @pytest.mark.asyncio
    async def test_unknown_proposal_reply_target_is_ignored(self, worker, caplog):
        msg = _make_message(
            "pull_request_review_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
                "comment": {
                    "id": 31,
                    "in_reply_to_id": 999,
                    "path": "prd.md",
                    "line": 5,
                    "body": "This target is not in workflow state.",
                },
                "sender": {"login": "reviewer"},
            },
        )
        state = _prd_gate_state(
            proposal_review_decisions=[
                {
                    "thread_id": "known-thread",
                    "comment_id": 10,
                    "disposition": "reply",
                    "feedback": "",
                    "response": "This conflicts with the API.",
                }
            ]
        )

        caplog.set_level("DEBUG", logger="forge.orchestrator.worker")
        with patch.object(
            worker,
            "_get_forge_github_login",
            new=AsyncMock(return_value="forge-bot"),
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result == state
        assert "Proposal reply target 999 did not match" in caplog.text

    @pytest.mark.asyncio
    async def test_standalone_inline_proposal_comment_is_triaged(self, worker):
        msg = _make_message(
            "pull_request_review_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
                "comment": {
                    "id": 30,
                    "path": "prd.md",
                    "line": 12,
                    "body": "Clarify the authorization behavior.",
                    "commit_id": "abc123",
                },
                "sender": {"login": "coderabbitai[bot]", "type": "Bot"},
            },
        )
        decision = {
            "thread_id": "comment-30",
            "comment_id": 30,
            "disposition": "accept",
            "feedback": "Clarify the authorization behavior.",
            "response": "",
            "reason": "Valid",
        }
        state = _prd_gate_state(prd_content="# Current PRD")

        with (
            patch.object(
                worker, "_get_forge_github_login", new=AsyncMock(return_value="forge-bot")
            ),
            patch(
                "forge.orchestrator.worker.triage_proposal_review_threads",
                new=AsyncMock(return_value=[decision]),
            ) as triage,
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Clarify the authorization behavior."
        assert result["proposal_review_decisions"] == [decision]
        triage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_satisfied_bot_review_stays_paused(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {"body": "!Score: 10/10. Verdict: PASS. Suggestions follow."},
                "sender": {"login": "reviewer[bot]", "type": "Bot"},
            },
        )
        state = _prd_gate_state(prd_content="# Current PRD")

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch(
                "forge.orchestrator.worker.triage_automated_review",
                new=AsyncMock(
                    return_value=AutomatedReviewDecision(
                        "satisfied", reason="The overall review passes"
                    )
                ),
            ) as triage,
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result == state
        triage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocking_bot_review_requests_bounded_revision(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {"body": "!The authorization requirement is missing."},
                "sender": {"login": "reviewer[bot]", "type": "Bot"},
            },
        )
        state = _prd_gate_state(prd_content="# Current PRD")

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch(
                "forge.orchestrator.worker.triage_automated_review",
                new=AsyncMock(
                    return_value=AutomatedReviewDecision(
                        "blocking",
                        blocking_feedback="Add the missing authorization requirement.",
                        reason="Acceptance is blocked",
                    )
                ),
            ),
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Add the missing authorization requirement."
        assert result.get("automated_review_revision_count", 0) == 0
        assert result["automated_review_revision_pending"] is True

    @pytest.mark.asyncio
    async def test_uncertain_bot_review_revises_with_original_feedback(self, worker):
        original_feedback = "!The result may still need changes, but the verdict is unclear."
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {"body": original_feedback},
                "sender": {"login": "reviewer[bot]", "type": "Bot"},
            },
        )
        state = _prd_gate_state(prd_content="# Current PRD")

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch(
                "forge.orchestrator.worker.triage_automated_review",
                new=AsyncMock(
                    return_value=AutomatedReviewDecision(
                        "uncertain", reason="The disposition is contradictory"
                    )
                ),
            ),
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result["revision_requested"] is True
        assert "The result may still need changes" in result["feedback_comment"]
        assert result.get("automated_review_revision_count", 0) == 0
        assert result["automated_review_revision_pending"] is True

    @pytest.mark.asyncio
    async def test_bot_review_at_revision_cap_stays_paused(self, worker):
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {"body": "!Another blocking request."},
                "sender": {"login": "reviewer[bot]", "type": "Bot"},
            },
        )
        state = _prd_gate_state(automated_review_revision_count=3)

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch(
                "forge.orchestrator.worker.triage_automated_review",
                new=AsyncMock(
                    return_value=AutomatedReviewDecision(
                        "blocking", blocking_feedback="Revise again."
                    )
                ),
            ),
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result == state


class TestJiraCommentIgnoredInPrMode:
    @pytest.mark.asyncio
    async def test_jira_comment_ignored_when_prd_pr_exists(self, worker):
        """Jira comments should not trigger feedback when PRD review is on GitHub PR."""
        msg = QueueMessage(
            message_id="msg-jira-1",
            event_id="evt-jira-1",
            source=EventSource.JIRA,
            event_type="issue_comment_created",
            ticket_key="TEST-123",
            payload={
                "comment": {
                    "body": "This is a Jira comment that should be ignored",
                },
                "changelog": {"items": []},
                "issue": {"fields": {"labels": ["forge:managed", "forge:prd-pending"]}},
            },
        )
        state = _prd_gate_state()

        result = await worker._handle_resume_event(msg, state)

        # Should remain paused — Jira comment ignored in PR mode
        assert result.get("is_paused", True) is True
        assert result.get("revision_requested") is not True

    @pytest.mark.asyncio
    async def test_jira_comment_processed_when_no_prd_pr(self, worker):
        """Jira comments with ! prefix should still work in normal Jira-only mode."""
        msg = QueueMessage(
            message_id="msg-jira-2",
            event_id="evt-jira-2",
            source=EventSource.JIRA,
            event_type="issue_comment_created",
            ticket_key="TEST-123",
            payload={
                "comment": {
                    "body": "!Please expand the scope section",
                },
                "changelog": {"items": []},
                "issue": {"fields": {"labels": ["forge:managed", "forge:prd-pending"]}},
            },
        )
        # No prd_pr_number — Jira-only mode
        state = _prd_gate_state(prd_pr_number=None, prd_pr_repo=None)

        result = await worker._handle_resume_event(msg, state)

        # Should process the comment as feedback
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "scope section" in result["feedback_comment"]


class TestInformationalCommentIgnored:
    @pytest.mark.asyncio
    async def test_plain_comment_on_prd_pr_is_ignored(self, worker):
        """Comments without ! or ? prefix on PRD PRs should be ignored."""
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {
                    "body": "Looks good to me",
                    "user": {"login": "reviewer"},
                },
                "sender": {"login": "reviewer"},
            },
        )
        state = _prd_gate_state()

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter):
            result = await worker._handle_resume_event(msg, state)

        assert result.get("is_paused", True) is True
        assert result.get("revision_requested") is not True

    @pytest.mark.asyncio
    async def test_bot_informational_comment_on_prd_pr_is_ignored(self, worker):
        """Bot comments without ! prefix (e.g. Prow, APPROVALNOTIFIER) should be ignored."""
        msg = _make_message(
            "issue_comment:created",
            {
                "repository": {"full_name": "org/proposals"},
                "issue": {"number": 7},
                "comment": {
                    "body": "@user: changing LGTM is restricted to collaborators",
                    "user": {"login": "openshift-ci[bot]"},
                },
                "sender": {"login": "openshift-ci[bot]", "type": "Bot"},
            },
        )
        state = _prd_gate_state()

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter):
            result = await worker._handle_resume_event(msg, state)

        assert result.get("is_paused", True) is True
        assert result.get("revision_requested") is not True


class TestHumanReviewSkipsTriage:
    @pytest.mark.asyncio
    async def test_human_review_bypasses_triage(self, worker):
        """Human pull_request_review should skip triage and always trigger revision."""
        msg = _make_message(
            "pull_request_review:submitted",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 7},
                "review": {"id": 201, "state": "changes_requested", "body": ""},
                "sender": {"login": "reviewer", "type": "User"},
            },
        )
        threads = [
            Review(
                id="thread-1",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(
                        id="100", path="prd.md", line=10, body="Fix this section.", author=""
                    )
                ],
            ),
        ]
        state = _prd_gate_state(prd_content="# Current PRD")

        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = threads

        with (
            _patch_adapter(_repo_ref_for("org/proposals"), mock_adapter),
            patch(
                "forge.orchestrator.worker.triage_proposal_review_threads",
                new=AsyncMock(),
            ) as triage,
        ):
            result = await worker._handle_resume_event(msg, state)

        assert result["revision_requested"] is True
        assert "Fix this section" in result["feedback_comment"]
        triage.assert_not_awaited()


class TestPrdPrReviewTypedFields:
    """Brief Step 1: the PRD-PR review/merge branches read typed event fields."""

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
            "current_node": "prd_approval_gate",
            "is_paused": True,
            "prd_pr_number": 42,
            "prd_pr_repo": "acme/payments",
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
            "current_node": "prd_approval_gate",
            "is_paused": True,
            "prd_pr_number": 42,
            "prd_pr_repo": "acme/payments",
        }

        with patch("forge.orchestrator.worker.JiraClient") as MockJira:
            MockJira.return_value.set_workflow_label = AsyncMock()
            MockJira.return_value.close = AsyncMock()
            updated = await worker._handle_resume_event(message, current_state)

        assert updated["is_paused"] is False


class TestProposalReplyTypedFields:
    """The inline proposal-reply block reads typed ReviewComment fields.

    An inline pull_request_review_comment is distinguished from an issue comment
    by comment.path being set; sender identity comes from actor.login.
    """

    def _reply_message(self, comment: ReviewComment, actor_login: str = "reviewer") -> QueueMessage:
        event = _make_normalized_event(kind=EventKind.COMMENT_CREATED)
        event.actor = Actor(login=actor_login, is_bot="[bot]" in actor_login)
        event.comment = comment
        return QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

    def _state(self, **overrides) -> dict:
        base = {
            "current_node": "prd_approval_gate",
            "is_paused": True,
            "prd_pr_number": 42,
            "prd_pr_repo": "acme/payments",
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_reply_matching_stored_decision_updates_and_unpauses(self, worker):
        message = self._reply_message(
            ReviewComment(
                id="12",
                body="Please make this change after all.",
                author="reviewer",
                path="prd.md",
                line=20,
                in_reply_to="11",
            )
        )
        state = self._state(
            proposal_review_decisions=[
                {
                    "thread_id": "thread-a",
                    "comment_id": 10,
                    "forge_reply_id": 11,
                    "disposition": "reply",
                    "feedback": "",
                    "response": "This conflicts with the API.",
                },
                {
                    "thread_id": "thread-b",
                    "comment_id": 20,
                    "disposition": "reply",
                    "feedback": "",
                    "response": "Out of scope.",
                },
            ]
        )

        with patch.object(
            worker, "_get_forge_github_login", new=AsyncMock(return_value="forge-bot")
        ):
            result = await worker._handle_resume_event(message, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please make this change after all."
        assert result["proposal_review_decisions"][0]["disposition"] == "accept"
        assert result["proposal_review_decisions"][0]["comment_id"] == 12
        assert result["proposal_review_decisions"][1] == state["proposal_review_decisions"][1]

    @pytest.mark.asyncio
    async def test_standalone_reply_builds_thread_and_sets_rejection(self, worker):
        # A human (non-bot) inline comment with no in_reply_to builds a fresh
        # proposal_review_threads entry and requests a revision; the bot-only
        # triage blocks are skipped for a human sender.
        message = self._reply_message(
            ReviewComment(
                id="30",
                body="Clarify the authorization behavior.",
                author="reviewer",
                path="prd.md",
                line=12,
            ),
            actor_login="reviewer",
        )
        state = self._state(prd_content="# Current PRD")

        with patch.object(
            worker, "_get_forge_github_login", new=AsyncMock(return_value="forge-bot")
        ):
            result = await worker._handle_resume_event(message, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Clarify the authorization behavior."

    @pytest.mark.asyncio
    async def test_self_reply_is_ignored(self, worker):
        message = self._reply_message(
            ReviewComment(
                id="99",
                body="Addressed in the latest revision.",
                author="forge-bot",
                path="prd.md",
                line=3,
                in_reply_to="11",
            ),
            actor_login="forge-bot",
        )
        state = self._state(
            proposal_review_decisions=[
                {"thread_id": "thread-a", "comment_id": 10, "forge_reply_id": 11}
            ]
        )

        with patch.object(
            worker, "_get_forge_github_login", new=AsyncMock(return_value="forge-bot")
        ):
            result = await worker._handle_resume_event(message, state)

        assert result == state
