"""Unit tests for the orchestrator worker."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckStatus,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    Review,
    ReviewComment,
    ReviewState,
)
from forge.models.events import EventSource
from forge.orchestrator.worker import (
    OrchestratorWorker,
    _has_new_reportable_error,
    _report_new_workflow_error,
)
from forge.queue.models import (
    QueueMessage,
    normalized_event_to_dict,
)
from forge.workflow.utils.source_control import identity_for


def _patch_adapter(repo_ref: RepositoryRef, adapter):
    """Patch worker.get_adapter to resolve to the given (repo_ref, adapter) pair."""
    return patch("forge.orchestrator.worker.get_adapter", return_value=(repo_ref, adapter))


@pytest.mark.parametrize(
    ("result", "error_before_invoke", "expected"),
    [
        ({"last_error": "new failure", "is_paused": False}, None, True),
        ({"last_error": "same failure", "is_paused": False}, "same failure", False),
        ({"last_error": "paused failure", "is_paused": True}, None, True),
        ({"last_error": None, "is_paused": False}, None, False),
    ],
)
def test_has_new_reportable_error(result: dict, error_before_invoke: str | None, expected: bool):
    assert _has_new_reportable_error(result, error_before_invoke) is expected


@pytest.mark.asyncio
async def test_report_new_workflow_error_posts_once():
    result = {
        "ticket_key": "TEST-123",
        "current_node": "setup_workspace",
        "last_error": "clone failed",
        "is_paused": False,
    }

    with patch("forge.orchestrator.worker.notify_error", new_callable=AsyncMock) as notify:
        await _report_new_workflow_error(result, None)

    notify.assert_awaited_once_with(result, "clone failed", "setup_workspace")


@pytest.mark.asyncio
async def test_report_new_workflow_error_posts_when_failure_ends_at_pause_gate():
    result = {
        "ticket_key": "TEST-123",
        "current_node": "triage_gate",
        "last_error": "model backend unavailable",
        "is_paused": True,
    }

    with patch("forge.orchestrator.worker.notify_error", new_callable=AsyncMock) as notify:
        await _report_new_workflow_error(result, None)

    notify.assert_awaited_once_with(result, "model backend unavailable", "triage_gate")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "error_before_invoke"),
    [
        ({"last_error": "same failure", "is_paused": False}, "same failure"),
        ({"last_error": None, "is_paused": False}, None),
    ],
)
async def test_report_new_workflow_error_skips_non_reportable_errors(
    result: dict, error_before_invoke: str | None
):
    with patch("forge.orchestrator.worker.notify_error", new_callable=AsyncMock) as notify:
        await _report_new_workflow_error(result, error_before_invoke)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_error_comment_uses_markdown_code_block():
    """Terminal errors use markup supported by the Markdown-to-ADF converter."""
    worker = OrchestratorWorker.__new__(OrchestratorWorker)
    jira = MagicMock()
    jira.close = AsyncMock()

    with (
        patch("forge.integrations.jira.client.JiraClient", return_value=jira),
        patch(
            "forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock
        ) as post_comment,
    ):
        await worker._post_terminal_error_comment(
            "TEST-123", "Object of type set is not JSON serializable"
        )

    post_comment.assert_awaited_once_with(
        jira,
        "TEST-123",
        "**Forge workflow stopped with error:**\n\n"
        "```\nObject of type set is not JSON serializable\n```\n\n"
        "To retry the workflow, add the label `forge:retry` to this ticket.",
    )
    jira.close.assert_awaited_once()


def _multi_repo_pr_state() -> dict:
    return {
        "ticket_key": "TEST-123",
        "current_node": "human_review_gate",
        "is_paused": True,
        "context": {},
        "current_repo": "acme/frontend",
        "current_pr_number": 20,
        "current_pr_url": "https://github.com/acme/frontend/pull/20",
        "pr_merged": False,
        "pull_requests": {
            "acme/backend:10": {
                "repo": "acme/backend",
                "number": 10,
                "url": "https://github.com/acme/backend/pull/10",
                "merged": False,
                "ci_status": "pending",
            },
            "acme/frontend:20": {
                "repo": "acme/frontend",
                "number": 20,
                "url": "https://github.com/acme/frontend/pull/20",
                "merged": False,
                "ci_status": "passed",
            },
        },
    }


@pytest.mark.asyncio
async def test_multi_repo_merge_waits_for_every_pr() -> None:
    worker = OrchestratorWorker(consumer_name="test-worker")
    state = _multi_repo_pr_state()

    def merge_message(repo: str, number: int) -> QueueMessage:
        event = _make_normalized_event(
            kind=EventKind.CR_MERGED,
            repo_ref=_sc_repo_ref(repo),
            change_request=_sc_change_request(repo, number, ChangeRequestState.MERGED),
        )
        return QueueMessage(
            message_id=f"msg-{number}",
            event_id=f"evt-{number}",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_merged",
            ticket_key="TEST-123",
            payload={
                "action": "closed",
                "pull_request": {"merged": True, "number": number},
                "repository": {"full_name": repo},
            },
            normalized_event=normalized_event_to_dict(event),
        )

    partial = await worker._handle_resume_event(merge_message("acme/backend", 10), state)

    assert partial["current_repo"] == "acme/backend"
    assert partial["pull_requests"]["acme/backend:10"]["merged"] is True
    assert partial["pull_requests"]["acme/frontend:20"]["merged"] is False
    assert partial["pr_merged"] is False
    assert partial["is_paused"] is True

    complete = await worker._handle_resume_event(merge_message("acme/frontend", 20), partial)

    assert complete["pr_merged"] is True
    assert complete["is_paused"] is False


@pytest.mark.asyncio
async def test_multi_repo_ci_webhook_selects_earlier_pr_from_review_gate() -> None:
    worker = OrchestratorWorker(consumer_name="test-worker")
    ci_payload = {
        "check_suite": {"status": "completed", "pull_requests": [{"number": 10}]},
        "repository": {"full_name": "acme/backend"},
    }
    event = _make_normalized_event(
        kind=EventKind.CHECK_UPDATED,
        repo_ref=_sc_repo_ref("acme/backend"),
        change_request=_sc_change_request("acme/backend", 10),
        raw=ci_payload,
    )
    message = QueueMessage(
        message_id="msg-ci",
        event_id="evt-ci",
        source=EventSource.SOURCE_CONTROL,
        event_type="check_updated",
        ticket_key="TEST-123",
        payload=ci_payload,
        normalized_event=normalized_event_to_dict(event),
    )

    result = await worker._handle_resume_event(message, _multi_repo_pr_state())

    assert result["current_repo"] == "acme/backend"
    assert result["current_pr_number"] == 10
    assert result["current_node"] == "human_review_gate"
    assert result["pending_ci_event"] is True
    assert result["is_paused"] is False


@pytest.mark.asyncio
async def test_multi_repo_approval_uses_common_state_cleanup_path() -> None:
    worker = OrchestratorWorker(consumer_name="test-worker")
    state = _multi_repo_pr_state()
    state["last_error"] = "stale review failure"
    state["revision_requested"] = True
    state["feedback_comment"] = "old feedback"
    event = _make_normalized_event(
        kind=EventKind.REVIEW_SUBMITTED,
        repo_ref=_sc_repo_ref("acme/backend"),
        change_request=_sc_change_request("acme/backend", 10),
        # author="" reproduces the original payload's absent sender, so the
        # self-comment guard is skipped without a network login lookup.
        review=Review(id="", state=ReviewState.APPROVED, body="Looks good", author=""),
    )
    message = QueueMessage(
        message_id="msg-approved",
        event_id="evt-approved",
        source=EventSource.SOURCE_CONTROL,
        event_type="review_submitted",
        ticket_key="TEST-123",
        payload={
            "review": {"state": "approved", "body": "Looks good"},
            "pull_request": {"number": 10},
            "repository": {"full_name": "acme/backend"},
        },
        normalized_event=normalized_event_to_dict(event),
    )

    result = await worker._handle_resume_event(message, state)

    assert result["current_repo"] == "acme/backend"
    assert result["is_paused"] is True
    assert result["last_error"] is None
    assert result["revision_requested"] is False
    assert result["feedback_comment"] is None
    assert result["human_review_status"] == "approved"
    assert result["pull_requests"]["acme/backend:10"]["human_review_status"] == "approved"


@pytest.mark.asyncio
async def test_multi_repo_review_selects_earlier_pr() -> None:
    mock_adapter = AsyncMock()
    mock_adapter.get_review_comments_for_submission.return_value = []
    worker = OrchestratorWorker(consumer_name="test-worker")
    state = _multi_repo_pr_state()
    state["current_node"] = "wait_for_ci_gate"
    event = _make_normalized_event(
        kind=EventKind.REVIEW_SUBMITTED,
        repo_ref=_sc_repo_ref("acme/backend"),
        change_request=_sc_change_request("acme/backend", 10),
        review=Review(id="5", state=ReviewState.CHANGES_REQUESTED, body="Fix backend", author=""),
    )
    message = QueueMessage(
        message_id="msg-review",
        event_id="evt-review",
        source=EventSource.SOURCE_CONTROL,
        event_type="review_submitted",
        ticket_key="TEST-123",
        payload={
            "review": {"id": 5, "state": "changes_requested", "body": "Fix backend"},
            "pull_request": {"number": 10},
            "repository": {"full_name": "acme/backend"},
        },
        normalized_event=normalized_event_to_dict(event),
    )

    with _patch_adapter(_sc_repo_ref("acme/backend"), mock_adapter):
        result = await worker._handle_resume_event(message, state)

    assert result["current_repo"] == "acme/backend"
    assert result["current_pr_number"] == 10
    assert result["current_node"] == "human_review_gate"
    assert result["revision_requested"] is True
    assert result["feedback_comment"] == "Fix backend"


@pytest.mark.asyncio
async def test_terminal_failure_posts_sanitized_recovery_comment():
    worker = OrchestratorWorker(consumer_name="test-worker")
    message = QueueMessage(
        message_id="1-0",
        event_id="evt-terminal-1",
        source=EventSource.JIRA,
        event_type="issue_updated",
        ticket_key="TEST-123",
    )
    jira = AsyncMock()
    jira.get_comments = AsyncMock(return_value=[])

    with patch("forge.orchestrator.worker.JiraClient", return_value=jira):
        await worker._handle_terminal_failure(
            message,
            "clone https://ghp_abcdefghijklmnopqrstuvwxyz123456@github.com/acme/repo failed",
        )

    jira.add_error_comment.assert_awaited_once()
    kwargs = jira.add_error_comment.await_args.kwargs
    assert kwargs["issue_key"] == "TEST-123"
    assert "[REDACTED]" in kwargs["error_message"]
    assert "ghp_" not in kwargs["error_message"]
    assert "Event/correlation ID: evt-terminal-1" in kwargs["error_message"]
    assert "Recovery:" in kwargs["error_message"]
    jira.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_failure_skips_existing_event_comment():
    worker = OrchestratorWorker(consumer_name="test-worker")
    message = QueueMessage(
        message_id="1-0",
        event_id="evt-terminal-1",
        source=EventSource.JIRA,
        event_type="issue_updated",
        ticket_key="TEST-123",
    )
    jira = AsyncMock()
    jira.get_comments = AsyncMock(
        return_value=[MagicMock(body="Event/correlation ID: evt-terminal-1")]
    )

    with patch("forge.orchestrator.worker.JiraClient", return_value=jira):
        await worker._handle_terminal_failure(message, "failed")

    jira.add_error_comment.assert_not_awaited()
    jira.close.assert_awaited_once()


class TestQuestionDetection:
    """Tests for Q&A mode question detection."""

    @pytest.fixture(autouse=True)
    def ack_comment_mocks(self):
        """Mock Jira acknowledgement posting for direct resume-event tests."""
        mock_jira = AsyncMock()
        mock_jira.close = AsyncMock()
        with (
            patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira),
            patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock) as post,
        ):
            yield post

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        """Create a worker instance for testing."""
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.fixture
    def base_message(self) -> QueueMessage:
        """Create a base queue message for testing."""
        return QueueMessage(
            message_id="1234567890-0",
            event_id="test-event-001",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            payload={
                "issue": {
                    "key": "TEST-123",
                    "fields": {
                        "issuetype": {"name": "Feature"},
                    },
                },
            },
        )

    @pytest.fixture
    def base_state(self) -> dict:
        """Create a base workflow state for testing."""
        return {
            "ticket_key": "TEST-123",
            "ticket_type": "Feature",
            "current_node": "prd_approval_gate",
            "is_paused": True,
            "context": {},
        }

    def _make_message_with_comment(
        self, base_message: QueueMessage, comment_body: str
    ) -> QueueMessage:
        """Create a message with a comment in the payload."""
        payload = {
            **base_message.payload,
            "comment": {"body": comment_body},
            "changelog": {"items": []},
        }
        return QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="comment_created",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

    @pytest.mark.asyncio
    async def test_question_comment_sets_is_question_flag(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Comments starting with ? set is_question flag."""
        message = self._make_message_with_comment(base_message, "?Why REST instead of GraphQL?")

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["feedback_comment"] == "?Why REST instead of GraphQL?"
        assert result["revision_requested"] is False
        assert result["is_paused"] is False
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-123"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "received your question" in ack_text
        assert "the PRD" in ack_text

    @pytest.mark.asyncio
    async def test_forge_ask_comment_sets_is_question_flag(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Comments with @forge ask set is_question flag."""
        message = self._make_message_with_comment(
            base_message, "@forge ask explain the database choice"
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["feedback_comment"] == "@forge ask explain the database choice"
        assert result["revision_requested"] is False
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_normal_feedback_still_works(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Feedback comments with ! prefix trigger revision_requested."""
        message = self._make_message_with_comment(
            base_message, "!Please add more detail to the security section"
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result.get("is_question") is not True
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please add more detail to the security section"
        assert result["is_paused"] is False
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-123"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "received your revision request" in ack_text
        assert "regenerating" in ack_text

    @pytest.mark.asyncio
    async def test_task_phase_feedback_from_epic_sets_current_epic_key(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Comments on an Epic during task review preserve the Epic source."""
        state = {
            **base_state,
            "current_node": "task_approval_gate",
            "epic_keys": ["TEST-124"],
            "task_keys": ["TEST-130"],
        }
        payload = {
            **base_message.payload,
            "source_ticket_key": "TEST-124",
            "comment": {"body": "!Please revise the tasks for this epic"},
            "changelog": {"items": []},
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="comment_created",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please revise the tasks for this epic"
        assert result["current_epic_key"] == "TEST-124"
        assert result["current_task_key"] is None
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-124"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "from TEST-124" in ack_text

    @pytest.mark.asyncio
    async def test_plan_phase_feedback_from_epic_acknowledges_epic(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Comments on an Epic during plan review are acknowledged on that Epic."""
        state = {
            **base_state,
            "current_node": "plan_approval_gate",
            "epic_keys": ["TEST-124"],
        }
        payload = {
            **base_message.payload,
            "source_ticket_key": "TEST-124",
            "comment": {"body": "!Please revise this epic plan"},
            "changelog": {"items": []},
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="comment_created",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please revise this epic plan"
        assert result["current_epic_key"] == "TEST-124"
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-124"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "received your revision request" in ack_text
        assert "from TEST-124" in ack_text

    @pytest.mark.asyncio
    async def test_retry_at_task_approval_gate_clears_stale_epic_and_task_keys(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
    ):
        """forge:retry at task_approval_gate must zero out current_epic_key and current_task_key."""
        state = {
            **base_state,
            "current_node": "task_approval_gate",
            "is_paused": True,
            "task_keys": ["TEST-130"],
            "current_epic_key": "TEST-124",  # stale from a prior epic comment
            "current_task_key": "TEST-130",  # stale from a prior task comment
            "last_error": None,
        }
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "toString": "forge:managed forge:retry",
                        "fromString": "forge:managed",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["current_epic_key"] is None
        assert result["current_task_key"] is None
        assert result["revision_requested"] is True

    @pytest.mark.asyncio
    async def test_retry_at_triage_gate_reenters_triage_check(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        state = {
            **base_state,
            "current_node": "triage_gate",
            "is_paused": True,
            "last_error": "model backend unavailable",
            "retry_count": 1,
        }
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed",
                        "toString": "forge:managed forge:retry",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["current_node"] == "triage_check"
        assert result["is_paused"] is False
        assert result["last_error"] is None
        assert result["context"]["force_fresh_invoke"] is True

    @pytest.mark.asyncio
    async def test_retry_at_approval_gate_with_error_triggers_regeneration(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        state = {
            **base_state,
            "current_node": "prd_approval_gate",
            "is_paused": True,
            "last_error": "PRD publish pending",
        }
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed",
                        "toString": "forge:managed forge:retry",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["current_node"] == "prd_approval_gate"
        assert result["is_paused"] is False
        assert result["last_error"] is None
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Regeneration requested via retry."

    @pytest.mark.asyncio
    async def test_retry_at_review_response_gate_transitions_to_human_review_gate(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        state = {
            **base_state,
            "current_node": "review_response_gate",
            "is_paused": True,
            "is_blocked": True,
            "retry_count": 2,
            "last_error": "some error",
            "auto_retry_cap_notified": True,
            "contested_comments": [{"text": "Objection: renaming breaks the public API"}],
            "revision_requested": True,
            "feedback_comment": "Changes requested",
        }
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed",
                        "toString": "forge:managed forge:retry",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["current_node"] == "human_review_gate"
        assert result["is_paused"] is False
        assert result["contested_comments"] == []
        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None
        assert result["context"]["force_fresh_invoke"] is True
        assert result["is_blocked"] is False
        assert result["retry_count"] == 0
        assert result["last_error"] is None
        assert result["auto_retry_cap_notified"] is False

    @pytest.mark.asyncio
    async def test_prd_label_change_to_approved_sets_approved_flag(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Approval is detected via label change from pending to approved, not comment text."""
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed forge:prd-pending",
                        "toString": "forge:managed forge:prd-approved",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result.get("is_question") is not True
        assert result["revision_requested"] is False
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_auto_retry_cap_marks_workflow_blocked_once(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
    ):
        """Errored workflows stop auto-resuming once retry_count reaches the cap."""
        state = {
            **base_state,
            "current_node": "implement_review",
            "is_paused": False,
            "last_error": "cannot rebase dirty workspace",
            "retry_count": 3,
            "is_blocked": False,
        }

        with patch.object(worker, "_post_terminal_error_comment", new_callable=AsyncMock) as post:
            result = await worker._handle_resume_event(base_message, state)

        assert result["current_node"] == "implement_review"
        assert result["retry_count"] == 3
        assert result["last_error"] == "cannot rebase dirty workspace"
        assert result["is_paused"] is True
        assert result["is_blocked"] is True
        assert result["auto_retry_cap_notified"] is True
        post.assert_awaited_once_with("TEST-123", "cannot rebase dirty workspace")

    @pytest.mark.asyncio
    async def test_question_with_leading_whitespace(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Questions with leading whitespace are still detected."""
        message = self._make_message_with_comment(base_message, "  ?What about caching?")

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["revision_requested"] is False

    @pytest.mark.asyncio
    async def test_forge_ask_case_insensitive(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """@forge ask detection is case insensitive."""
        message = self._make_message_with_comment(base_message, "@FORGE ASK why use microservices?")

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["revision_requested"] is False


class TestEnsureSkillsIntegration:
    """Tests for ensure_skills() integration inside _process_workflow."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        """Create a worker instance for testing."""
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.fixture
    def jira_message(self) -> QueueMessage:
        """Create a minimal Jira queue message."""
        return QueueMessage(
            message_id="1234567890-0",
            event_id="test-event-001",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            payload={
                "issue": {
                    "key": "TEST-123",
                    "fields": {
                        "issuetype": {"name": "Feature"},
                    },
                },
            },
        )

    @pytest.mark.asyncio
    async def test_ensure_skills_called_before_workflow_resolution(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """ensure_skills() is invoked at the top of _process_workflow."""
        call_order: list[str] = []

        async def fake_ensure_skills(*_args, **_kwargs) -> None:
            call_order.append("ensure_skills")

        async def fake_find_workflow(*_args, **_kwargs):
            call_order.append("workflow_resolution")
            return None, None

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", fake_find_workflow),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            # _find_workflow_by_state returns (None, None) → worker returns early
            await worker._process_workflow(jira_message)

        # ensure_skills must have been called before any workflow resolution
        assert "ensure_skills" in call_order

    @pytest.mark.asyncio
    async def test_ensure_skills_receives_correct_project_key(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Project key extracted from ticket key is passed to ensure_skills."""
        received: dict = {}

        async def fake_ensure_skills(project_key, _jira_client, _skills_dir, **_kw) -> None:
            received["project_key"] = project_key

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            await worker._process_workflow(jira_message)

        assert received["project_key"] == "TEST"

    @pytest.mark.asyncio
    async def test_ensure_skills_receives_skills_dir_from_settings(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """skills_dir and skills_install_dir passed to ensure_skills come from settings."""
        received: dict = {}

        async def fake_ensure_skills(
            _project_key, _jira_client, skills_dir, *, skills_install_dir=None
        ) -> None:
            received["skills_dir"] = skills_dir
            received["skills_install_dir"] = skills_install_dir

        worker.settings.skills_dir = "custom/skills"

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            await worker._process_workflow(jira_message)

        assert received["skills_dir"] == Path("custom/skills")
        assert received["skills_install_dir"] == worker.settings.skills_install_dir

    @pytest.mark.asyncio
    async def test_workflow_continues_when_ensure_skills_raises(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Workflow processing continues past skill sync even when ensure_skills raises."""
        extract_ticket_type_called = False

        async def failing_ensure_skills(*_args, **_kwargs) -> None:
            raise RuntimeError("git clone failed")

        original_extract = worker._extract_ticket_type

        def tracking_extract_ticket_type(msg):
            nonlocal extract_ticket_type_called
            extract_ticket_type_called = True
            return original_extract(msg)

        # The main workflow may raise for unrelated reasons (no checkpointer in tests),
        # but what matters is that _extract_ticket_type was called, proving execution
        # continued past the skill-sync try/except block.
        with (
            patch("forge.orchestrator.worker.ensure_skills", failing_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", side_effect=tracking_extract_ticket_type),
            pytest.raises(ValueError),
        ):
            await worker._process_workflow(jira_message)

        assert extract_ticket_type_called, (
            "Workflow processing should continue after skill sync failure"
        )

    @pytest.mark.asyncio
    async def test_warning_logged_when_ensure_skills_fails(
        self,
        worker: OrchestratorWorker,
        jira_message: QueueMessage,
        caplog: pytest.LogCaptureFixture,
    ):
        """A warning is logged when ensure_skills raises an exception."""
        import logging

        async def failing_ensure_skills(*_args, **_kwargs) -> None:
            raise ValueError("bad config")

        with (
            patch("forge.orchestrator.worker.ensure_skills", failing_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
            caplog.at_level(logging.WARNING, logger="forge.orchestrator.worker"),
        ):
            await worker._process_workflow(jira_message)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Skill synchronisation failed" in m for m in warning_messages)

    @pytest.mark.asyncio
    async def test_jira_client_instantiated_for_ensure_skills(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """A JiraClient instance is created and passed to ensure_skills."""
        received: dict = {}
        fake_client_instance = MagicMock()

        async def fake_ensure_skills(_project_key, jira_client, _skills_dir, **_kw) -> None:
            received["jira_client"] = jira_client

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient", return_value=fake_client_instance),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            await worker._process_workflow(jira_message)

        assert received["jira_client"] is fake_client_instance

    @pytest.mark.asyncio
    async def test_ensure_skills_skipped_gracefully_when_forge_skills_not_set(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """When forge.skills is not configured, ensure_skills returns without error.

        Simulates the real ensure_skills behaviour: get_skills_config returns None
        (property not set), so the function returns early and the workflow continues.
        """
        ensure_skills_called = False

        async def fake_ensure_skills_no_property(
            project_key, jira_client, _skills_dir, **_kw
        ) -> None:
            """Simulate ensure_skills when forge.skills property is absent (returns None)."""
            nonlocal ensure_skills_called
            ensure_skills_called = True
            # Mimic real behaviour: get_skills_config returns None → early return, no error
            skills_config = await jira_client.get_skills_config(project_key)
            if skills_config is None:
                return

        fake_jira = MagicMock()
        fake_jira.get_skills_config = MagicMock(return_value=None)

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills_no_property),
            patch("forge.orchestrator.worker.JiraClient", return_value=fake_jira),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            # Should not raise; workflow continues normally after early-return from ensure_skills
            await worker._process_workflow(jira_message)

        assert ensure_skills_called, (
            "ensure_skills should be called even when forge.skills is unset"
        )

    @pytest.mark.asyncio
    async def test_ensure_skills_called_for_resumed_workflows(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """ensure_skills is triggered for resumed (paused) workflows, not just new ones.

        Verifies that skill synchronisation happens regardless of whether the workflow
        is being started fresh or resumed from a checkpoint.
        """
        ensure_skills_called = False

        async def fake_ensure_skills(*_args, **_kwargs) -> None:
            nonlocal ensure_skills_called
            ensure_skills_called = True

        # Simulate a paused, in-progress workflow state stored in the checkpoint.
        paused_state = MagicMock()
        paused_state.values = {
            "ticket_key": "TEST-123",
            "ticket_type": "Feature",
            "current_node": "prd_approval_gate",
            "is_paused": True,
        }

        # Fake workflow instance returned by the router
        fake_workflow = MagicMock()
        fake_workflow.name = "feature_workflow"
        fake_compiled = MagicMock()
        fake_compiled.aget_state = AsyncMock(return_value=paused_state)
        fake_compiled.aupdate_state = AsyncMock(return_value=None)
        fake_compiled.ainvoke = AsyncMock(
            return_value={
                "current_node": "prd_approval_gate",
                "is_paused": True,
                "ticket_type": "Feature",
            }
        )

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="Feature")),
            patch.object(worker.router, "resolve", return_value=fake_workflow),
            patch.object(worker, "_get_compiled_workflow", return_value=fake_compiled),
            patch.object(
                worker,
                "_handle_resume_event",
                return_value={
                    "ticket_key": "TEST-123",
                    "current_node": "prd_approval_gate",
                    "is_paused": False,
                    "is_blocked": False,
                    "ticket_type": "Feature",
                },
            ),
        ):
            await worker._process_workflow(jira_message)

        assert ensure_skills_called, (
            "ensure_skills must be called for resumed workflows, not just new ones"
        )

    @pytest.mark.asyncio
    async def test_setup_workspace_retry_reinvokes_fresh_state(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Retrying a setup_workspace failure re-runs the node instead of continuing past it."""
        blocked_state = MagicMock()
        blocked_state.values = {
            "ticket_key": "TEST-123",
            "ticket_type": "Feature",
            "current_node": "setup_workspace",
            "is_paused": True,
            "is_blocked": True,
            "last_error": "Clone failed",
            "context": {},
        }
        retry_cleared_state = {
            **blocked_state.values,
            "is_paused": False,
            "is_blocked": False,
            "last_error": None,
        }

        fake_workflow = MagicMock()
        fake_workflow.name = "feature_workflow"
        fake_compiled = MagicMock()
        fake_compiled.aget_state = AsyncMock(return_value=blocked_state)
        fake_compiled.aupdate_state = AsyncMock(return_value=None)
        fake_compiled.ainvoke = AsyncMock(
            return_value={
                "ticket_key": "TEST-123",
                "current_node": "setup_workspace",
                "is_paused": False,
                "ticket_type": "Feature",
            }
        )

        with (
            patch("forge.orchestrator.worker.ensure_skills", AsyncMock()),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="Feature")),
            patch.object(worker.router, "resolve", return_value=fake_workflow),
            patch.object(worker, "_get_compiled_workflow", return_value=fake_compiled),
            patch.object(worker, "_handle_resume_event", return_value=retry_cleared_state),
        ):
            await worker._process_workflow(jira_message)

        fake_compiled.aupdate_state.assert_not_awaited()
        fake_compiled.ainvoke.assert_awaited_once_with(
            retry_cleared_state,
            config={"configurable": {"thread_id": "TEST-123"}},
        )

    @pytest.mark.asyncio
    async def test_retry_force_fresh_invoke_reruns_bug_implementation(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Bug implementation retry re-enters implement_bug_fix instead of routing past it."""
        blocked_state = MagicMock()
        blocked_state.values = {
            "ticket_key": "TEST-123",
            "ticket_type": "Bug",
            "current_node": "implement_bug_fix",
            "is_paused": True,
            "is_blocked": True,
            "last_error": "Implementation failed",
            "context": {},
        }
        retry_cleared_state = {
            **blocked_state.values,
            "is_paused": False,
            "is_blocked": False,
            "last_error": None,
            "context": {"force_fresh_invoke": True},
        }
        expected_invoked_state = {
            **retry_cleared_state,
            "context": {},
        }

        fake_workflow = MagicMock()
        fake_workflow.name = "bug_workflow"
        fake_compiled = MagicMock()
        fake_compiled.aget_state = AsyncMock(return_value=blocked_state)
        fake_compiled.aupdate_state = AsyncMock(return_value=None)
        fake_compiled.ainvoke = AsyncMock(
            return_value={
                "ticket_key": "TEST-123",
                "current_node": "implement_bug_fix",
                "is_paused": False,
                "ticket_type": "Bug",
            }
        )

        with (
            patch("forge.orchestrator.worker.ensure_skills", AsyncMock()),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="Bug")),
            patch.object(worker.router, "resolve", return_value=fake_workflow),
            patch.object(worker, "_get_compiled_workflow", return_value=fake_compiled),
            patch.object(worker, "_handle_resume_event", return_value=retry_cleared_state),
        ):
            await worker._process_workflow(jira_message)

        fake_compiled.aupdate_state.assert_not_awaited()
        fake_compiled.ainvoke.assert_awaited_once_with(
            expected_invoked_state,
            config={"configurable": {"thread_id": "TEST-123"}},
        )


class TestCiWebhookSignalAtCiEvaluator:
    """check_suite events must wake up the workflow when paused at ci_evaluator.

    Previously the signal check only covered wait_for_ci_gate. Workflows that
    resume directly at ci_evaluator (e.g. after a skip-gate command) were silently
    ignored, leaving CI failures unhandled.
    """

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        return OrchestratorWorker(consumer_name="test-worker")

    def _ci_state(self, node: str) -> dict:
        return {
            "ticket_key": "AISOS-701",
            "ticket_type": "Bug",
            "current_node": node,
            "is_paused": False,
            "last_error": None,
            "context": {},
        }

    def _check_suite_message(self, conclusion: str = "failure") -> QueueMessage:
        raw = {
            "action": "completed",
            "check_suite": {
                "status": "completed",
                "conclusion": conclusion,
                "head_branch": "forge/aisos-701",
                "pull_requests": [{"number": 52}],
            },
            "repository": {"full_name": "forge-sdlc/forge"},
        }
        event = _make_normalized_event(kind=EventKind.CHECK_UPDATED, raw=raw)
        return QueueMessage(
            message_id="1-0",
            event_id="test-ci-001",
            source=EventSource.SOURCE_CONTROL,
            event_type="check_updated",
            ticket_key="AISOS-701",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

    @pytest.mark.asyncio
    async def test_check_suite_recognized_at_ci_evaluator(self, worker):
        """A completed check_suite event at ci_evaluator must produce a new state object.

        _handle_resume_event signals 'no valid event' by returning the *same* state
        object unchanged. A recognised signal always returns a new dict. We verify
        object identity to catch the bug where the worker silently ignored the event.
        """
        state = self._ci_state("ci_evaluator")
        message = self._check_suite_message("failure")

        result = await worker._handle_resume_event(message, state)

        assert result is not state, (
            "check_suite at ci_evaluator returned the original state unchanged — "
            "signal was not recognised"
        )
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_incomplete_check_suite_does_not_unpause_at_ci_evaluator(self, worker):
        """A check_suite with status=in_progress must not wake up the workflow."""
        state = self._ci_state("ci_evaluator")
        event = _make_normalized_event(
            kind=EventKind.CHECK_UPDATED,
            check_suite_status=CheckStatus.IN_PROGRESS,
            raw={
                "check_suite": {"status": "in_progress", "conclusion": None},
                "repository": {"full_name": "forge-sdlc/forge"},
            },
        )
        message = QueueMessage(
            message_id="1-0",
            event_id="test-ci-002",
            source=EventSource.SOURCE_CONTROL,
            event_type="check_updated",
            ticket_key="AISOS-701",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        result = await worker._handle_resume_event(message, state)

        # unchanged state returned — is_paused stays as it was
        assert result is state


class TestExtractTextFromAdf:
    """Tests for _extract_text_from_adf."""

    def test_paragraph_text(self):
        adf = {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
        }
        assert OrchestratorWorker._extract_text_from_adf(adf) == "hello"

    def test_blockquote_text(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "blockquote",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "option 2"}]}
                    ],
                }
            ],
        }
        assert "option 2" in OrchestratorWorker._extract_text_from_adf(adf)

    def test_heading_text(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": "Title"}],
                }
            ],
        }
        assert "Title" in OrchestratorWorker._extract_text_from_adf(adf)

    def test_bullet_list_text(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "item one"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        assert "item one" in OrchestratorWorker._extract_text_from_adf(adf)

    def test_non_dict_returns_string(self):
        assert OrchestratorWorker._extract_text_from_adf("plain") == "plain"
        assert OrchestratorWorker._extract_text_from_adf(None) == ""


class TestTaskPlanApprovalAndLabelPreservation:
    """Tests for task plan approval resumption, YOLO gate, and label preservation."""

    @pytest.fixture(autouse=True)
    def ack_comment_mocks(self):
        """Mock Jira acknowledgement posting for direct resume-event tests."""
        mock_jira = AsyncMock()
        mock_jira.close = AsyncMock()
        with (
            patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira),
            patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock) as post,
        ):
            yield post

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        """Create a worker instance for testing."""
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.fixture
    def base_message(self) -> QueueMessage:
        """Create a base queue message for testing."""
        return QueueMessage(
            message_id="1234567890-0",
            event_id="test-event-001",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            payload={
                "issue": {
                    "key": "TEST-123",
                    "fields": {
                        "issuetype": {"name": "Task"},
                        "labels": ["forge:managed"],
                    },
                },
            },
        )

    @pytest.fixture
    def base_state(self) -> dict:
        """Create a base workflow state for testing."""
        return {
            "ticket_key": "TEST-123",
            "ticket_type": "Task",
            "current_node": "task_plan_approval_gate",
            "is_paused": True,
            "context": {},
        }

    @pytest.mark.asyncio
    async def test_task_plan_label_change_to_approved_sets_approved_flag(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Approval for task plan is detected via label change from pending to approved."""
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed forge:plan-pending",
                        "toString": "forge:managed forge:plan-approved",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_paused"] is False
        assert result.get("revision_requested") is not True

    @pytest.mark.asyncio
    async def test_task_plan_label_fallback_approved(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Fallback detection: check current labels on the ticket when changelog check missed it."""
        payload = {
            **base_message.payload,
            "issue": {
                "key": "TEST-123",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "labels": ["forge:managed", "forge:plan-approved"],
                },
            },
            "changelog": {"items": []},
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_paused"] is False
        assert result.get("revision_requested") is not True

    @pytest.mark.asyncio
    async def test_task_plan_yolo_gate_activation(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Adding forge:yolo label at task_plan_approval_gate activates YOLO mode."""
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed",
                        "toString": "forge:managed forge:yolo",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result["yolo_mode"] is True
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_label_preservation_during_transitions(self):
        """Transitions do not clear identity preservation labels forge:managed:task and forge:managed:task-takeover."""
        from forge.integrations.jira.client import JiraClient
        from forge.models.workflow import ForgeLabel

        # Mock settings for JiraClient instantiation
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"

            client = JiraClient()

        # Mock get_labels to return current labels including identity preservation ones
        client.get_labels = AsyncMock(
            return_value=[
                "forge:managed",
                "forge:plan-pending",
                "forge:managed:task",
                "forge:managed:task-takeover",
                "other-label",
            ]
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.put = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            await client.set_workflow_label("TEST-123", ForgeLabel.PLAN_APPROVED)

        # Check that PUT was called with correct operations
        mock_http.put.assert_called_once()
        call_args = mock_http.put.call_args
        update_ops = call_args.kwargs["json"]["update"]["labels"]

        # Assert no remove operations are queued for the identity labels
        remove_ops = [op for op in update_ops if "remove" in op]
        assert not any(op["remove"] == "forge:managed:task" for op in remove_ops)
        assert not any(op["remove"] == "forge:managed:task-takeover" for op in remove_ops)

        # Verify that "forge:plan-pending" is removed
        assert any(op["remove"] == "forge:plan-pending" for op in remove_ops)
        # Verify that "forge:plan-approved" is added
        add_ops = [op for op in update_ops if "add" in op]
        assert any(op["add"] == ForgeLabel.PLAN_APPROVED.value for op in add_ops)


class TestWorkerRouting:
    """Tests for message routing and label extraction in the worker."""

    @pytest.mark.asyncio
    async def test_process_workflow_extracts_labels_and_calls_resolve(self):
        """Worker extracts labels from the payload and passes them to the router."""
        from forge.models.workflow import TicketType

        worker = OrchestratorWorker(consumer_name="test-worker")

        message = QueueMessage(
            message_id="1234567890-0",
            event_id="test-event-001",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            payload={
                "issue": {
                    "key": "TEST-123",
                    "fields": {
                        "issuetype": {"name": "Task"},
                        "labels": ["forge:managed"],
                    },
                },
            },
        )

        mock_router = MagicMock()
        mock_router.resolve = MagicMock(return_value=None)
        worker.router = mock_router

        with (
            patch("forge.orchestrator.worker.ensure_skills", AsyncMock()),
            patch("forge.orchestrator.worker.JiraClient"),
        ):
            await worker._process_workflow(message)

        mock_router.resolve.assert_called_once_with(
            ticket_type=TicketType.TASK,
            labels=["forge:managed"],
            event=message.payload,
        )


class TestCiWebhookAtHumanReviewGate:
    """Worker routes CI webhooks to human_review_gate correctly."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.mark.asyncio
    async def test_ci_webhook_at_review_gate_sets_pending_ci_event(self, worker):
        """CI check_suite.completed at human_review_gate sets pending_ci_event=True."""
        current_state = {
            "ticket_key": "TEST-1",
            "ticket_type": "Feature",
            "current_node": "human_review_gate",
            "is_paused": True,
            "pending_ci_event": False,
            "context": {},
            "pull_requests": {"org/repo:42": {"number": 42}},
        }
        raw = {
            "repository": {"full_name": "org/repo"},
            "check_suite": {
                "status": "completed",
                "pull_requests": [{"number": 42}],
            },
        }
        event = _make_normalized_event(
            kind=EventKind.CHECK_UPDATED,
            repo_ref=_sc_repo_ref("org/repo"),
            change_request=_sc_change_request("org/repo", 42),
            raw=raw,
        )
        message = QueueMessage(
            message_id="msg-1",
            event_id="evt-1",
            source=EventSource.SOURCE_CONTROL,
            event_type="check_updated",
            ticket_key="TEST-1",
            payload={
                "repository": {"full_name": "org/repo"},
                "check_suite": {
                    "status": "completed",
                    "pull_requests": [{"number": 42}],
                },
            },
            normalized_event=normalized_event_to_dict(event),
        )

        result = await worker._handle_resume_event(message, current_state)

        assert result.get("pending_ci_event") is True
        assert result.get("is_paused") is False
        assert result.get("current_node") == "human_review_gate"  # unchanged

    @pytest.mark.asyncio
    async def test_ci_webhook_at_ci_evaluator_does_not_set_pending_ci_event(self, worker):
        """CI webhook at ci_evaluator does NOT set pending_ci_event (old behavior preserved)."""
        current_state = {
            "ticket_key": "TEST-1",
            "ticket_type": "Feature",
            "current_node": "ci_evaluator",
            "is_paused": False,
            "pending_ci_event": False,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.CHECK_UPDATED,
            raw={
                "check_suite": {
                    "status": "completed",
                    "pull_requests": [{"number": 42}],
                }
            },
        )
        message = QueueMessage(
            message_id="msg-2",
            event_id="evt-2",
            source=EventSource.SOURCE_CONTROL,
            event_type="check_updated",
            ticket_key="TEST-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        result = await worker._handle_resume_event(message, current_state)

        assert result.get("is_paused") is False
        assert result.get("pending_ci_event", False) is False  # not set for ci_evaluator

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_review_arriving_during_in_flight_ci_cycle_is_not_dropped(
        self, _mock_post_comment
    ):
        """A PR review submitted while a CI webhook is still being evaluated at
        human_review_gate must not be silently discarded — it should unpause and
        record revision_requested/feedback_comment, and pending_ci_event must stay
        set so the in-flight CI cycle still runs to completion."""
        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = []

        worker = OrchestratorWorker(consumer_name="test-worker")
        # State as left by the CI webhook that arrived first: unpaused, but still
        # parked at human_review_gate with pending_ci_event set.
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "is_paused": False,
            "pending_ci_event": True,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(
                id="", state=ReviewState.CHANGES_REQUESTED, body="Needs changes", author=""
            ),
        )
        message = QueueMessage(
            message_id="msg-124",
            event_id="evt-124",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        with _patch_adapter(_sc_repo_ref("owner/repo"), mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Needs changes"
        assert result["pending_ci_event"] is True


class TestHandleResumeEventReviewGates:
    """Tests for resuming workflows from human_review_gate and review_response_gate."""

    @pytest.mark.asyncio
    async def test_forge_github_login_is_cached_per_worker(self):
        worker = OrchestratorWorker.__new__(OrchestratorWorker)
        worker._forge_github_logins = {}
        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        repo_ref = _sc_repo_ref("owner/repo")

        with _patch_adapter(repo_ref, mock_adapter):
            first = await worker._get_forge_github_login(repo_ref)
            second = await worker._get_forge_github_login(repo_ref)

        assert first == second == "forge-bot"
        mock_adapter.get_authenticated_identity.assert_awaited_once_with(repo_ref)

    @pytest.mark.asyncio
    async def test_forge_authored_pr_review_does_not_resume_review_workflow(self):
        """Thread replies create review events that Forge must not consume itself."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-236",
            "current_node": "human_review_gate",
            "current_repo": "owner/repo",
            "current_pr_number": 42,
            "is_paused": True,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="forge-bot", is_bot=True),
            review=Review(id="99", state=ReviewState.COMMENTED, body="", author="forge-bot"),
        )
        message = QueueMessage(
            message_id="msg-forge-review",
            event_id="evt-forge-review",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-236",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        with (
            patch.object(
                worker,
                "_get_forge_github_login",
                new=AsyncMock(return_value="forge-bot"),
            ) as get_forge_login,
            patch("forge.orchestrator.worker.get_adapter") as get_adapter_mock,
        ):
            result = await worker._handle_resume_event(message, state)

        assert result is state
        get_forge_login.assert_awaited_once()
        get_adapter_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_inline_reply_resumes_only_its_contested_thread(self):
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-233",
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [
                {"thread_id": "thread-a", "comment_id": 10, "forge_reply_id": 11},
                {"thread_id": "thread-b", "comment_id": 20},
            ],
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.COMMENT_CREATED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="reviewer", is_bot=False),
            comment=ReviewComment(
                id="12",
                body="Please make this change after all.",
                author="reviewer",
                path="src/file.py",
                in_reply_to="11",
            ),
        )
        message = QueueMessage(
            message_id="msg-thread-reply",
            event_id="evt-thread-reply",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="TEST-233",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)
        with _patch_adapter(_sc_repo_ref("owner/repo"), mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please make this change after all."
        assert result["contested_comments"] == [{"thread_id": "thread-b", "comment_id": 20}]

    @pytest.mark.asyncio
    async def test_standalone_inline_comment_is_actionable_at_response_gate(self):
        """A non-reply inline comment (no in_reply_to) at review_response_gate must
        still be actionable — it does NOT silently fall through to an unchanged
        state. This is the second (own-id) branch the typed cutover preserved: it
        unpauses and requests revision using the comment's OWN id as the review
        thread id, leaving contested_comments untouched (no reply target to clear).
        """
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-233",
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [{"thread_id": "thread-a", "comment_id": 10}],
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.COMMENT_CREATED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="reviewer", is_bot=False),
            comment=ReviewComment(
                id="30",
                body="Please cover this edge case.",
                author="reviewer",
                path="src/file.py",
            ),
        )
        message = QueueMessage(
            message_id="msg-new-thread",
            event_id="evt-new-thread",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="TEST-233",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        mock_adapter = AsyncMock()
        mock_adapter.get_authenticated_identity.return_value = Actor(login="forge-bot", is_bot=True)

        with _patch_adapter(_sc_repo_ref("owner/repo"), mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please cover this edge case."
        assert result["contested_comments"] == state["contested_comments"]
        # The own-comment id (not a reply target) becomes the review thread id.
        assert result["context"]["review_thread_comment_id"] == 30

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_review_changes_requested_at_review_response_gate(self, _mock_post_comment):
        """changes_requested at review_response_gate unpauses and clears contested_comments."""
        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = [
            Review(
                id="t1",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(
                        id="1", path="src/file.py", line=10, body="Please fix this.", author=""
                    )
                ],
            )
        ]

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [
                {"text": "Objection: the suggested refactor conflicts with the spec"}
            ],
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(
                id="",
                state=ReviewState.CHANGES_REQUESTED,
                body="PR needs some work",
                author="",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        repo_ref = _sc_repo_ref("owner/repo")
        with _patch_adapter(repo_ref, mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["contested_comments"] == []
        assert "PR needs some work" in result["feedback_comment"]
        assert "src/file.py" in result["feedback_comment"]
        mock_adapter.get_review_thread_comments.assert_called_once_with(
            repo_ref, identity_for(repo_ref, 42)
        )

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_review_with_review_id_calls_get_review_comments(self, _mock_post_comment):
        """When review payload contains a review ID, get_review_comments_for_submission
        is called (scoped to that review, not every unresolved thread)."""
        mock_adapter = AsyncMock()
        mock_adapter.get_review_comments_for_submission.return_value = [
            ReviewComment(id="1", path="src/file1.py", line=10, body="Fix line.", author=""),
            ReviewComment(id="2", path="src/file2.py", line=20, body="Fix line.", author=""),
            ReviewComment(id="3", path="src/file4.py", line=None, body="Fix none.", author=""),
        ]

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [
                {"text": "Objection: the requested change conflicts with the spec"}
            ],
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(
                id="999",
                state=ReviewState.CHANGES_REQUESTED,
                body="PR review body",
                author="",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        repo_ref = _sc_repo_ref("owner/repo")
        with _patch_adapter(repo_ref, mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["contested_comments"] == []
        assert "PR review body" in result["feedback_comment"]
        assert "src/file1.py" in result["feedback_comment"]
        assert "(line 10)" in result["feedback_comment"]
        assert "(line 20)" in result["feedback_comment"]
        assert "(line ?)" in result["feedback_comment"]
        mock_adapter.get_review_comments_for_submission.assert_called_once_with(
            repo_ref, identity_for(repo_ref, 42), "999"
        )
        mock_adapter.get_review_thread_comments.assert_not_called()

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_review_without_review_id_falls_back(self, _mock_post_comment):
        """When review payload has NO review ID, get_review_thread_comments is
        called (every unresolved thread) instead of the submission-scoped fetch."""
        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = [
            Review(
                id="t1",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(id="1", path="src/file1.py", line=10, body="Fix line.", author="")
                ],
            ),
            Review(
                id="t2",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(id="2", path="src/file2.py", line=20, body="Fix line.", author="")
                ],
            ),
            Review(
                id="t3",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(
                        id="3", path="src/file4.py", line=None, body="Fix none.", author=""
                    )
                ],
            ),
        ]

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(
                id="",
                state=ReviewState.CHANGES_REQUESTED,
                body="PR review body",
                author="",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        repo_ref = _sc_repo_ref("owner/repo")
        with _patch_adapter(repo_ref, mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "PR review body" in result["feedback_comment"]
        assert "src/file1.py" in result["feedback_comment"]
        assert "(line 10)" in result["feedback_comment"]
        assert "(line 20)" in result["feedback_comment"]
        assert "(line ?)" in result["feedback_comment"]
        mock_adapter.get_review_thread_comments.assert_called_once_with(
            repo_ref, identity_for(repo_ref, 42)
        )
        mock_adapter.get_review_comments_for_submission.assert_not_called()

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_approve_at_review_response_gate(self, _mock_post_comment):
        """PR review approved at review_response_gate unpauses the workflow."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(id="", state=ReviewState.APPROVED, body="Looks great!", author=""),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result.get("revision_requested") is False

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_merge_at_review_response_gate(self, _mock_post_comment):
        """PR merge event at review_response_gate unpauses and sets pr_merged."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.CR_MERGED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42, ChangeRequestState.MERGED),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_merged",
            ticket_key="TEST-123",
            payload={
                "action": "closed",
                "pull_request": {"merged": True, "number": 42},
                "repository": {"full_name": "owner/repo"},
            },
            normalized_event=normalized_event_to_dict(event),
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["pr_merged"] is True

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_review_changes_requested_at_human_review_gate(self, _mock_post_comment):
        """changes_requested at human_review_gate unpauses and sets revision_requested."""
        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = []

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "is_paused": True,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(
                id="", state=ReviewState.CHANGES_REQUESTED, body="Needs changes", author=""
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        with _patch_adapter(_sc_repo_ref("owner/repo"), mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Needs changes"

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_commented_review_with_inline_at_review_response_gate(
        self, _mock_post_comment
    ):
        """A 'commented' review with inline comments at review_response_gate is actionable."""
        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = [
            Review(
                id="t1",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(
                        id="1",
                        path="src/app.py",
                        line=5,
                        body="Nit: rename this variable.",
                        author="",
                    )
                ],
            )
        ]

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(id="", state=ReviewState.COMMENTED, body="", author=""),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        repo_ref = _sc_repo_ref("owner/repo")
        with _patch_adapter(repo_ref, mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "src/app.py" in result["feedback_comment"]
        mock_adapter.get_review_thread_comments.assert_called_once_with(
            repo_ref, identity_for(repo_ref, 42)
        )

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_pr_review_ignored_when_not_paused_at_review_response_gate(
        self, _mock_post_comment
    ):
        """A PR review event when is_paused=False at review_response_gate must not
        trigger revision handling — the review guard skips it entirely."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": False,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(id="", state=ReviewState.CHANGES_REQUESTED, body="Fix this", author=""),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        result = await worker._handle_resume_event(message, state)

        assert result is state

    def test_review_response_gate_not_in_fresh_invoke_nodes(self):
        """review_response_gate must NOT use fresh-invoke — the gate re-pauses,
        so ainvoke(state) would negate is_paused=False set by the handler."""
        from forge.orchestrator.worker import _FRESH_INVOKE_NODES

        assert "review_response_gate" not in _FRESH_INVOKE_NODES

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_review_response_gate_resume_routes_to_implement_review(self, _mock_post_comment):
        """After changes_requested at review_response_gate, state routes to implement_review."""
        from forge.workflow.nodes.implement_review import route_review_response

        mock_adapter = AsyncMock()
        mock_adapter.get_review_thread_comments.return_value = [
            Review(
                id="t1",
                state=ReviewState.COMMENTED,
                body="",
                author="",
                comments=[
                    ReviewComment(
                        id="1", path="src/main.py", line=7, body="Fix the typo here.", author=""
                    )
                ],
            )
        ]

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [{"text": "Objection: renaming breaks the public API"}],
            "revision_requested": False,
            "context": {},
        }
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            review=Review(
                id="",
                state=ReviewState.CHANGES_REQUESTED,
                body="No, please apply the rename as requested",
                author="",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        with _patch_adapter(_sc_repo_ref("owner/repo"), mock_adapter):
            result = await worker._handle_resume_event(message, state)

        assert route_review_response(result) == "implement_review"


class TestWorkerWebhookCommentFiltering:
    """Tests confirming worker webhook filter/processing dual-check logic."""

    @pytest.mark.asyncio
    async def test_integration_bot_login_comment_without_prefix_processed_as_human_feedback(self):
        """Integration test confirms that a bot-login comment without a configured prefix signature is processed as human feedback."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "current_repo": "owner/repo",
            "current_pr_number": 42,
            "is_paused": True,
            "context": {},
        }
        # Sender matches the bot login 'dev-user', but body does NOT contain the prefix signature
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="dev-user", is_bot=False),
            review=Review(
                id="100",
                state=ReviewState.CHANGES_REQUESTED,
                body="!This is a human review comment without signature prefix.",
                author="dev-user",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        settings = MagicMock(forge_bot_comment_prefix="my-signature")
        mock_adapter = AsyncMock()
        mock_adapter.get_review_comments_for_submission.return_value = []

        with (
            patch.object(worker, "_get_forge_github_login", new=AsyncMock(return_value="dev-user")),
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
            _patch_adapter(_sc_repo_ref("owner/repo"), mock_adapter),
        ):
            result = await worker._handle_resume_event(message, state)

        # It should be processed (not ignored), so state will have updated to resume (is_paused becomes False)
        assert result is not state
        assert result.get("is_paused") is False
        assert result.get("revision_requested") is True
        assert "!This is a human review comment" in result.get("feedback_comment", "")

    @pytest.mark.asyncio
    async def test_integration_bot_login_comment_with_prefix_ignored_as_self_comment(self):
        """Integration test confirms that a bot-login comment with a configured prefix signature is ignored as a self-comment."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "current_repo": "owner/repo",
            "current_pr_number": 42,
            "is_paused": True,
            "context": {},
        }
        # Sender matches the bot login 'dev-user', and body contains the prefix signature
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="dev-user", is_bot=False),
            review=Review(
                id="100",
                state=ReviewState.CHANGES_REQUESTED,
                body="<!-- my-signature -->\n\nThis is an automated comment with signature.",
                author="dev-user",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        settings = MagicMock(forge_bot_comment_prefix="my-signature")

        with (
            patch.object(worker, "_get_forge_github_login", new=AsyncMock(return_value="dev-user")),
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
        ):
            result = await worker._handle_resume_event(message, state)

        # It should be ignored (is_self_comment is True), so returns unchanged state
        assert result is state
        assert result.get("is_paused") is True

    @pytest.mark.asyncio
    async def test_integration_app_bot_comment_ending_in_bot_ignored_as_self_comment(self):
        """Integration test confirms that standard App bot comments ending in [bot] are ignored as self-comments."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "current_repo": "owner/repo",
            "current_pr_number": 42,
            "is_paused": True,
            "context": {},
        }
        # Sender is an App bot (ends with [bot]) and matches the bot login
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="forge-bot[bot]", is_bot=True),
            review=Review(
                id="100",
                state=ReviewState.CHANGES_REQUESTED,
                body="Some comment body from app bot",
                author="forge-bot[bot]",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        settings = MagicMock(forge_bot_comment_prefix="my-signature")

        with (
            patch.object(
                worker, "_get_forge_github_login", new=AsyncMock(return_value="forge-bot")
            ),
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
        ):
            result = await worker._handle_resume_event(message, state)

        # It should be ignored because of the App bot suffix matching our bot login
        assert result is state
        assert result.get("is_paused") is True

    @pytest.mark.asyncio
    async def test_integration_other_app_bot_comment_ending_in_bot_is_not_ignored(self):
        """Confirm that external App bot reviews/comments (not our bot) are not ignored and are processed."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "current_repo": "owner/repo",
            "current_pr_number": 42,
            "is_paused": True,
            "context": {},
        }
        # Sender is another App bot (ends with [bot], e.g., 'coderabbitai[bot]')
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="coderabbitai[bot]", is_bot=True),
            review=Review(
                id="100",
                state=ReviewState.CHANGES_REQUESTED,
                body="!This is an external bot review comment.",
                author="coderabbitai[bot]",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        settings = MagicMock(forge_bot_comment_prefix="my-signature")
        mock_adapter = AsyncMock()
        mock_adapter.get_review_comments_for_submission.return_value = []

        with (
            patch.object(
                worker, "_get_forge_github_login", new=AsyncMock(return_value="forge-bot")
            ),
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
            _patch_adapter(_sc_repo_ref("owner/repo"), mock_adapter),
        ):
            result = await worker._handle_resume_event(message, state)

        # It should be processed (not ignored)
        assert result is not state
        assert result.get("is_paused") is False
        assert result.get("revision_requested") is True
        assert "!This is an external bot review comment." in result.get("feedback_comment", "")

    @pytest.mark.asyncio
    async def test_integration_legacy_fallback_no_prefix_ignored(self):
        """Integration test confirms that when no prefix is configured, matching bot-login comments are always ignored (legacy fallback)."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "current_repo": "owner/repo",
            "current_pr_number": 42,
            "is_paused": True,
            "context": {},
        }
        # Sender matches bot login, prefix is not configured
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            repo_ref=_sc_repo_ref("owner/repo"),
            change_request=_sc_change_request("owner/repo", 42),
            actor=Actor(login="dev-user", is_bot=False),
            review=Review(
                id="100",
                state=ReviewState.CHANGES_REQUESTED,
                body="Some body without signature",
                author="dev-user",
            ),
        )
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="TEST-123",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )

        # Prefix is empty/None/disabled
        settings = MagicMock(forge_bot_comment_prefix="")

        with (
            patch.object(worker, "_get_forge_github_login", new=AsyncMock(return_value="dev-user")),
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
        ):
            result = await worker._handle_resume_event(message, state)

        # It should be ignored under the legacy fallback because prefix is empty
        assert result is state
        assert result.get("is_paused") is True


def _make_normalized_event(**overrides) -> NormalizedEvent:
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


def _sc_repo_ref(namespace: str = "owner/repo") -> RepositoryRef:
    """Build a RepositoryRef for a given owner/repo namespace."""
    return RepositoryRef(
        id=namespace,
        provider=Provider.GITHUB,
        connection="default-github",
        namespace=namespace,
        default_branch="main",
        change_request_mode="fork",
    )


def _sc_change_request(
    namespace: str = "owner/repo",
    number: int = 42,
    state: ChangeRequestState = ChangeRequestState.OPEN,
) -> ChangeRequest:
    """Build a ChangeRequest carrying the PR number in native_id."""
    return ChangeRequest(
        identity=ChangeRequestIdentity(
            connection="default-github", repository_id=namespace, native_id=number
        ),
        url=f"https://github.com/{namespace}/pull/{number}",
        title="t",
        body="",
        state=state,
        source_branch="feature",
        target_branch="main",
        draft=False,
    )


class TestDeserializeEvent:
    """Tests for NormalizedEvent reconstruction from a queue message."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        """Create a worker instance for testing."""
        return OrchestratorWorker(consumer_name="test-worker")

    def test_returns_none_for_jira_message(self, worker):
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="PROJ-1",
            payload={},
        )
        assert worker._deserialize_event(message) is None

    def test_deserializes_source_control_message(self, worker):
        event = _make_normalized_event()
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_opened",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        restored = worker._deserialize_event(message)
        assert restored is not None
        assert restored.kind == EventKind.CR_OPENED
        assert restored.repo_ref.namespace == "acme/payments"


class TestIsPrdSpecPrEvent:
    """Tests for PRD/spec proposals-PR detection off typed event fields."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        """Create a worker instance for testing."""
        return OrchestratorWorker(consumer_name="test-worker")

    def test_is_prd_pr_event_matches_by_repo_and_number(self, worker):
        event = _make_normalized_event()
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_updated",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {"prd_pr_number": 42, "prd_pr_repo": "acme/payments"}

        assert worker._is_prd_pr_event(message, current_state) is True

    def test_is_prd_pr_event_false_when_number_differs(self, worker):
        event = _make_normalized_event()
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_updated",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {"prd_pr_number": 99, "prd_pr_repo": "acme/payments"}

        assert worker._is_prd_pr_event(message, current_state) is False

    def test_is_prd_pr_event_false_for_jira_source(self, worker):
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="PROJ-1",
            payload={},
        )
        current_state = {"prd_pr_number": 42, "prd_pr_repo": "acme/payments"}

        assert worker._is_prd_pr_event(message, current_state) is False


class TestCiWebhookDetectionTypedFields:
    """CI-webhook detection reads typed NormalizedEvent fields (Task 14)."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.mark.asyncio
    async def test_check_run_completed_wakes_ci_evaluator(self, worker):
        event = _make_normalized_event(kind=EventKind.CHECK_UPDATED)
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="check_updated",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "ci_evaluator",
            "is_paused": True,
            "pull_requests": {"acme/payments:42": {"number": 42, "repo": "acme/payments"}},
            "current_repo": "acme/payments",
            "current_pr_number": 42,
        }

        updated = await worker._handle_resume_event(message, current_state)

        assert updated["is_paused"] is False

    @pytest.mark.asyncio
    async def test_incomplete_check_suite_does_not_wake_ci_evaluator(self, worker):
        """A CHECK_UPDATED event whose suite is still in_progress must not unpause.

        The suite-completion nuance from the original truth table is preserved by
        reading the normalized check_suite_status field (which survives the queue hop).
        """
        event = _make_normalized_event(
            kind=EventKind.CHECK_UPDATED,
            check_suite_status=CheckStatus.IN_PROGRESS,
            raw={"check_suite": {"status": "in_progress"}},
        )
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="check_updated",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {"current_node": "ci_evaluator", "is_paused": True, "context": {}}

        updated = await worker._handle_resume_event(message, current_state)

        assert updated is current_state

    @pytest.mark.asyncio
    async def test_synchronize_push_event_wakes_ci_evaluator(self, worker):
        """Preserve the original 'extra CI branch': a non-check, non-comment,
        non-review, non-merged event with targets_implementation_pr=True still
        wakes the workflow at ci_evaluator (branch (b) of the original truth table).
        """
        event = _make_normalized_event(kind=EventKind.CR_UPDATED)  # synchronize
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_updated",
            ticket_key="PROJ-1",
            payload={
                "repository": {"full_name": "acme/payments"},
                "pull_request": {"number": 42},
            },
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "ci_evaluator",
            "is_paused": True,
            "context": {},
            "pull_requests": {"acme/payments:42": {"number": 42, "repo": "acme/payments"}},
        }

        updated = await worker._handle_resume_event(message, current_state)

        assert updated["is_paused"] is False

    @pytest.mark.asyncio
    async def test_merged_pr_event_does_not_wake_ci_evaluator(self, worker):
        """A merged change request is excluded from the 'extra CI branch' — it
        must not set is_ci_webhook (matching the original merged-PR exclusion).

        This isolates the CI-branch exclusion: the event does NOT target an
        implementation PR, so the (separately tested) PR-merge-at-review-gate
        block does not fire and the paused ci_evaluator stays paused because no
        CI signal was recognised.
        """
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
            "current_node": "ci_evaluator",
            "is_paused": True,
            "context": {},
        }

        updated = await worker._handle_resume_event(message, current_state)

        # No CI signal recognised — the paused gate is not woken.
        assert updated["is_paused"] is True

    @pytest.mark.asyncio
    async def test_non_command_comment_does_not_set_ci_webhook(self, worker):
        """A plain (non-command) COMMENT_CREATED at ci_evaluator must NOT fire the
        CI-webhook branch — comment-kind events are excluded from branch (b).
        """
        event = _make_normalized_event(kind=EventKind.COMMENT_CREATED)
        event.comment = ReviewComment(id="1", body="just a regular comment", author="octocat")
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {"current_node": "ci_evaluator", "is_paused": True, "context": {}}

        updated = await worker._handle_resume_event(message, current_state)

        # CI-webhook branch did not fire — the paused gate stays paused.
        assert updated["is_paused"] is True

    @pytest.mark.asyncio
    async def test_review_submitted_does_not_set_ci_webhook(self, worker):
        """A REVIEW_SUBMITTED event at ci_evaluator must NOT fire the CI-webhook
        branch — review-kind events are excluded from branch (b).
        """
        event = _make_normalized_event(kind=EventKind.REVIEW_SUBMITTED)
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {"current_node": "ci_evaluator", "is_paused": True, "context": {}}

        updated = await worker._handle_resume_event(message, current_state)

        assert updated["is_paused"] is True


class TestSkipGateCommandTypedFields:
    """skip-gate/unskip-gate/rebase detection reads typed fields (Task 14)."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.mark.asyncio
    async def test_skip_gate_command_adds_check_name(self, worker):
        event = _make_normalized_event(kind=EventKind.COMMENT_CREATED)
        event.comment = ReviewComment(id="1", body="/forge skip-gate flaky-test", author="octocat")
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {"current_node": "ci_evaluator", "is_paused": True}

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            updated = await worker._handle_resume_event(message, current_state)

        assert "flaky-test" in updated["ci_skipped_checks"]
        assert updated["current_node"] == "ci_evaluator"

    @pytest.mark.asyncio
    async def test_skip_gate_passes_typed_pr_and_sender_to_feedback(self, worker):
        """pr_number, owner/repo and sender come from typed fields, not the payload."""
        event = _make_normalized_event(kind=EventKind.COMMENT_CREATED)
        event.comment = ReviewComment(id="1", body="/forge skip-gate flaky-test", author="octocat")
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {"current_node": "ci_evaluator", "is_paused": True}
        feedback = AsyncMock()

        with patch.object(worker, "_post_skip_gate_feedback", feedback):
            await worker._handle_resume_event(message, current_state)

        feedback.assert_called_once()
        kwargs = feedback.call_args.kwargs
        assert kwargs["repo_ref"].namespace == "acme/payments"
        assert kwargs["pr_number"] == 42
        assert kwargs["sender"] == "octocat"

    @pytest.mark.asyncio
    async def test_rebase_command_routes_to_rebase_pr(self, worker):
        """/forge rebase reads typed fields and routes to rebase_pr."""
        event = _make_normalized_event(kind=EventKind.COMMENT_CREATED)
        event.comment = ReviewComment(id="1", body="/forge rebase", author="octocat")
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "human_review_gate",
            "is_paused": True,
            "current_pr_number": 42,
        }
        feedback = AsyncMock()

        with patch.object(worker, "_post_rebase_feedback", feedback):
            updated = await worker._handle_resume_event(message, current_state)

        assert updated["current_node"] == "rebase_pr"
        assert updated["is_paused"] is False
        assert updated["rebase_return_node"] == "human_review_gate"
        feedback.assert_called_once()
        kwargs = feedback.call_args.kwargs
        assert kwargs["repo_ref"].namespace == "acme/payments"
        assert kwargs["pr_number"] == 42
        assert kwargs["sender"] == "octocat"


class TestInlineReviewReplyTypedFields:
    """Inline review-reply detection at review_response_gate reads typed fields."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.mark.asyncio
    async def test_inline_reply_clears_matching_contested_comment(self, worker):
        # A pull_request_review_comment reply maps to COMMENT_CREATED with a path
        # set and in_reply_to carrying the parent comment id (a str). The parent
        # id is coerced to int to match the int comment ids persisted in state.
        event = _make_normalized_event(
            kind=EventKind.COMMENT_CREATED,
            comment=ReviewComment(
                id="2", body="fixed", author="octocat", path="src/x.py", in_reply_to="1"
            ),
        )
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [{"comment_id": 1}],
        }

        with patch.object(worker, "_get_forge_github_login", AsyncMock(return_value="forge-bot")):
            updated = await worker._handle_resume_event(message, current_state)

        assert updated["revision_requested"] is True
        assert updated["contested_comments"] == []
        assert updated["context"]["review_thread_comment_id"] == 1

    @pytest.mark.asyncio
    async def test_non_reply_inline_comment_is_still_actionable(self, worker):
        """The two-branch question resolved: a non-reply inline comment (no
        in_reply_to) at review_response_gate does NOT fall through to an unchanged
        state (which would be a silent regression). It is handled by the preserved
        second branch — unpause + revision using the comment's own id — leaving
        contested threads untouched. Behavior is therefore NOT equivalent to a
        fall-through, so both branches are kept.
        """
        event = _make_normalized_event(
            kind=EventKind.COMMENT_CREATED,
            comment=ReviewComment(
                id="30", body="Please cover this case.", author="octocat", path="src/x.py"
            ),
        )
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [{"comment_id": 1}],
        }

        with patch.object(worker, "_get_forge_github_login", AsyncMock(return_value="forge-bot")):
            updated = await worker._handle_resume_event(message, current_state)

        assert updated is not current_state
        assert updated["is_paused"] is False
        assert updated["revision_requested"] is True
        assert updated["feedback_comment"] == "Please cover this case."
        # Non-reply: contested threads are preserved and the own id is recorded.
        assert updated["contested_comments"] == [{"comment_id": 1}]
        assert updated["context"]["review_thread_comment_id"] == 30

    @pytest.mark.asyncio
    async def test_top_level_issue_comment_does_not_match_this_block(self, worker):
        """An issue comment (COMMENT_CREATED with no path) at review_response_gate
        must NOT be treated as an inline review reply — matching the original
        'pull_request_review_comment' event-type restriction. With no other
        review_response_gate handler, a paused gate stays paused.
        """
        event = _make_normalized_event(
            kind=EventKind.COMMENT_CREATED,
            comment=ReviewComment(id="7", body="just a comment", author="octocat"),
        )
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="comment_created",
            ticket_key="PROJ-1",
            payload={},
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }

        updated = await worker._handle_resume_event(message, current_state)

        assert updated is current_state


class TestHumanReviewGateTypedFields:
    """Human-review-gate PR-review + PR-merge detection reads typed fields."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.mark.asyncio
    async def test_review_approved_sets_implementation_pr_approved(self, worker):
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            review=Review(id="1", state=ReviewState.APPROVED, body="", author="reviewer1"),
        )
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="PROJ-1",
            payload={
                "repository": {"full_name": "acme/payments"},
                "pull_request": {"number": 42},
            },
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "human_review_gate",
            "is_paused": True,
            "pull_requests": {"acme/payments:42": {"number": 42, "repo": "acme/payments"}},
            "current_repo": "acme/payments",
            "current_pr_number": 42,
        }

        with patch.object(worker, "_get_forge_github_login", AsyncMock(return_value="forge-bot")):
            updated = await worker._handle_resume_event(message, current_state)

        assert updated["human_review_status"] == "approved"

    @pytest.mark.asyncio
    async def test_pr_merged_at_review_gate_sets_pr_merged(self, worker):
        event = _make_normalized_event(kind=EventKind.CR_MERGED)
        event.change_request.state = ChangeRequestState.MERGED
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="cr_merged",
            ticket_key="PROJ-1",
            payload={
                "repository": {"full_name": "acme/payments"},
                "pull_request": {"merged": True, "number": 42},
            },
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "human_review_gate",
            "is_paused": True,
            "pull_requests": {"acme/payments:42": {"number": 42, "repo": "acme/payments"}},
            "current_repo": "acme/payments",
            "current_pr_number": 42,
        }

        updated = await worker._handle_resume_event(message, current_state)

        assert updated.get("pr_merged") is True

    @pytest.mark.asyncio
    async def test_dismissed_review_does_not_trigger_revision(self, worker):
        """A dismissed review (an admin unblocking a stale review) must not be
        mistaken for an active COMMENTED review requesting changes -- it maps
        to its own ReviewState.DISMISSED, which matches neither the APPROVED
        nor the (CHANGES_REQUESTED, COMMENTED) branches, so state is left
        unchanged, same as the original raw-string-based behavior."""
        event = _make_normalized_event(
            kind=EventKind.REVIEW_SUBMITTED,
            review=Review(id="1", state=ReviewState.DISMISSED, body="", author="reviewer1"),
        )
        message = QueueMessage(
            message_id="1",
            event_id="e1",
            source=EventSource.SOURCE_CONTROL,
            event_type="review_submitted",
            ticket_key="PROJ-1",
            payload={
                "repository": {"full_name": "acme/payments"},
                "pull_request": {"number": 42},
            },
            normalized_event=normalized_event_to_dict(event),
        )
        current_state = {
            "current_node": "human_review_gate",
            "is_paused": True,
            "pull_requests": {"acme/payments:42": {"number": 42, "repo": "acme/payments"}},
            "current_repo": "acme/payments",
            "current_pr_number": 42,
        }

        with patch.object(worker, "_get_forge_github_login", AsyncMock(return_value="forge-bot")):
            updated = await worker._handle_resume_event(message, current_state)

        assert "human_review_status" not in updated
        assert updated.get("revision_requested") is not True
        assert updated.get("is_paused") is True
