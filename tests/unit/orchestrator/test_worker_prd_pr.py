"""Tests for PRD PR event handling in the worker."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage


def _make_message(event_type: str, payload: dict, ticket_key: str = "TEST-123") -> QueueMessage:
    return QueueMessage(
        message_id="msg-1",
        event_id="evt-1",
        source=EventSource.GITHUB,
        event_type=event_type,
        ticket_key=ticket_key,
        payload=payload,
    )


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
        return w


class TestIsPrdPrEvent:
    def test_true_for_matching_repo_and_pr(self, worker):
        msg = _make_message("pull_request_review:submitted", {
            "repository": {"full_name": "org/proposals"},
            "pull_request": {"number": 7},
        })
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is True

    def test_false_for_wrong_repo(self, worker):
        msg = _make_message("pull_request_review:submitted", {
            "repository": {"full_name": "org/other-repo"},
            "pull_request": {"number": 7},
        })
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is False

    def test_false_for_wrong_pr_number(self, worker):
        msg = _make_message("pull_request_review:submitted", {
            "repository": {"full_name": "org/proposals"},
            "pull_request": {"number": 99},
        })
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is False

    def test_false_when_no_prd_pr_in_state(self, worker):
        msg = _make_message("pull_request_review:submitted", {
            "repository": {"full_name": "org/proposals"},
            "pull_request": {"number": 7},
        })
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
        msg = _make_message("issue_comment:created", {
            "repository": {"full_name": "org/proposals"},
            "issue": {"number": 7},
        })
        state = _prd_gate_state()
        assert worker._is_prd_pr_event(msg, state) is True


class TestHandlePrdPrMerge:
    @pytest.mark.asyncio
    async def test_pr_merge_sets_approved(self, worker):
        msg = _make_message("pull_request:closed", {
            "repository": {"full_name": "org/proposals"},
            "pull_request": {"number": 7, "merged": True},
        })
        state = _prd_gate_state()

        with patch("forge.orchestrator.worker.JiraClient") as MockJira:
            mock_jira = MagicMock()
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.close = AsyncMock()
            MockJira.return_value = mock_jira

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        mock_jira.set_workflow_label.assert_called_once()

    @pytest.mark.asyncio
    async def test_pr_close_without_merge_is_ignored(self, worker):
        msg = _make_message("pull_request:closed", {
            "repository": {"full_name": "org/proposals"},
            "pull_request": {"number": 7, "merged": False},
        })
        state = _prd_gate_state()

        result = await worker._handle_resume_event(msg, state)

        # Should remain paused -- closed without merge is not approval
        assert result.get("is_paused", True) is True


class TestHandlePrdPrReview:
    @pytest.mark.asyncio
    async def test_changes_requested_sets_feedback(self, worker):
        msg = _make_message("pull_request_review:submitted", {
            "repository": {"full_name": "org/proposals"},
            "pull_request": {"number": 7},
            "review": {"id": 101, "state": "changes_requested", "body": "Please add more detail"},
        })
        state = _prd_gate_state()

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_review_comments = AsyncMock(return_value=[])
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "more detail" in result["feedback_comment"]
        mock_gh.get_review_comments.assert_called_once_with("org", "proposals", 7, 101)

    @pytest.mark.asyncio
    async def test_approved_review_is_ignored(self, worker):
        msg = _make_message("pull_request_review:submitted", {
            "repository": {"full_name": "org/proposals"},
            "pull_request": {"number": 7},
            "review": {"state": "approved", "body": "LGTM"},
        })
        state = _prd_gate_state()

        result = await worker._handle_resume_event(msg, state)

        # Should remain paused -- review approval is not an approval signal
        assert result.get("is_paused", True) is True


class TestHandlePrdPrComment:
    @pytest.mark.asyncio
    async def test_comment_sets_feedback(self, worker):
        msg = _make_message("issue_comment:created", {
            "repository": {"full_name": "org/proposals"},
            "issue": {"number": 7},
            "comment": {
                "body": "Please expand the scope section",
                "user": {"login": "reviewer"},
            },
            "sender": {"login": "reviewer"},
        })
        state = _prd_gate_state()

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "scope section" in result["feedback_comment"]

    @pytest.mark.asyncio
    async def test_self_comment_is_ignored(self, worker):
        msg = _make_message("issue_comment:created", {
            "repository": {"full_name": "org/proposals"},
            "issue": {"number": 7},
            "comment": {
                "body": "PRD has been revised based on feedback.",
                "user": {"login": "forge-bot"},
            },
            "sender": {"login": "forge-bot"},
        })
        state = _prd_gate_state()

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

            result = await worker._handle_resume_event(msg, state)

        # Should remain paused -- self-comment ignored
        assert result.get("is_paused", True) is True

    @pytest.mark.asyncio
    async def test_question_comment_sets_question_flag(self, worker):
        msg = _make_message("issue_comment:created", {
            "repository": {"full_name": "org/proposals"},
            "issue": {"number": 7},
            "comment": {
                "body": "?Why did you choose REST over GraphQL?",
                "user": {"login": "reviewer"},
            },
            "sender": {"login": "reviewer"},
        })
        state = _prd_gate_state()

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result.get("is_question") is True
        assert "REST" in result["feedback_comment"]


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
