"""Unit and integration tests for Task Takeover Qualitative Review Node."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.nodes.task_takeover_review import (
    _extract_acceptance_criteria,
    _parse_qualitative_review,
    run_qualitative_review,
)
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
    create_initial_task_takeover_state,
)


def make_task_state(**overrides: Any) -> TaskTakeoverState:
    """Create a TaskTakeoverState dict for review tests."""
    state = create_initial_task_takeover_state("TASK-101")
    state_dict = cast(dict[str, Any], state)
    state_dict.update(overrides)
    return cast(TaskTakeoverState, state_dict)


@pytest.fixture
def base_task_state() -> TaskTakeoverState:
    return make_task_state(
        workspace_path="/tmp/fake-workspace-review",
        current_repo="owner/repo",
        context={"branch_name": "task/TASK-101"},
    )


@pytest.fixture(autouse=True)
def mock_workspace_recovery():
    """Keep these review tests focused on review behavior, not cloning."""

    def _prepare(state):
        workspace_path = state.get("workspace_path")
        if not workspace_path:
            raise ValueError("Workspace not set up")
        return workspace_path, MagicMock()

    with patch(
        "forge.workflow.nodes.task_takeover_review.prepare_workspace",
        side_effect=_prepare,
    ):
        yield


def _make_mock_jira(description: str = "Acceptance Criteria:\n- Foo\n- Bar") -> AsyncMock:
    jira = AsyncMock()
    issue = MagicMock()
    issue.summary = "Fix session timeout"
    issue.description = description
    issue.project_key = "TASK"
    jira.get_issue = AsyncMock(return_value=issue)
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()
    return jira


def _make_mock_runner(stdout: str) -> MagicMock:
    runner = MagicMock()
    result = MagicMock()
    result.success = True
    result.exit_code = 0
    result.stdout = stdout
    result.stderr = ""
    result.error_message = None
    runner.run = AsyncMock(return_value=result)
    return runner


class TestParseQualitativeReview:
    """Tests for _parse_qualitative_review helper."""

    def test_parses_adequate_success(self) -> None:
        output = "verdict: adequate\nfeedback: Everything is correct and fully tested."
        verdict, feedback = _parse_qualitative_review(output)
        assert verdict == "adequate"
        assert feedback == "Everything is correct and fully tested."

    def test_parses_tests_incomplete_failure(self) -> None:
        output = "verdict: tests_incomplete\nfeedback: Tests do not fail without the fix."
        verdict, feedback = _parse_qualitative_review(output)
        assert verdict == "tests_incomplete"
        assert feedback == "Tests do not fail without the fix."

    def test_unknown_verdict_defaults_to_tests_incomplete(self) -> None:
        """Unrecognized or absent verdict defaults to tests_incomplete to avoid skipping quality gate."""
        output = "verdict: outstanding\nfeedback: Great work."
        verdict, feedback = _parse_qualitative_review(output)
        assert verdict == "tests_incomplete"
        assert feedback == "Great work."

    def test_case_insensitive_verdict(self) -> None:
        output = "Verdict: Adequate\nfeedback: Well done."
        verdict, feedback = _parse_qualitative_review(output)
        assert verdict == "adequate"
        assert feedback == "Well done."

    def test_backtick_and_literal_escape_after_verdict_parses_correctly(self) -> None:
        """LLM output with trailing backtick and literal \\n-dash is still parsed."""
        output = "verdict: adequate`\\n- next section\nfeedback: Good."
        verdict, feedback = _parse_qualitative_review(output)
        assert verdict == "adequate"
        assert feedback == "Good."

    def test_verdict_in_inline_code_backticks_parses_correctly(self) -> None:
        """Verdict wrapped in markdown inline code backticks is parsed."""
        output = "verdict: `adequate`\nfeedback: Excellent."
        verdict, feedback = _parse_qualitative_review(output)
        assert verdict == "adequate"
        assert feedback == "Excellent."


class TestExtractAcceptanceCriteria:
    """Tests for _extract_acceptance_criteria helper."""

    def test_extract_found(self) -> None:
        desc = "Some setup info.\nAcceptance Criteria:\n1. Must run fast.\n2. Must pass."
        criteria = _extract_acceptance_criteria(desc)
        assert criteria.startswith("Acceptance Criteria:")
        assert "Must pass." in criteria

    def test_extract_not_found(self) -> None:
        desc = "Plain description without the heading."
        criteria = _extract_acceptance_criteria(desc)
        assert criteria == desc

    def test_extract_empty(self) -> None:
        assert _extract_acceptance_criteria("") == "No description or acceptance criteria provided."


class TestRunQualitativeReviewNode:
    """Tests for run_qualitative_review node."""

    @pytest.mark.asyncio
    async def test_run_qualitative_review_success_state_updates(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify state updates when qualitative review passes (verdict is adequate)."""
        mock_jira = _make_mock_jira()
        mock_runner = _make_mock_runner(
            "verdict: adequate\nfeedback: All acceptance criteria met and automated tests verified."
        )

        with (
            patch("forge.workflow.nodes.task_takeover_review.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_review.GitOperations") as mock_git,
            patch("forge.workflow.nodes.task_takeover_review.ContainerRunner", return_value=mock_runner),
        ):
            mock_git_instance = MagicMock()
            mock_git_instance._run_git = MagicMock()
            mock_git_instance._run_git.return_value.returncode = 0
            mock_git_instance._run_git.return_value.stdout = "diff contents"
            mock_git_instance.has_uncommitted_changes = MagicMock(return_value=False)
            mock_git.return_value = mock_git_instance

            result = await run_qualitative_review(base_task_state)

        assert result["review_verdict"] == "adequate"
        assert "All acceptance criteria met" in result["review_feedback"]
        assert result["qualitative_review_retry_count"] == 0
        assert result["qualitative_review_failed"] is False
        assert result["current_node"] == "qualitative_review"
        assert result["last_error"] is None
        mock_jira.add_comment.assert_not_called()
        mock_runner.run.assert_called_once()
        _, kwargs = mock_runner.run.call_args
        assert kwargs["task_key"] == "TASK-101-review"
        assert kwargs["repo_name"] == "owner/repo"
        assert "repo-local review guidance" in kwargs["task_description"]

    @pytest.mark.asyncio
    async def test_run_qualitative_review_failure_state_updates(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify state updates and retry metric increment when review fails."""
        mock_jira = _make_mock_jira()
        mock_runner = _make_mock_runner(
            "verdict: tests_incomplete\nfeedback: No automated tests found in the git diff."
        )

        with (
            patch("forge.workflow.nodes.task_takeover_review.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_review.GitOperations") as mock_git,
            patch("forge.workflow.nodes.task_takeover_review.ContainerRunner", return_value=mock_runner),
        ):
            mock_git_instance = MagicMock()
            mock_git_instance._run_git = MagicMock()
            mock_git_instance._run_git.return_value.returncode = 0
            mock_git_instance._run_git.return_value.stdout = "diff contents"
            mock_git_instance.has_uncommitted_changes = MagicMock(return_value=False)
            mock_git.return_value = mock_git_instance

            result = await run_qualitative_review(base_task_state)

        assert result["review_verdict"] == "tests_incomplete"
        assert "No automated tests found" in result["review_feedback"]
        assert result["qualitative_review_retry_count"] == 1
        assert result["qualitative_review_failed"] is True
        assert result["current_node"] == "qualitative_review"
        assert result["last_error"] is None
        mock_jira.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_qualitative_review_retry_increment(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify that existing retry counts are incremented correctly on failure."""
        base_task_state["qualitative_review_retry_count"] = 1

        mock_jira = _make_mock_jira()
        mock_runner = _make_mock_runner(
            "verdict: tests_incomplete\nfeedback: Still lacking necessary test coverage."
        )

        with (
            patch("forge.workflow.nodes.task_takeover_review.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_review.GitOperations") as mock_git,
            patch("forge.workflow.nodes.task_takeover_review.ContainerRunner", return_value=mock_runner),
        ):
            mock_git_instance = MagicMock()
            mock_git_instance._run_git = MagicMock()
            mock_git_instance._run_git.return_value.returncode = 0
            mock_git_instance._run_git.return_value.stdout = "diff contents"
            mock_git_instance.has_uncommitted_changes = MagicMock(return_value=False)
            mock_git.return_value = mock_git_instance

            result = await run_qualitative_review(base_task_state)

        assert result["qualitative_review_retry_count"] == 2
        assert result["qualitative_review_failed"] is True

    @pytest.mark.asyncio
    async def test_run_qualitative_review_valid_diff(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify qualitative review behavior when dealing with a valid git diff structure.

        Valid structure has requirements met and automated tests added.
        """
        mock_jira = _make_mock_jira(
            description="Acceptance Criteria:\n1. Must implement user authentication.\n2. Must add tests."
        )
        # Mocking LLM confirming that the diff met all requirements and added tests
        mock_runner = _make_mock_runner(
            "verdict: adequate\nfeedback: Perfect, all requirements met and tests are written."
        )

        valid_diff = """diff --git a/src/auth.py b/src/auth.py
new file mode 100644
--- /dev/null
+++ b/src/auth.py
@@ -0,0 +1,5 @@
+def login():
+    return True
diff --git a/tests/test_auth.py b/tests/test_auth.py
new file mode 100644
--- /dev/null
+++ b/tests/test_auth.py
@@ -0,0 +1,4 @@
+from src.auth import login
+def test_login():
+    assert login() is True
"""

        with (
            patch("forge.workflow.nodes.task_takeover_review.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_review.GitOperations") as mock_git,
            patch("forge.workflow.nodes.task_takeover_review.ContainerRunner", return_value=mock_runner),
        ):
            mock_git_instance = MagicMock()
            mock_git_instance._run_git = MagicMock()
            mock_git_instance._run_git.return_value.returncode = 0
            mock_git_instance._run_git.return_value.stdout = valid_diff
            mock_git_instance.has_uncommitted_changes = MagicMock(return_value=False)
            mock_git.return_value = mock_git_instance

            result = await run_qualitative_review(base_task_state)

        assert result["review_verdict"] == "adequate"
        assert result["qualitative_review_failed"] is False

    @pytest.mark.asyncio
    async def test_run_qualitative_review_invalid_diff_missing_tests(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify qualitative review behavior when dealing with an invalid git diff structure lacking tests."""
        mock_jira = _make_mock_jira(
            description="Acceptance Criteria:\n1. Must implement user authentication.\n2. Must add tests."
        )
        # Mocking LLM indicating that no automated test is found
        mock_runner = _make_mock_runner(
            "verdict: tests_incomplete\nfeedback: No automated test was found in the git diff."
        )

        invalid_diff = """diff --git a/src/auth.py b/src/auth.py
new file mode 100644
--- /dev/null
+++ b/src/auth.py
@@ -0,0 +1,5 @@
+def login():
+    return True
"""

        with (
            patch("forge.workflow.nodes.task_takeover_review.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_review.GitOperations") as mock_git,
            patch("forge.workflow.nodes.task_takeover_review.ContainerRunner", return_value=mock_runner),
        ):
            mock_git_instance = MagicMock()
            mock_git_instance._run_git = MagicMock()
            mock_git_instance._run_git.return_value.returncode = 0
            mock_git_instance._run_git.return_value.stdout = invalid_diff
            mock_git_instance.has_uncommitted_changes = MagicMock(return_value=False)
            mock_git.return_value = mock_git_instance

            result = await run_qualitative_review(base_task_state)

        assert result["review_verdict"] == "tests_incomplete"
        assert result["qualitative_review_failed"] is True

    @pytest.mark.asyncio
    async def test_run_qualitative_review_invalid_diff_unmet_criteria(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify qualitative review behavior when dealing with an invalid git diff structure that fails requirements."""
        mock_jira = _make_mock_jira(
            description="Acceptance Criteria:\n1. Must implement user authentication.\n2. Must add tests."
        )
        # Mocking LLM indicating that the implementation is incomplete or buggy
        mock_runner = _make_mock_runner(
            "verdict: tests_incomplete\nfeedback: The user authentication logic is missing password hashing requirement."
        )

        invalid_diff = """diff --git a/src/auth.py b/src/auth.py
new file mode 100644
--- /dev/null
+++ b/src/auth.py
@@ -0,0 +1,4 @@
+def login():
+    # Missing password hashing or actual implementation
+    return True
"""

        with (
            patch("forge.workflow.nodes.task_takeover_review.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_review.GitOperations") as mock_git,
            patch("forge.workflow.nodes.task_takeover_review.ContainerRunner", return_value=mock_runner),
        ):
            mock_git_instance = MagicMock()
            mock_git_instance._run_git = MagicMock()
            mock_git_instance._run_git.return_value.returncode = 0
            mock_git_instance._run_git.return_value.stdout = invalid_diff
            mock_git_instance.has_uncommitted_changes = MagicMock(return_value=False)
            mock_git.return_value = mock_git_instance

            result = await run_qualitative_review(base_task_state)

        assert result["review_verdict"] == "tests_incomplete"
        assert result["qualitative_review_failed"] is True

    @pytest.mark.asyncio
    async def test_run_qualitative_review_missing_workspace(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify error state is set when the workspace path is missing."""
        base_task_state["workspace_path"] = None

        result = await run_qualitative_review(base_task_state)
        assert result["last_error"] == "Workspace not set up"
        assert result["current_node"] == "qualitative_review"

    @pytest.mark.asyncio
    async def test_run_qualitative_review_exception_handling(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify robust error recovery when exceptions are raised."""
        mock_jira = _make_mock_jira()
        mock_jira.get_issue = AsyncMock(side_effect=RuntimeError("Jira API timeout"))

        with patch(
            "forge.workflow.nodes.task_takeover_review.JiraClient", return_value=mock_jira
        ), patch(
            "forge.workflow.nodes.task_takeover_review.prepare_workspace",
            return_value=("/tmp/fake-workspace-review", MagicMock()),
        ):
            result = await run_qualitative_review(base_task_state)

        assert result["last_error"] is not None
        assert "Jira API timeout" in result["last_error"]
        assert result["current_node"] == "qualitative_review"
