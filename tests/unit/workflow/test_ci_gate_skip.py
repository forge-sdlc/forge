"""Tests for CI gate skip via GitHub PR comment (proposal 005)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckConclusion,
    CheckRun,
    CheckStatus,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    ReviewComment,
)
from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage, normalized_event_to_dict
from tests.fixtures.workflow_states import make_workflow_state

# ── Helpers ───────────────────────────────────────────────────────────────────


def _comment_message(base: QueueMessage, body: str) -> QueueMessage:
    """Source-control comment event (COMMENT_CREATED) carrying a PR comment body."""
    event = NormalizedEvent(
        id=base.event_id,
        kind=EventKind.COMMENT_CREATED,
        repo_ref=RepositoryRef(
            id="org/repo",
            provider=Provider.GITHUB,
            connection="default-github",
            namespace="org/repo",
            default_branch="main",
            change_request_mode="fork",
        ),
        actor=Actor(login="eshulman2", is_bot=False),
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        change_request=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="default-github", repository_id="org/repo", native_id=42
            ),
            url="https://github.com/org/repo/pull/42",
            title="t",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="feature",
            target_branch="main",
            draft=False,
        ),
        comment=ReviewComment(id="c1", body=body, author="eshulman2"),
    )
    return QueueMessage(
        message_id=base.message_id,
        event_id=base.event_id,
        source=EventSource.SOURCE_CONTROL,
        event_type="comment_created",
        ticket_key=base.ticket_key,
        payload={},
        normalized_event=normalized_event_to_dict(event),
    )


def _skip_gate_message(base: QueueMessage, check_name: str) -> QueueMessage:
    """Source-control comment event with /forge skip-gate command."""
    return _comment_message(base, f"/forge skip-gate {check_name}")


def _unskip_gate_message(base: QueueMessage, check_name: str) -> QueueMessage:
    """Source-control comment event with /forge unskip-gate command."""
    return _comment_message(base, f"/forge unskip-gate {check_name}")


@pytest.fixture
def worker():
    return OrchestratorWorker(consumer_name="test-worker")


@pytest.fixture
def base_message():
    return QueueMessage(
        message_id="1234567890-0",
        event_id="test-event-001",
        source=EventSource.SOURCE_CONTROL,
        event_type="comment_created",
        ticket_key="TEST-123",
        payload={},
    )


@pytest.fixture
def ci_state():
    return make_workflow_state(
        current_node="human_review_gate",
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
    async def test_skip_gate_adds_check_to_skipped_list(self, worker, base_message, ci_state):
        """/forge skip-gate appends the check name to ci_skipped_checks."""
        msg = _skip_gate_message(base_message, "epoxy")

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        assert "epoxy" in result.get("ci_skipped_checks", [])

    @pytest.mark.asyncio
    async def test_skip_gate_routes_to_ci_evaluator(self, worker, base_message, ci_state):
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
    async def test_skip_gate_deduplicates(self, worker, base_message, ci_state):
        """Skipping the same check twice doesn't add a duplicate."""
        ci_state["ci_skipped_checks"] = ["epoxy"]
        msg = _skip_gate_message(base_message, "epoxy")

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        assert result["ci_skipped_checks"].count("epoxy") == 1

    @pytest.mark.asyncio
    async def test_skip_gate_ignored_outside_ci_stages(self, worker, base_message):
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
    async def test_skip_gate_posts_feedback(self, worker, base_message, ci_state):
        """/forge skip-gate calls _post_skip_gate_feedback."""
        msg = _skip_gate_message(base_message, "epoxy")
        mock_feedback = AsyncMock()

        with patch.object(worker, "_post_skip_gate_feedback", mock_feedback):
            await worker._handle_resume_event(msg, ci_state)

        mock_feedback.assert_called_once()

    @pytest.mark.asyncio
    async def test_case_insensitive_command_detection(self, worker, base_message, ci_state):
        """Command prefix matching is case-insensitive."""
        msg = _comment_message(base_message, "/FORGE SKIP-GATE epoxy")

        with patch.object(worker, "_post_skip_gate_feedback", AsyncMock()):
            result = await worker._handle_resume_event(msg, ci_state)

        assert "epoxy" in result.get("ci_skipped_checks", [])


# ── _post_skip_gate_feedback ─────────────────────────────────────────────────


class TestPostSkipGateFeedback:
    @pytest.mark.asyncio
    async def test_posts_github_reply_and_jira_comment(self):
        """Posts a GitHub PR comment and a Jira audit comment."""
        worker = OrchestratorWorker(consumer_name="test")

        repo_ref = RepositoryRef(
            id="org/repo",
            provider=Provider.GITHUB,
            connection="default-github",
            namespace="org/repo",
            default_branch="main",
            change_request_mode="fork",
        )
        mock_adapter = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.orchestrator.worker.get_adapter", return_value=(repo_ref, mock_adapter)),
            patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira),
        ):
            await worker._post_skip_gate_feedback(
                ticket_key="TEST-123",
                repo_ref=repo_ref,
                pr_number=42,
                check_name="epoxy",
                sender="eshulman2",
                action="skip",
            )

        mock_adapter.create_comment.assert_called_once()
        mock_jira.add_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_unskip_posts_different_message(self):
        """Unskip action produces a different confirmation message."""
        worker = OrchestratorWorker(consumer_name="test")

        repo_ref = RepositoryRef(
            id="org/repo",
            provider=Provider.GITHUB,
            connection="default-github",
            namespace="org/repo",
            default_branch="main",
            change_request_mode="fork",
        )
        mock_adapter = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.orchestrator.worker.get_adapter", return_value=(repo_ref, mock_adapter)),
            patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira),
        ):
            await worker._post_skip_gate_feedback(
                ticket_key="TEST-123",
                repo_ref=repo_ref,
                pr_number=42,
                check_name="epoxy",
                sender="eshulman2",
                action="unskip",
            )

        comment = mock_adapter.create_comment.call_args[0][2]
        assert "unskip" in comment.lower() or "removed" in comment.lower()


# ── CI evaluator: filtering skipped checks ────────────────────────────────────


_STATUS_MAP = {
    "completed": CheckStatus.COMPLETED,
    "in_progress": CheckStatus.IN_PROGRESS,
    "pending": CheckStatus.QUEUED,
}
_CONCLUSION_MAP = {
    "success": CheckConclusion.SUCCESS,
    "failure": CheckConclusion.FAILURE,
    "skipped": CheckConclusion.SKIPPED,
    "neutral": CheckConclusion.NEUTRAL,
    None: CheckConclusion.NONE,
}


def _check_run(name: str, status: str, conclusion: str | None) -> CheckRun:
    return CheckRun(
        name=name,
        status=_STATUS_MAP[status],
        conclusion=_CONCLUSION_MAP[conclusion],
    )


def _mock_adapter_with_checks(checks: list[CheckRun], repo="org/repo", pr_number=42):
    """Patch target for ci_evaluator.get_adapter returning a fixed check list."""
    adapter = AsyncMock()
    adapter.get_change_request = AsyncMock(
        return_value=ChangeRequest(
            identity=ChangeRequestIdentity(connection="c", repository_id=repo, native_id=pr_number),
            url=f"https://github.com/{repo}/pull/{pr_number}",
            title="t",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="feature",
            target_branch="main",
        )
    )
    adapter.get_checks = AsyncMock(return_value=checks)
    repo_ref = RepositoryRef(
        id=repo,
        provider=Provider.GITHUB,
        connection="c",
        namespace=repo,
        default_branch="main",
        change_request_mode="fork",
    )
    return repo_ref, adapter


class TestEvaluateCIStatusSkipsChecks:
    @pytest.mark.asyncio
    async def test_skipped_check_does_not_count_as_failure(self):
        """A check whose name matches a ci_skipped_checks entry is treated as passing."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            current_repo="org/repo",
            current_pr_number=42,
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["epoxy"],
        )

        repo_ref, adapter = _mock_adapter_with_checks(
            [
                _check_run("Run acceptance tests against OpenStack epoxy", "completed", "failure"),
                _check_run(
                    "Run acceptance tests against OpenStack flamingo", "completed", "success"
                ),
            ]
        )

        with patch(
            "forge.workflow.nodes.ci_evaluator.get_adapter", return_value=(repo_ref, adapter)
        ):
            result = await evaluate_ci_status(state)

        # Epoxy is skipped, flamingo passed — CI should be "passed"
        assert result["ci_status"] == "passed"

    @pytest.mark.asyncio
    async def test_all_skipped_checks_plus_pass_routes_to_human_review(self):
        """When remaining non-skipped checks all pass, CI is considered passed."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            current_repo="org/repo",
            current_pr_number=42,
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["epoxy", "flamingo"],
        )

        repo_ref, adapter = _mock_adapter_with_checks(
            [
                _check_run("Run acceptance tests against OpenStack epoxy", "completed", "failure"),
                _check_run(
                    "Run acceptance tests against OpenStack flamingo", "completed", "failure"
                ),
            ]
        )

        with patch(
            "forge.workflow.nodes.ci_evaluator.get_adapter", return_value=(repo_ref, adapter)
        ):
            result = await evaluate_ci_status(state)

        assert result["ci_status"] == "passed"
        assert result.get("current_node") == "human_review_gate"

    @pytest.mark.asyncio
    async def test_skipped_check_not_in_failed_checks(self):
        """Skipped checks are not included in ci_failed_checks."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            current_repo="org/repo",
            current_pr_number=42,
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["epoxy"],
        )

        repo_ref, adapter = _mock_adapter_with_checks(
            [
                _check_run("Run acceptance tests against OpenStack epoxy", "completed", "failure"),
                _check_run("unit-tests", "completed", "failure"),
            ]
        )

        with patch(
            "forge.workflow.nodes.ci_evaluator.get_adapter", return_value=(repo_ref, adapter)
        ):
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
            current_repo="org/repo",
            current_pr_number=42,
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["EPOXY"],  # uppercase skip
        )

        repo_ref, adapter = _mock_adapter_with_checks(
            [
                _check_run("Run acceptance tests against OpenStack epoxy", "completed", "failure"),
            ]
        )

        with patch(
            "forge.workflow.nodes.ci_evaluator.get_adapter", return_value=(repo_ref, adapter)
        ):
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
            current_repo="org/repo",
            current_pr_number=42,
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["e2e-openstack"],
        )

        repo_ref, adapter = _mock_adapter_with_checks(
            [
                # Openstack e2e Prow checks — skipped by human override
                _check_run("ci/prow/e2e-openstack-ovn", "completed", "failure"),
                # tide — always pending, explicitly filtered by name
                _check_run("tide", "pending", None),
                # Real check that passed
                _check_run("ci/prow/unit", "completed", "success"),
            ]
        )

        with patch(
            "forge.workflow.nodes.ci_evaluator.get_adapter", return_value=(repo_ref, adapter)
        ):
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
            current_repo="org/repo",
            current_pr_number=42,
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=["e2e-openstack"],
        )

        repo_ref, adapter = _mock_adapter_with_checks(
            [
                _check_run("ci/prow/e2e-openstack-ovn", "completed", "failure"),
                # golint still running — real check, must block
                _check_run("ci/prow/golint", "in_progress", None),
            ]
        )

        with patch(
            "forge.workflow.nodes.ci_evaluator.get_adapter", return_value=(repo_ref, adapter)
        ):
            result = await evaluate_ci_status(state)

        # golint not done → still pending, don't declare passed yet
        assert result["ci_status"] != "passed"

    @pytest.mark.asyncio
    async def test_empty_skipped_checks_behaves_normally(self):
        """With no skipped checks the evaluator behaves exactly as before."""
        from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

        state = make_workflow_state(
            current_node="ci_evaluator",
            current_repo="org/repo",
            current_pr_number=42,
            pr_urls=["https://github.com/org/repo/pull/42"],
            ci_skipped_checks=[],
        )

        repo_ref, adapter = _mock_adapter_with_checks(
            [
                _check_run("unit-tests", "completed", "failure"),
            ]
        )

        with patch(
            "forge.workflow.nodes.ci_evaluator.get_adapter", return_value=(repo_ref, adapter)
        ):
            result = await evaluate_ci_status(state)

        assert result["ci_status"] == "fixing"
