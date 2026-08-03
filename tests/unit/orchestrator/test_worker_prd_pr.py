"""Tests for PRD PR event handling in the worker."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage
from forge.workflow.utils.automated_review_triage import AutomatedReviewDecision


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
        w._post_resume_ack_comment = AsyncMock()
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

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_pull_request_review_threads = AsyncMock(return_value=[])
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        assert result["revision_requested"] is True
        assert "more detail" in result["feedback_comment"]
        mock_gh.get_pull_request_review_threads.assert_called_once_with("org", "proposals", 7)

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
            {
                "thread_id": "accept-thread",
                "path": "prd.md",
                "line": 10,
                "comments": [{"comment_id": 10, "body": "Clarify authorization."}],
            },
            {
                "thread_id": "reply-thread",
                "path": "prd.md",
                "line": 20,
                "comments": [{"comment_id": 20, "body": "Rename the product."}],
            },
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

        with (
            patch("forge.orchestrator.worker.GitHubClient") as MockGH,
            patch(
                "forge.orchestrator.worker.triage_proposal_review_threads",
                new=AsyncMock(return_value=decisions),
            ),
            patch(
                "forge.orchestrator.worker.reply_to_proposal_decisions",
                new=AsyncMock(),
            ) as reply_decisions,
        ):
            mock_gh = MagicMock()
            mock_gh.get_pull_request_review_threads = AsyncMock(return_value=threads)
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh
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

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

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

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

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

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh
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

        with (
            patch("forge.orchestrator.worker.GitHubClient") as MockGH,
            patch(
                "forge.orchestrator.worker.triage_automated_review",
                new=AsyncMock(
                    return_value=AutomatedReviewDecision(
                        "satisfied", reason="The overall review passes"
                    )
                ),
            ) as triage,
        ):
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh
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

        with (
            patch("forge.orchestrator.worker.GitHubClient") as MockGH,
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
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh
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

        with (
            patch("forge.orchestrator.worker.GitHubClient") as MockGH,
            patch(
                "forge.orchestrator.worker.triage_automated_review",
                new=AsyncMock(
                    return_value=AutomatedReviewDecision(
                        "uncertain", reason="The disposition is contradictory"
                    )
                ),
            ),
        ):
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh
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

        with (
            patch("forge.orchestrator.worker.GitHubClient") as MockGH,
            patch(
                "forge.orchestrator.worker.triage_automated_review",
                new=AsyncMock(
                    return_value=AutomatedReviewDecision(
                        "blocking", blocking_feedback="Revise again."
                    )
                ),
            ),
        ):
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh
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

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

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

        with patch("forge.orchestrator.worker.GitHubClient") as MockGH:
            mock_gh = MagicMock()
            mock_gh.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

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
            {
                "thread_id": "thread-1",
                "path": "prd.md",
                "line": 10,
                "comments": [{"comment_id": 100, "body": "Fix this section."}],
            },
        ]
        state = _prd_gate_state(prd_content="# Current PRD")

        with (
            patch("forge.orchestrator.worker.GitHubClient") as MockGH,
            patch(
                "forge.orchestrator.worker.triage_proposal_review_threads",
                new=AsyncMock(),
            ) as triage,
        ):
            mock_gh = MagicMock()
            mock_gh.get_pull_request_review_threads = AsyncMock(return_value=threads)
            mock_gh.close = AsyncMock()
            MockGH.return_value = mock_gh

            result = await worker._handle_resume_event(msg, state)

        assert result["revision_requested"] is True
        assert "Fix this section" in result["feedback_comment"]
        triage.assert_not_awaited()
