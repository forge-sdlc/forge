"""Unit tests for local_review_changes bug-specific enhancements."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.observability.review_poller import ReviewCycleData
from forge.workflow.nodes.local_reviewer import (
    _parse_bug_verdict,
    local_review_changes,
    route_local_review,
)

pytestmark = pytest.mark.usefixtures("mock_review_workspace_recovery")


@pytest.fixture
def base_bug_review_state():
    return {
        "ticket_key": "BUG-42",
        "ticket_type": TicketType.BUG,
        "current_node": "local_review",
        "is_paused": False,
        "workspace_path": "/tmp/fake-workspace",
        "current_repo": "acme/backend",
        "rca_content": "Password validator rejects special chars.",
        "selected_fix_approach": {
            "title": "Fix regex",
            "description": "Update VALID_PASSWORD_PATTERN.",
            "tradeoffs": "Low risk.",
        },
        "plan_content": "## Plan\n\nFix regex in validators.py.",
        "local_review_verdict": None,
        "qualitative_feedback": None,
        "qualitative_retry_count": 0,
        "qualitative_review_failed": False,
        "context": {"branch_name": "fix/BUG-42"},
        "retry_count": 0,
        "last_error": None,
        "fork_owner": "forge-bot",
        "fork_repo": "backend",
    }


@pytest.fixture
def base_feature_review_state():
    return {
        "ticket_key": "FEAT-10",
        "ticket_type": TicketType.FEATURE,
        "current_node": "local_review",
        "is_paused": False,
        "workspace_path": "/tmp/fake-workspace",
        "current_repo": "acme/backend",
        "spec_content": "Feature spec here.",
        "local_review_attempts": 0,
        "context": {"branch_name": "feat/FEAT-10"},
        "retry_count": 0,
        "last_error": None,
        "fork_owner": "forge-bot",
        "fork_repo": "backend",
    }


def _make_mock_runner(stdout="verdict: adequate\n\nfeedback: Looks good."):
    class _FakeRunner:
        async def run(self, workspace_path, **_kwargs):  # noqa: ARG002
            result = MagicMock()
            result.success = True
            result.exit_code = 0
            result.stdout = stdout
            result.stderr = ""
            result.review_cycles = []
            result.review_exhausted = False
            return result

    return _FakeRunner()


def _make_mock_git(has_changes=False):
    git = MagicMock()
    git.has_uncommitted_changes.return_value = has_changes
    git.stage_all = MagicMock()
    git.commit = MagicMock()
    return git


class TestParseBugVerdict:
    """Tests for the _parse_bug_verdict helper."""

    def test_parses_adequate(self):
        output = "verdict: adequate\n\nfeedback: Everything is correct."
        verdict, feedback = _parse_bug_verdict(output)
        assert verdict == "adequate"
        assert "Everything is correct" in feedback

    def test_parses_tests_incomplete(self):
        output = "verdict: tests_incomplete\n\nfeedback: Tests do not fail without the fix."
        verdict, feedback = _parse_bug_verdict(output)
        assert verdict == "tests_incomplete"

    def test_parses_symptom_only(self):
        output = "verdict: symptom_only\n\nfeedback: Root cause not addressed."
        verdict, feedback = _parse_bug_verdict(output)
        assert verdict == "symptom_only"

    def test_unknown_verdict_defaults_to_tests_incomplete(self):
        """Unrecognized or absent verdict defaults to tests_incomplete to avoid skipping quality gate."""
        output = "No verdict line present."
        verdict, feedback = _parse_bug_verdict(output)
        assert verdict == "tests_incomplete"

    def test_case_insensitive_verdict(self):
        output = "Verdict: Adequate\n\nfeedback: Ok."
        verdict, feedback = _parse_bug_verdict(output)
        assert verdict == "adequate"

    def test_backtick_and_literal_escape_after_verdict_parses_correctly(self):
        """LLM output with trailing backtick and literal \\n-dash is still parsed."""
        # The container agent outputs `verdict: adequate`\n- next bullet` where
        # \n is the two-character sequence backslash-n (not a real newline).
        output = "verdict: adequate`\\n- next section"
        verdict, _ = _parse_bug_verdict(output)
        assert verdict == "adequate"

    def test_verdict_in_inline_code_backticks_parses_correctly(self):
        """Verdict wrapped in markdown inline code backticks is parsed."""
        output = "verdict: `adequate`"
        verdict, _ = _parse_bug_verdict(output)
        assert verdict == "adequate"


class TestLocalReviewBug:
    """Tests for bug-specific local review behavior."""

    @pytest.mark.asyncio
    async def test_bug_uses_bug_prompt(self, base_bug_review_state):
        """TicketType.BUG → local-review-bug.md prompt used."""
        captured_desc = []

        class _CapturingRunner:
            async def run(self, workspace_path, task_description="", **_kwargs):  # noqa: ARG002
                captured_desc.append(task_description)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "verdict: adequate\n\nfeedback: Good."
                result.stderr = ""
                result.review_cycles = []
                result.review_exhausted = False
                return result

        mock_git = _make_mock_git()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_CapturingRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            await local_review_changes(base_bug_review_state)

        assert captured_desc, "runner.run was not called"
        desc = captured_desc[0]
        # Bug-specific context should be in description
        assert "Password validator" in desc or "Fix regex" in desc or "validators.py" in desc

    @pytest.mark.asyncio
    async def test_adequate_verdict_routes_to_create_pr(self, base_bug_review_state):
        """'adequate' verdict → routes to create_pr."""
        runner = _make_mock_runner("verdict: adequate\n\nfeedback: Looks good.")
        mock_git = _make_mock_git()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "create_pr"
        assert result["local_review_verdict"] == "adequate"
        mock_git.push_to_fork.assert_called_once()

    @pytest.mark.asyncio
    async def test_transient_push_resumes_without_rerunning_review(
        self, base_bug_review_state, tmp_path
    ):
        """A review verdict is checkpointed while only its push is retried."""
        base_bug_review_state["workspace_path"] = str(tmp_path)
        runner = _make_mock_runner("verdict: adequate\nfeedback: Looks good.")
        mock_git = _make_mock_git()
        mock_git.push_to_fork.side_effect = [
            RuntimeError("connection reset"),
            RuntimeError("connection reset"),
            RuntimeError("connection reset"),
            None,
        ]

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.prepare_workspace",
                return_value=(str(tmp_path), mock_git),
            ),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch(
                "forge.workflow.nodes.git_persistence.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            pending = await local_review_changes(base_bug_review_state)
            assert pending["review_push_pending"] is True
            assert pending["current_node"] == "local_review"

            with patch("forge.workflow.nodes.local_reviewer.ContainerRunner") as rerun:
                completed = await local_review_changes(pending)

        rerun.assert_not_called()
        assert completed["review_push_pending"] is False
        assert completed["current_node"] == "create_pr"
        assert completed["local_review_verdict"] == "adequate"

    @pytest.mark.asyncio
    async def test_tests_incomplete_increments_retry(self, base_bug_review_state):
        """'tests_incomplete' verdict → qualitative_retry_count incremented, routes to implement_bug_fix."""
        runner = _make_mock_runner(
            "verdict: tests_incomplete\n\nfeedback: Tests do not fail without fix."
        )
        mock_git = _make_mock_git()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "implement_bug_fix"
        assert result["qualitative_retry_count"] == 1
        assert result["local_review_verdict"] == "tests_incomplete"
        assert "Tests do not fail" in (result["qualitative_feedback"] or "")

    @pytest.mark.asyncio
    async def test_symptom_only_increments_retry(self, base_bug_review_state):
        """'symptom_only' verdict → qualitative_retry_count incremented, routes to implement_bug_fix."""
        runner = _make_mock_runner("verdict: symptom_only\n\nfeedback: Root cause not addressed.")
        mock_git = _make_mock_git()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "implement_bug_fix"
        assert result["qualitative_retry_count"] == 1

    @pytest.mark.asyncio
    async def test_retry_uses_task_keys_when_linked_task_keys_empty(self, base_bug_review_state):
        """When linked_task_keys is empty, task_keys is used to reset current_task_key on retry."""
        base_bug_review_state["linked_task_keys"] = []
        base_bug_review_state["task_keys"] = ["TASK-789"]
        runner = _make_mock_runner("verdict: tests_incomplete\n\nfeedback: Missing tests.")
        mock_git = _make_mock_git()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_task_key"] == "TASK-789"

    @pytest.mark.asyncio
    async def test_cap_at_two_retries_routes_to_create_pr(self, base_bug_review_state):
        """qualitative_retry_count >= 2 → routes to create_pr with qualitative_review_failed=True."""
        base_bug_review_state["qualitative_retry_count"] = 1  # Already 1, will become 2 → cap
        runner = _make_mock_runner("verdict: tests_incomplete\n\nfeedback: Still missing tests.")
        mock_git = _make_mock_git()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "create_pr"
        assert result["qualitative_review_failed"] is True
        assert result["qualitative_retry_count"] == 2


class TestRouteLocalReview:
    """Tests for the route_local_review routing function."""

    def test_routes_based_on_current_node(self):
        """route_local_review returns state's current_node."""
        state = {"current_node": "implement_bug_fix"}
        assert route_local_review(state) == "implement_bug_fix"

    def test_defaults_to_create_pr(self):
        """route_local_review defaults to create_pr when current_node not set."""
        state = {}
        assert route_local_review(state) == "create_pr"


class TestBugReviewExceptionHandling:
    @pytest.mark.asyncio
    async def test_exception_preserves_last_error(self, base_bug_review_state):
        """Container exception sets last_error in state (not None)."""

        class _FailingRunner:
            async def run(self, **_kwargs):
                raise RuntimeError("Container crashed")

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=_FailingRunner()
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=MagicMock()),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "create_pr"
        assert result["last_error"] is not None
        assert "Container crashed" in result["last_error"]

    @pytest.mark.asyncio
    async def test_exception_clears_stale_verdict(self, base_bug_review_state):
        """Exception handler must clear local_review_verdict to prevent stale routing."""
        base_bug_review_state["local_review_verdict"] = "tests_incomplete"
        base_bug_review_state["qualitative_retry_count"] = 1

        class _FailingRunner:
            async def run(self, **_kwargs):
                raise RuntimeError("OOM")

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=_FailingRunner()
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=MagicMock()),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "create_pr"
        assert result["local_review_verdict"] is None


class TestLocalReviewFeature:
    """Tests that feature tickets use existing non-bug behavior."""

    @pytest.mark.asyncio
    async def test_feature_uses_existing_prompt(self, base_feature_review_state):
        """Feature ticket → existing prompt behavior (no qualitative verdict parsing)."""
        captured_desc = []

        class _CapturingRunner:
            async def run(self, workspace_path, task_description="", **_kwargs):  # noqa: ARG002
                captured_desc.append(task_description)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "No issues found."
                result.stderr = ""
                result.review_cycles = []
                result.review_exhausted = False
                return result

        mock_git = _make_mock_git()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_CapturingRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_feature_review_state)

        assert result["current_node"] == "create_pr"
        assert result.get("local_review_verdict") is None
        mock_git.push_to_fork.assert_called_once()

    @pytest.mark.asyncio
    async def test_feature_no_qualitative_fields_set(self, base_feature_review_state):
        """Feature ticket → qualitative_retry_count and verdict not modified."""
        runner = _make_mock_runner("verdict: adequate\n\nfeedback: Good.")
        mock_git = _make_mock_git()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_feature_review_state)

        # Feature state should not have qualitative fields modified
        assert (
            "qualitative_retry_count" not in result or result.get("qualitative_retry_count") is None
        )
        assert "local_review_verdict" not in result or result.get("local_review_verdict") is None


class TestLocalReviewStepName:
    """Tests for step_name propagation to ContainerRunner."""

    @pytest.mark.asyncio
    async def test_bug_review_passes_step_name_local_review(self, base_bug_review_state):
        """Bug qualitative review passes step_name='local_review' to ContainerRunner."""
        captured_kwargs = []

        class _CapturingRunner:
            async def run(self, **kwargs):
                captured_kwargs.append(kwargs)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "verdict: adequate\n\nfeedback: Good."
                result.stderr = ""
                result.review_cycles = []
                result.review_exhausted = False
                return result

        mock_git = _make_mock_git()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_CapturingRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            await local_review_changes(base_bug_review_state)

        assert captured_kwargs, "runner.run was not called"
        assert captured_kwargs[0].get("step_name") == "local_review"

    @pytest.mark.asyncio
    async def test_feature_review_passes_step_name_local_review(self, base_feature_review_state):
        """Feature review passes step_name='local_review' to ContainerRunner."""
        captured_kwargs = []

        class _CapturingRunner:
            async def run(self, **kwargs):
                captured_kwargs.append(kwargs)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "No issues found."
                result.stderr = ""
                result.review_cycles = []
                result.review_exhausted = False
                return result

        mock_git = _make_mock_git()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_CapturingRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            await local_review_changes(base_feature_review_state)

        assert captured_kwargs, "runner.run was not called"
        assert captured_kwargs[0].get("step_name") == "local_review"


class TestMergeReviewExhaustionIntegration:
    """Tests that review_exhaustion_report is populated when review cycles are exhausted."""

    @pytest.mark.asyncio
    async def test_bug_review_populates_exhaustion_report(self, base_bug_review_state):
        """Bug review with exhausted cycles populates review_exhaustion_report in state."""
        exhausted_cycles = [
            ReviewCycleData(
                cycle=1,
                max_cycles=2,
                verdict="rejected",
                feedback="Tests missing for edge case.",
                skill="local-review-bug",
                elapsed_seconds=12.5,
                timestamp="2026-07-16T10:00:00Z",
            ),
            ReviewCycleData(
                cycle=2,
                max_cycles=2,
                verdict="rejected",
                feedback="Still missing edge case coverage.",
                skill="local-review-bug",
                elapsed_seconds=14.0,
                timestamp="2026-07-16T10:01:00Z",
            ),
        ]

        class _ExhaustedRunner:
            async def run(self, **_kwargs):
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "verdict: adequate\n\nfeedback: Good."
                result.stderr = ""
                result.review_cycles = exhausted_cycles
                result.review_exhausted = True
                return result

        mock_git = _make_mock_git()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_ExhaustedRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_bug_review_state)

        report = result.get("review_exhaustion_report", {})
        assert "BUG-42__local_review" in report
        entry = report["BUG-42__local_review"]
        assert entry["task_key"] == "BUG-42"
        assert entry["step_name"] == "local_review"

    @pytest.mark.asyncio
    async def test_feature_review_populates_exhaustion_report(self, base_feature_review_state):
        """Feature review with exhausted cycles populates review_exhaustion_report in state."""
        exhausted_cycles = [
            ReviewCycleData(
                cycle=1,
                max_cycles=2,
                verdict="rejected",
                feedback="Breaking issue not resolved.",
                skill="local-code-review",
                elapsed_seconds=10.0,
                timestamp="2026-07-16T11:00:00Z",
            ),
            ReviewCycleData(
                cycle=2,
                max_cycles=2,
                verdict="rejected",
                feedback="Breaking issue persists after retry.",
                skill="local-code-review",
                elapsed_seconds=11.0,
                timestamp="2026-07-16T11:01:00Z",
            ),
        ]

        class _ExhaustedRunner:
            async def run(self, **_kwargs):
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "No issues found."
                result.stderr = ""
                result.review_cycles = exhausted_cycles
                result.review_exhausted = True
                return result

        mock_git = _make_mock_git()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_ExhaustedRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(base_feature_review_state)

        report = result.get("review_exhaustion_report", {})
        assert "FEAT-10__local_review" in report
        entry = report["FEAT-10__local_review"]
        assert entry["task_key"] == "FEAT-10"
        assert entry["step_name"] == "local_review"
