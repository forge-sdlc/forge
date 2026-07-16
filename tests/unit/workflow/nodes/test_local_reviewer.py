"""Unit tests for local_review_changes bug-specific enhancements."""

from unittest.mock import MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.nodes.local_reviewer import (
    _parse_bug_verdict,
    _parse_feature_verdict,
    local_review_changes,
    route_local_review,
)


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
    }


def _make_mock_runner(stdout="verdict: adequate\n\nfeedback: Looks good."):
    class _FakeRunner:
        async def run(self, workspace_path, **_kwargs):  # noqa: ARG002
            result = MagicMock()
            result.success = True
            result.exit_code = 0
            result.stdout = stdout
            result.stderr = ""
            return result

    return _FakeRunner()


def _make_mock_git(has_changes=False):
    git = MagicMock()
    git.has_uncommitted_changes.return_value = has_changes
    git.stage_all = MagicMock()
    git.commit = MagicMock()
    git.reset_hard = MagicMock()
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


class TestParseFeatureVerdict:
    """Tests for the _parse_feature_verdict helper."""

    def test_parses_adequate(self):
        output = "verdict: adequate\n\nfeedback: Everything is correct."
        verdict, feedback = _parse_feature_verdict(output)
        assert verdict == "adequate"
        assert "Everything is correct" in feedback

    def test_parses_tests_incomplete(self):
        output = "verdict: tests_incomplete\n\nfeedback: Missing coverage."
        verdict, feedback = _parse_feature_verdict(output)
        assert verdict == "tests_incomplete"
        assert "Missing coverage" in feedback

    def test_unknown_verdict_defaults_to_tests_incomplete(self):
        output = "verdict: maybe\n\nfeedback: unclear"
        verdict, _ = _parse_feature_verdict(output)
        assert verdict == "tests_incomplete"


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
                return result

        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_CapturingRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            await local_review_changes(base_bug_review_state)

        assert captured_desc, "runner.run was not called"
        desc = captured_desc[0]
        # Bug-specific context should be in description
        assert "Password validator" in desc or "Fix regex" in desc or "validators.py" in desc

    @pytest.mark.asyncio
    async def test_adequate_verdict_records_review_state(self, base_bug_review_state):
        """'adequate' verdict is recorded for graph routing."""
        runner = _make_mock_runner("verdict: adequate\n\nfeedback: Looks good.")
        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "local_review"
        assert result["local_review_verdict"] == "adequate"

    @pytest.mark.asyncio
    async def test_tests_incomplete_increments_retry(self, base_bug_review_state):
        """'tests_incomplete' verdict increments qualitative_retry_count for graph routing."""
        runner = _make_mock_runner(
            "verdict: tests_incomplete\n\nfeedback: Tests do not fail without fix."
        )
        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "local_review"
        assert result["qualitative_retry_count"] == 1
        assert result["local_review_verdict"] == "tests_incomplete"
        assert "Tests do not fail" in (result["qualitative_feedback"] or "")

    @pytest.mark.asyncio
    async def test_symptom_only_increments_retry(self, base_bug_review_state):
        """'symptom_only' verdict increments qualitative_retry_count for graph routing."""
        runner = _make_mock_runner("verdict: symptom_only\n\nfeedback: Root cause not addressed.")
        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "local_review"
        assert result["qualitative_retry_count"] == 1

    @pytest.mark.asyncio
    async def test_retry_leaves_task_key_empty_for_review_feedback_fix(self, base_bug_review_state):
        """Failed review asks implementation for a feedback fix pass, not a task rerun."""
        base_bug_review_state["linked_task_keys"] = []
        base_bug_review_state["task_keys"] = ["TASK-789"]
        runner = _make_mock_runner("verdict: tests_incomplete\n\nfeedback: Missing tests.")
        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_task_key"] is None

    @pytest.mark.asyncio
    async def test_cap_at_two_retries_records_failed_review(self, base_bug_review_state):
        """qualitative_retry_count >= 2 records qualitative_review_failed=True."""
        base_bug_review_state["qualitative_retry_count"] = 1  # Already 1, will become 2 → cap
        runner = _make_mock_runner("verdict: tests_incomplete\n\nfeedback: Still missing tests.")
        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "local_review"
        assert result["qualitative_review_failed"] is True
        assert result["qualitative_retry_count"] == 2

    @pytest.mark.asyncio
    async def test_reviewer_changes_are_discarded_not_committed(self, base_bug_review_state):
        """Bug reviewer output is feedback only; file edits are discarded."""
        runner = _make_mock_runner("verdict: tests_incomplete\n\nfeedback: Missing tests.")
        mock_git = _make_mock_git(has_changes=True)
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            await local_review_changes(base_bug_review_state)

        mock_git.reset_hard.assert_called_once()
        mock_git.stage_all.assert_not_called()
        mock_git.commit.assert_not_called()


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

        mock_workspace = MagicMock()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=_FailingRunner()
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=MagicMock()),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "local_review"
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

        mock_workspace = MagicMock()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=_FailingRunner()
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=MagicMock()),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_bug_review_state)

        assert result["current_node"] == "local_review"
        assert result["local_review_verdict"] is None


class TestLocalReviewFeature:
    """Tests that feature tickets use read-only local review behavior."""

    @pytest.mark.asyncio
    async def test_feature_uses_read_only_prompt(self, base_feature_review_state):
        """Feature ticket → read-only prompt behavior with verdict parsing."""
        captured_desc = []

        class _CapturingRunner:
            async def run(self, workspace_path, task_description="", **_kwargs):  # noqa: ARG002
                captured_desc.append(task_description)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "verdict: adequate\n\nfeedback: No issues found."
                result.stderr = ""
                return result

        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner",
                return_value=_CapturingRunner(),
            ),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_feature_review_state)

        assert result["current_node"] == "local_review"
        assert result["local_review_has_unfixed_issues"] is False
        assert result["local_review_verdict"] == "adequate"

    @pytest.mark.asyncio
    async def test_feature_tests_incomplete_records_feedback(self, base_feature_review_state):
        """Feature reviewer feedback drives a later implementation fix pass."""
        runner = _make_mock_runner("verdict: tests_incomplete\n\nfeedback: Add regression tests.")
        mock_git = _make_mock_git()
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            result = await local_review_changes(base_feature_review_state)

        assert result["local_review_has_unfixed_issues"] is True
        assert result["local_review_verdict"] == "tests_incomplete"
        assert "Add regression tests" in (result["qualitative_feedback"] or "")

    @pytest.mark.asyncio
    async def test_feature_reviewer_changes_are_discarded_not_committed(
        self, base_feature_review_state
    ):
        """Feature reviewer output is feedback only; file edits are discarded."""
        runner = _make_mock_runner("verdict: tests_incomplete\n\nfeedback: Missing tests.")
        mock_git = _make_mock_git(has_changes=True)
        mock_workspace = MagicMock()

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.local_reviewer.Workspace", return_value=mock_workspace),
        ):
            await local_review_changes(base_feature_review_state)

        mock_git.reset_hard.assert_called_once()
        mock_git.stage_all.assert_not_called()
        mock_git.commit.assert_not_called()
