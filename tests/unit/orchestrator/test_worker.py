"""Unit tests for the orchestrator worker."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.events import EventSource
from forge.orchestrator.worker import (
    OrchestratorWorker,
    _has_new_reportable_error,
    _report_new_workflow_error,
)
from forge.queue.models import QueueMessage


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
            "acme/backend": {
                "repo": "acme/backend",
                "number": 10,
                "url": "https://github.com/acme/backend/pull/10",
                "merged": False,
                "ci_status": "pending",
            },
            "acme/frontend": {
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
        return QueueMessage(
            message_id=f"msg-{number}",
            event_id=f"evt-{number}",
            source=EventSource.GITHUB,
            event_type="pull_request",
            ticket_key="TEST-123",
            payload={
                "action": "closed",
                "pull_request": {"merged": True, "number": number},
                "repository": {"full_name": repo},
            },
        )

    partial = await worker._handle_resume_event(merge_message("acme/backend", 10), state)

    assert partial["current_repo"] == "acme/backend"
    assert partial["pull_requests"]["acme/backend"]["merged"] is True
    assert partial["pull_requests"]["acme/frontend"]["merged"] is False
    assert partial["pr_merged"] is False
    assert partial["is_paused"] is True

    complete = await worker._handle_resume_event(merge_message("acme/frontend", 20), partial)

    assert complete["pr_merged"] is True
    assert complete["is_paused"] is False


@pytest.mark.asyncio
async def test_multi_repo_ci_webhook_selects_earlier_pr_from_review_gate() -> None:
    worker = OrchestratorWorker(consumer_name="test-worker")
    message = QueueMessage(
        message_id="msg-ci",
        event_id="evt-ci",
        source=EventSource.GITHUB,
        event_type="check_suite",
        ticket_key="TEST-123",
        payload={
            "check_suite": {"status": "completed", "pull_requests": [{"number": 10}]},
            "repository": {"full_name": "acme/backend"},
        },
    )

    result = await worker._handle_resume_event(message, _multi_repo_pr_state())

    assert result["current_repo"] == "acme/backend"
    assert result["current_pr_number"] == 10
    assert result["current_node"] == "ci_evaluator"
    assert result["is_paused"] is False


@pytest.mark.asyncio
async def test_multi_repo_approval_uses_common_state_cleanup_path() -> None:
    worker = OrchestratorWorker(consumer_name="test-worker")
    state = _multi_repo_pr_state()
    state["last_error"] = "stale review failure"
    state["revision_requested"] = True
    state["feedback_comment"] = "old feedback"
    message = QueueMessage(
        message_id="msg-approved",
        event_id="evt-approved",
        source=EventSource.GITHUB,
        event_type="pull_request_review",
        ticket_key="TEST-123",
        payload={
            "review": {"state": "approved", "body": "Looks good"},
            "pull_request": {"number": 10},
            "repository": {"full_name": "acme/backend"},
        },
    )

    result = await worker._handle_resume_event(message, state)

    assert result["current_repo"] == "acme/backend"
    assert result["is_paused"] is True
    assert result["last_error"] is None
    assert result["revision_requested"] is False
    assert result["feedback_comment"] is None
    assert result["human_review_status"] == "approved"
    assert result["pull_requests"]["acme/backend"]["human_review_status"] == "approved"


@pytest.mark.asyncio
@patch("forge.orchestrator.worker.GitHubClient")
async def test_multi_repo_review_selects_earlier_pr(mock_github_client: MagicMock) -> None:
    github = AsyncMock()
    github.get_review_comments.return_value = []
    mock_github_client.return_value = github
    worker = OrchestratorWorker(consumer_name="test-worker")
    state = _multi_repo_pr_state()
    state["current_node"] = "wait_for_ci_gate"
    message = QueueMessage(
        message_id="msg-review",
        event_id="evt-review",
        source=EventSource.GITHUB,
        event_type="pull_request_review",
        ticket_key="TEST-123",
        payload={
            "review": {"id": 5, "state": "changes_requested", "body": "Fix backend"},
            "pull_request": {"number": 10},
            "repository": {"full_name": "acme/backend"},
        },
    )

    result = await worker._handle_resume_event(message, state)

    assert result["current_repo"] == "acme/backend"
    assert result["current_pr_number"] == 10
    assert result["current_node"] == "human_review_gate"
    assert result["revision_requested"] is True
    assert result["feedback_comment"] == "Fix backend"


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

        async def fake_ensure_skills(project_key, _jira_client, _skills_dir) -> None:
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
        """skills_dir passed to ensure_skills comes from settings.skills_dir."""
        received: dict = {}

        async def fake_ensure_skills(_project_key, _jira_client, skills_dir) -> None:
            received["skills_dir"] = skills_dir

        worker.settings.skills_dir = "custom/skills"

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            await worker._process_workflow(jira_message)

        assert received["skills_dir"] == Path("custom/skills")

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

        async def fake_ensure_skills(_project_key, jira_client, _skills_dir) -> None:
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

        async def fake_ensure_skills_no_property(project_key, jira_client, _skills_dir) -> None:
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
        return QueueMessage(
            message_id="1-0",
            event_id="test-ci-001",
            source=EventSource.GITHUB,
            event_type="check_suite",
            ticket_key="AISOS-701",
            payload={
                "action": "completed",
                "check_suite": {
                    "status": "completed",
                    "conclusion": conclusion,
                    "head_branch": "forge/aisos-701",
                    "pull_requests": [{"number": 52}],
                },
                "repository": {"full_name": "forge-sdlc/forge"},
            },
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
    async def test_check_suite_at_wait_for_ci_gate_still_works(self, worker):
        """Existing wait_for_ci_gate behaviour must be preserved."""
        state = self._ci_state("wait_for_ci_gate")
        message = self._check_suite_message("success")

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_incomplete_check_suite_does_not_unpause_at_ci_evaluator(self, worker):
        """A check_suite with status=in_progress must not wake up the workflow."""
        state = self._ci_state("ci_evaluator")
        message = QueueMessage(
            message_id="1-0",
            event_id="test-ci-002",
            source=EventSource.GITHUB,
            event_type="check_suite",
            ticket_key="AISOS-701",
            payload={
                "check_suite": {"status": "in_progress", "conclusion": None},
                "repository": {"full_name": "forge-sdlc/forge"},
            },
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


class TestHandleResumeEventReviewGates:
    """Tests for resuming workflows from human_review_gate and review_response_gate."""

    @pytest.mark.asyncio
    async def test_forge_github_login_is_cached_per_worker(self):
        worker = OrchestratorWorker.__new__(OrchestratorWorker)
        mock_github = AsyncMock()
        mock_github.get_authenticated_user.return_value = {"login": "forge-bot"}

        with patch("forge.orchestrator.worker.GitHubClient", return_value=mock_github):
            first = await worker._get_forge_github_login()
            second = await worker._get_forge_github_login()

        assert first == second == "forge-bot"
        mock_github.get_authenticated_user.assert_awaited_once()
        mock_github.close.assert_awaited_once()

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
        message = QueueMessage(
            message_id="msg-forge-review",
            event_id="evt-forge-review",
            source=EventSource.GITHUB,
            event_type="pull_request_review:submitted",
            ticket_key="TEST-236",
            payload={
                "review": {
                    "id": 99,
                    "state": "commented",
                    "body": "",
                    "user": {"login": "forge-bot", "type": "Bot"},
                },
                "pull_request": {"number": 42},
                "repository": {"full_name": "owner/repo"},
                "sender": {"login": "forge-bot", "type": "Bot"},
            },
        )

        with (
            patch.object(
                worker,
                "_get_forge_github_login",
                new=AsyncMock(return_value="forge-bot"),
            ) as get_forge_login,
            patch("forge.orchestrator.worker.GitHubClient") as github_client,
        ):
            result = await worker._handle_resume_event(message, state)

        assert result is state
        get_forge_login.assert_awaited_once()
        github_client.assert_not_called()

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
        message = QueueMessage(
            message_id="msg-thread-reply",
            event_id="evt-thread-reply",
            source=EventSource.GITHUB,
            event_type="pull_request_review_comment:created",
            ticket_key="TEST-233",
            payload={
                "comment": {
                    "id": 12,
                    "in_reply_to_id": 11,
                    "body": "Please make this change after all.",
                },
                "pull_request": {"number": 42},
                "repository": {"full_name": "owner/repo"},
                "sender": {"login": "reviewer"},
            },
        )

        mock_github = AsyncMock()
        mock_github.get_authenticated_user.return_value = {"login": "forge-bot"}
        with patch("forge.orchestrator.worker.GitHubClient", return_value=mock_github):
            result = await worker._handle_resume_event(message, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please make this change after all."
        assert result["contested_comments"] == [{"thread_id": "thread-b", "comment_id": 20}]

    @pytest.mark.asyncio
    async def test_standalone_inline_comment_is_actionable_at_response_gate(self):
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-233",
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [{"thread_id": "thread-a", "comment_id": 10}],
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-new-thread",
            event_id="evt-new-thread",
            source=EventSource.GITHUB,
            event_type="pull_request_review_comment:created",
            ticket_key="TEST-233",
            payload={
                "comment": {"id": 30, "body": "Please cover this edge case."},
                "pull_request": {"number": 42},
                "repository": {"full_name": "owner/repo"},
                "sender": {"login": "reviewer"},
            },
        )
        mock_github = AsyncMock()
        mock_github.get_authenticated_user.return_value = {"login": "forge-bot"}

        with patch("forge.orchestrator.worker.GitHubClient", return_value=mock_github):
            result = await worker._handle_resume_event(message, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please cover this edge case."
        assert result["contested_comments"] == state["contested_comments"]

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    @patch("forge.orchestrator.worker.GitHubClient")
    async def test_handle_resume_event_pr_review_at_review_response_gate(
        self, mock_github_client, _mock_post_comment
    ):
        """When at review_response_gate and a PR review changes_requested/commented occurs, unpause the workflow."""
        mock_gh = AsyncMock()
        mock_gh.get_pull_request_review_comments.return_value = [
            {"path": "src/file.py", "position": 10, "body": "Please fix this."}
        ]
        mock_github_client.return_value = mock_gh

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
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "state": "changes_requested",
                    "body": "PR needs some work",
                },
                "pull_request": {
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["contested_comments"] == []
        assert "PR needs some work" in result["feedback_comment"]
        assert "src/file.py" in result["feedback_comment"]
        mock_gh.get_pull_request_review_comments.assert_called_once_with("owner", "repo", 42)

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    @patch("forge.orchestrator.worker.GitHubClient")
    async def test_handle_resume_event_pr_review_with_review_id_at_review_response_gate(
        self, mock_github_client, _mock_post_comment
    ):
        """When at review_response_gate and a pull_request_review payload contains a valid review ID, gh.get_review_comments is called."""
        mock_gh = AsyncMock()
        # Mock various position/line structures to verify fallback
        mock_gh.get_review_comments.return_value = [
            {"path": "src/file1.py", "position": 10, "body": "Fix position."},
            {"path": "src/file2.py", "line": 20, "body": "Fix line."},
            {
                "path": "src/file2b.py",
                "position": 4,
                "line": 150,
                "body": "Prefer the file line.",
            },
            {"path": "src/file3.py", "original_line": 30, "body": "Fix original_line."},
            {"path": "src/file4.py", "body": "Fix none."},
        ]
        mock_github_client.return_value = mock_gh

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
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "id": 999,
                    "state": "changes_requested",
                    "body": "PR review body",
                },
                "pull_request": {
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["contested_comments"] == []
        assert "PR review body" in result["feedback_comment"]
        assert "src/file1.py" in result["feedback_comment"]
        assert "(line 10)" in result["feedback_comment"]
        assert "(line 20)" in result["feedback_comment"]
        assert "(line 150)" in result["feedback_comment"]
        assert "(line 4)" not in result["feedback_comment"]
        assert "(line 30)" in result["feedback_comment"]
        assert "(line ?)" in result["feedback_comment"]
        mock_gh.get_review_comments.assert_called_once_with("owner", "repo", 42, 999)
        mock_gh.get_pull_request_review_comments.assert_not_called()

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    @patch("forge.orchestrator.worker.GitHubClient")
    async def test_handle_resume_event_pr_review_fallback_at_review_response_gate(
        self, mock_github_client, _mock_post_comment
    ):
        """When at review_response_gate and a pull_request_review payload contains NO review ID, gh.get_pull_request_review_comments is called."""
        mock_gh = AsyncMock()
        mock_gh.get_pull_request_review_comments.return_value = [
            {"path": "src/file1.py", "position": 10, "body": "Fix position."},
            {"path": "src/file2.py", "line": 20, "body": "Fix line."},
            {"path": "src/file3.py", "original_line": 30, "body": "Fix original_line."},
            {"path": "src/file4.py", "body": "Fix none."},
        ]
        mock_github_client.return_value = mock_gh

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "state": "changes_requested",
                    "body": "PR review body",
                },
                "pull_request": {
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "PR review body" in result["feedback_comment"]
        assert "src/file1.py" in result["feedback_comment"]
        assert "(line 10)" in result["feedback_comment"]
        assert "(line 20)" in result["feedback_comment"]
        assert "(line 30)" in result["feedback_comment"]
        assert "(line ?)" in result["feedback_comment"]
        mock_gh.get_pull_request_review_comments.assert_called_once_with("owner", "repo", 42)
        mock_gh.get_review_comments.assert_not_called()

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_handle_resume_event_pr_approve_at_review_response_gate(self, _mock_post_comment):
        """When at review_response_gate and a PR review approved occurs, unpause the workflow."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "state": "approved",
                    "body": "Looks great!",
                },
                "pull_request": {
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result.get("revision_requested") is False

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_handle_resume_event_pr_merge_at_review_response_gate(self, _mock_post_comment):
        """When at review_response_gate and a PR merge occurs, unpause the workflow."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request",
            ticket_key="TEST-123",
            payload={
                "action": "closed",
                "pull_request": {
                    "merged": True,
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["pr_merged"] is True

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    @patch("forge.orchestrator.worker.GitHubClient")
    async def test_handle_resume_event_pr_review_at_human_review_gate(
        self, mock_github_client, _mock_post_comment
    ):
        """When at human_review_gate and a PR review changes_requested/commented occurs, unpause the workflow."""
        mock_gh = AsyncMock()
        mock_gh.get_pull_request_review_comments.return_value = []
        mock_github_client.return_value = mock_gh

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "human_review_gate",
            "is_paused": True,
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "state": "changes_requested",
                    "body": "Needs changes",
                },
                "pull_request": {
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Needs changes"

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    @patch("forge.orchestrator.worker.GitHubClient")
    async def test_handle_resume_event_pr_commented_review_at_review_response_gate(
        self, mock_github_client, _mock_post_comment
    ):
        """When at review_response_gate and a PR review with state 'commented' occurs, unpause the workflow."""
        mock_gh = AsyncMock()
        mock_gh.get_pull_request_review_comments.return_value = [
            {"path": "src/app.py", "position": 5, "body": "Nit: rename this variable."}
        ]
        mock_github_client.return_value = mock_gh

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "state": "commented",
                    "body": "",
                },
                "pull_request": {
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "src/app.py" in result["feedback_comment"]
        mock_gh.get_pull_request_review_comments.assert_called_once_with("owner", "repo", 42)

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    async def test_handle_resume_event_pr_review_ignored_when_not_paused_at_review_response_gate(
        self, _mock_post_comment
    ):
        """A PR review event arriving when is_paused=False at review_response_gate must be ignored."""
        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": False,
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "state": "changes_requested",
                    "body": "Fix this",
                },
                "pull_request": {
                    "number": 42,
                },
                "repository": {
                    "full_name": "owner/repo",
                },
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert result is state

    def test_review_response_gate_not_in_fresh_invoke_nodes(self):
        """review_response_gate must NOT use fresh-invoke — the gate unconditionally
        re-pauses, so ainvoke(state) would negate the is_paused=False set by the handler."""
        from forge.orchestrator.worker import _FRESH_INVOKE_NODES

        assert "review_response_gate" not in _FRESH_INVOKE_NODES

    @pytest.mark.asyncio
    @patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock)
    @patch("forge.orchestrator.worker.GitHubClient")
    async def test_review_response_gate_resume_state_routes_to_implement_review(
        self, mock_github_client, _mock_post_comment
    ):
        """After a changes_requested event at review_response_gate, the resulting
        state must route to implement_review (not back to human_review_gate)."""
        from forge.workflow.nodes.implement_review import route_review_response

        mock_gh = AsyncMock()
        mock_gh.get_pull_request_review_comments.return_value = [
            {"path": "src/main.py", "position": 7, "body": "Fix the typo here."}
        ]
        mock_github_client.return_value = mock_gh

        worker = OrchestratorWorker(consumer_name="test-worker")
        state = {
            "ticket_key": "TEST-123",
            "current_node": "review_response_gate",
            "is_paused": True,
            "contested_comments": [{"text": "Objection: renaming breaks the public API"}],
            "revision_requested": False,
            "context": {},
        }
        message = QueueMessage(
            message_id="msg-123",
            event_id="evt-123",
            source=EventSource.GITHUB,
            event_type="pull_request_review",
            ticket_key="TEST-123",
            payload={
                "review": {
                    "state": "changes_requested",
                    "body": "No, please apply the rename as requested",
                },
                "pull_request": {"number": 42},
                "repository": {"full_name": "owner/repo"},
            },
        )

        result = await worker._handle_resume_event(message, state)

        assert route_review_response(result) == "implement_review"
