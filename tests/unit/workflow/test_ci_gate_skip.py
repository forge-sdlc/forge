"""Tests for CI gate skip via GitHub PR comment (proposal 005)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.fixtures.workflow_states import make_workflow_state

from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage

# ── Helpers ───────────────────────────────────────────────────────────────────


def _skip_gate_message(base: QueueMessage, check_name: str) -> QueueMessage:
    """GitHub issue_comment event with /forge skip-gate command."""
    return QueueMessage(
        message_id=base.message_id,
        event_id=base.event_id,
        source=EventSource.GITHUB,
        event_type="issue_comment:created",  # GitHub appends :action
        ticket_key=base.ticket_key,
        payload={
            **base.payload,
            "comment": {"body": f"/forge skip-gate {check_name}"},
            "issue": {"number": 42, "pull_request": {}},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "eshulman2"},
        },
    )


def _unskip_gate_message(base: QueueMessage, check_name: str) -> QueueMessage:
    """GitHub issue_comment event with /forge unskip-gate command."""
    return QueueMessage(
        message_id=base.message_id,
        event_id=base.event_id,
        source=EventSource.GITHUB,
        event_type="issue_comment:created",
        ticket_key=base.ticket_key,
        payload={
            **base.payload,
            "comment": {"body": f"/forge unskip-gate {check_name}"},
            "issue": {"number": 42, "pull_request": {}},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "eshulman2"},
        },
    )


@pytest.fixture
def worker():
    return OrchestratorWorker(consumer_name="test-worker")


@pytest.fixture
def base_message():
    return QueueMessage(
        message_id="1234567890-0",
        event_id="test-event-001",
        source=EventSource.GITHUB,
        event_type="issue_comment",
        ticket_key="TEST-123",
        payload={
            "issue": {"key": "TEST-123", "fields": {"issuetype": {"name": "Feature"}}},
        },
    )


@pytest.fixture
def ci_state():
    return make_workflow_state(
        current_node="wait_for_ci_gate",
        current_repo="org/repo",
        current_pr_number=42,
        ci_failed_checks=[
            {"name": "Run acceptance tests against OpenStack epoxy", "conclusion": "failure"},
            {"name": "Run acceptance tests against OpenStack flamingo", "conclusion": "failure"},
        ],
        is_paused=True,
    )


# ── State field ───────────────────────────────────────────────────────────────


class TestCISkippedChecksStateField:

    def test_ci_skipped_checks_in_ci_integration_state(self):
        """ci_skipped_checks must be a field in CIIntegrationState."""
        from forge.workflow.base import CIIntegrationState
        assert "ci_skipped_checks" in CIIntegrationState.__annotations__

    def test_initial_feature_state_has_empty_skipped_checks(self):
        """Fresh feature state initialises ci_skipped_checks to []."""
        from forge.models.workflow import TicketType
        from forge.workflow.feature.state import create_initial_feature_state
        state = create_initial_feature_state(
            thread_id="t", ticket_key="TEST-1", ticket_type=TicketType.FEATURE
        )
        assert state.get("ci_skipped_checks") == []

    def test_initial_bug_state_has_empty_skipped_checks(self):
        """Fresh bug state initialises ci_skipped_checks to []."""
        from forge.models.workflow import TicketType
        from forge.workflow.bug.state import create_initial_bug_state
        state = create_initial_bug_state(
            thread_id="t", ticket_key="TEST-2", ticket_type=TicketType.BUG
        )
        assert state.get("ci_skipped_checks") == []


# ── Worker: /forge skip-gate detection ───────────────────────────────────────


class TestWorkerSkipGateDetection:

    @pytest.mark.asyncio
    async def test_skip_gate_adds_check_to_skipped_list(
        self, worker, base_message, ci_state
    ):
        """/forge skip-gate appends the check name to ci_skipped_checks."""
        msg = _skip_gate_message(base_message, "epoxy")

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        assert "epoxy" in result.get("ci_skipped_checks", [])

    @pytest.mark.asyncio
    async def test_skip_gate_routes_to_ci_evaluator(
        self, worker, base_message, ci_state
    ):
        """/forge skip-gate unpauses and routes to ci_evaluator."""
        msg = _skip_gate_message(base_message, "epoxy")

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        assert result["is_paused"] is False
        assert result["current_node"] == "ci_evaluator"

    @pytest.mark.asyncio
    async def test_unskip_gate_removes_check_from_skipped_list(
        self, worker, base_message, ci_state
    ):
        """/forge unskip-gate removes the matching check name."""
        ci_state["ci_skipped_checks"] = ["epoxy", "flamingo"]
        msg = _unskip_gate_message(base_message, "epoxy")

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        skipped = result.get("ci_skipped_checks", [])
        assert "epoxy" not in skipped
        assert "flamingo" in skipped

    @pytest.mark.asyncio
    async def test_skip_gate_deduplicates(
        self, worker, base_message, ci_state
    ):
        """Skipping the same check twice doesn't add a duplicate."""
        ci_state["ci_skipped_checks"] = ["epoxy"]
        msg = _skip_gate_message(base_message, "epoxy")

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        assert result["ci_skipped_checks"].count("epoxy") == 1

    @pytest.mark.asyncio
    async def test_skip_gate_ignored_outside_ci_stages(
        self, worker, base_message
    ):
        """/forge skip-gate has no effect when workflow is not at a CI stage."""
        planning_state = make_workflow_state(
            current_node="prd_approval_gate",
            is_paused=True,
        )
        msg = _skip_gate_message(base_message, "epoxy")

        result = await worker._handle_resume_event(msg, planning_state)

        assert result.get("ci_skipped_checks", []) == []
        assert result.get("is_paused") is True  # unchanged

    @pytest.mark.asyncio
    async def test_skip_gate_posts_feedback(
        self, worker, base_message, ci_state
    ):
        """/forge skip-gate calls _post_skip_gate_feedback."""
        msg = _skip_gate_message(base_message, "epoxy")
        mock_feedback = AsyncMock()

        with patch.object(worker, "_post_skip_gate_feedback", mock_feedback):
            await worker._handle_resume_event(msg, ci_state)

        mock_feedback.assert_called_once()

    @pytest.mark.asyncio
    async def test_case_insensitive_command_detection(
        self, worker, base_message, ci_state
    ):
        """Command prefix matching is case-insensitive."""
        msg = _skip_gate_message(base_message, "epoxy")
        msg = QueueMessage(
            message_id=msg.message_id,
            event_id=msg.event_id,
            source=msg.source,
            event_type=msg.event_type,
            ticket_key=msg.ticket_key,
            payload={
                **msg.payload,
                "comment": {"body": "/FORGE SKIP-GATE epoxy"},
            },
        )

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        assert "epoxy" in result.get("ci_skipped_checks", [])


# ── _post_skip_gate_feedback ─────────────────────────────────────────────────


class TestPostSkipGateFeedback:

    @pytest.mark.asyncio
    async def test_posts_github_reply_and_jira_comment(self):
        """Posts a GitHub PR comment and a Jira audit comment."""
        worker = OrchestratorWorker(consumer_name="test")

        mock_github = MagicMock()
        mock_github.create_issue_comment = AsyncMock()
        mock_github.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with patch("forge.orchestrator.worker.GitHubClient", return_value=mock_github), \
             patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira):
            await worker._post_skip_gate_feedback(
                ticket_key="TEST-123",
                owner="org",
                repo="repo",
                pr_number=42,
                check_name="epoxy",
                sender="eshulman2",
                action="skip",
            )

        mock_github.create_issue_comment.assert_called_once()
        mock_jira.add_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_unskip_posts_different_message(self):
        """Unskip action produces a different confirmation message."""
        worker = OrchestratorWorker(consumer_name="test")

        mock_github = MagicMock()
        mock_github.create_issue_comment = AsyncMock()
        mock_github.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with patch("forge.orchestrator.worker.GitHubClient", return_value=mock_github), \
             patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira):
            await worker._post_skip_gate_feedback(
                ticket_key="TEST-123",
                owner="org",
                repo="repo",
                pr_number=42,
                check_name="epoxy",
                sender="eshulman2",
                action="unskip",
            )

        comment = mock_github.create_issue_comment.call_args[0][3]
        assert "unskip" in comment.lower() or "removed" in comment.lower()


# ── CI evaluator: filtering skipped checks ────────────────────────────────────


class TestEvaluateCIStatusSkipsChecks:

    @pytest.mark.asyncio
    async def test_skipped_check_does_not_count_as_failure(self):
        """A check whose name matches a ci_skipped_checks entry is treated as passing."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["epoxy"],
        )

        mock_github = MagicMock()
        mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc"}})
        mock_github.get_check_runs = AsyncMock(return_value=[
            {"name": "Run acceptance tests against OpenStack epoxy",
             "status": "completed", "conclusion": "failure"},
            {"name": "Run acceptance tests against OpenStack flamingo",
             "status": "completed", "conclusion": "success"},
        ])
        mock_github.close = AsyncMock()

        with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
            result = await evaluate_ci_status(state)

        # Epoxy is skipped, flamingo passed — CI should be "passed"
        assert result["ci_status"] == "passed"

    @pytest.mark.asyncio
    async def test_all_skipped_checks_plus_pass_routes_to_human_review(self):
        """When remaining non-skipped checks all pass, CI is considered passed."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["epoxy", "flamingo"],
        )

        mock_github = MagicMock()
        mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc"}})
        mock_github.get_check_runs = AsyncMock(return_value=[
            {"name": "Run acceptance tests against OpenStack epoxy",
             "status": "completed", "conclusion": "failure"},
            {"name": "Run acceptance tests against OpenStack flamingo",
             "status": "completed", "conclusion": "failure"},
        ])
        mock_github.close = AsyncMock()

        with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
            result = await evaluate_ci_status(state)

        assert result["ci_status"] == "passed"
        assert result.get("current_node") == "human_review_gate"

    @pytest.mark.asyncio
    async def test_skipped_check_not_in_failed_checks(self):
        """Skipped checks are not included in ci_failed_checks."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["epoxy"],
        )

        mock_github = MagicMock()
        mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc"}})
        mock_github.get_check_runs = AsyncMock(return_value=[
            {"name": "Run acceptance tests against OpenStack epoxy",
             "status": "completed", "conclusion": "failure"},
            {"name": "unit-tests",
             "status": "completed", "conclusion": "failure"},
        ])
        mock_github.close = AsyncMock()

        with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
            result = await evaluate_ci_status(state)

        failed = [c["name"] for c in result.get("ci_failed_checks", [])]
        assert "Run acceptance tests against OpenStack epoxy" not in failed
        assert "unit-tests" in failed

    @pytest.mark.asyncio
    async def test_substring_match_is_case_insensitive(self):
        """Skipped check matching uses case-insensitive substring."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["EPOXY"],  # uppercase skip
        )

        mock_github = MagicMock()
        mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc"}})
        mock_github.get_check_runs = AsyncMock(return_value=[
            {"name": "Run acceptance tests against OpenStack epoxy",
             "status": "completed", "conclusion": "failure"},
        ])
        mock_github.close = AsyncMock()

        with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
            result = await evaluate_ci_status(state)

        assert result["ci_status"] == "passed"

    @pytest.mark.asyncio
    async def test_tide_is_ignored_as_permanent_pending_check(self):
        """tide (Prow merge-queue) is ignored — it stays pending until labels are added.

        A still-running real CI check would cause 'all_passed=False' and wait.
        But tide is a meta-check, not a CI check, so it must not block evaluation.
        """
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["e2e-openstack"],
        )

        mock_github = MagicMock()
        mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc"}})
        mock_github.get_check_runs = AsyncMock(return_value=[
            # Openstack e2e Prow checks — skipped by human override
            {"name": "ci/prow/e2e-openstack-ovn",
             "status": "completed", "conclusion": "failure"},
            # tide — always pending, explicitly filtered by name
            {"name": "tide", "status": "pending", "conclusion": None},
            # Real check that passed
            {"name": "ci/prow/unit", "status": "completed", "conclusion": "success"},
        ])
        mock_github.close = AsyncMock()

        with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
            result = await evaluate_ci_status(state)

        # e2e-openstack skipped, tide ignored, unit passed → CI passes
        assert result["ci_status"] == "passed"
        assert result["current_node"] == "human_review_gate"

    @pytest.mark.asyncio
    async def test_real_pending_check_still_blocks_evaluation(self):
        """A genuinely still-running real check (not tide) still causes a wait."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["e2e-openstack"],
        )

        mock_github = MagicMock()
        mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc"}})
        mock_github.get_check_runs = AsyncMock(return_value=[
            {"name": "ci/prow/e2e-openstack-ovn",
             "status": "completed", "conclusion": "failure"},
            # golint still running — real check, must block
            {"name": "ci/prow/golint", "status": "in_progress", "conclusion": None},
        ])
        mock_github.close = AsyncMock()

        with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
            result = await evaluate_ci_status(state)

        # golint not done → still pending, don't declare passed yet
        assert result["ci_status"] != "passed"

    @pytest.mark.asyncio
    async def test_empty_skipped_checks_behaves_normally(self):
        """With no skipped checks the evaluator behaves exactly as before."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=[],
        )

        mock_github = MagicMock()
        mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc"}})
        mock_github.get_check_runs = AsyncMock(return_value=[
            {"name": "unit-tests", "status": "completed", "conclusion": "failure"},
        ])
        mock_github.close = AsyncMock()

        with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
            result = await evaluate_ci_status(state)

        assert result["ci_status"] == "fixing"
