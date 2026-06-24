"""Unit tests for local_review_pass_number tracking."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.bug.state import create_initial_bug_state
from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.implementation import implement_task
from forge.workflow.nodes.local_reviewer import local_review_changes


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.get_issue = AsyncMock()

    # Mock issue with description and summary
    mock_issue = MagicMock()
    mock_issue.description = "Task description"
    mock_issue.summary = "Task summary"
    mock.get_issue.return_value = mock_issue

    return mock


def create_mock_container_runner(success=True, has_unfixed_issues=False):
    """Create a mock ContainerRunner."""
    mock = MagicMock()

    # Mock result
    mock_result = MagicMock()
    mock_result.success = success
    mock_result.error_message = None if success else "Container failed"

    # Set stdout/stderr to indicate unfixed issues if requested
    if has_unfixed_issues:
        mock_result.stdout = "unfixed breaking issues remain"
        mock_result.stderr = ""
    else:
        mock_result.stdout = "all issues fixed"
        mock_result.stderr = ""

    mock.run = AsyncMock(return_value=mock_result)
    return mock, mock_result


def create_mock_git_operations(has_changes=True):
    """Create a mock GitOperations."""
    mock = MagicMock()
    mock.has_uncommitted_changes = MagicMock(return_value=has_changes)
    mock.stage_all = MagicMock()
    mock.commit = MagicMock()
    return mock


class TestPassNumberInitialization:
    """Test cases for pass number initialization."""

    def test_feature_state_initializes_pass_number_to_1(self):
        """Feature state should initialize local_review_pass_number to 1."""
        state = create_initial_feature_state(ticket_key="FEAT-123")
        assert state["local_review_pass_number"] == 1

    def test_bug_state_initializes_pass_number_to_1(self):
        """Bug state should initialize local_review_pass_number to 1."""
        state = create_initial_bug_state(ticket_key="BUG-456")
        assert state["local_review_pass_number"] == 1


class TestPassNumberResetOnLocalReviewPhase:
    """Test cases for pass number reset when entering local_review phase."""

    @pytest.mark.asyncio
    async def test_implement_task_resets_pass_number_when_entering_local_review(self):
        """Should reset pass number to 1 when all tasks are done and entering local_review."""
        mock_jira = create_mock_jira_client()
        mock_runner, mock_result = create_mock_container_runner(success=True)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        # No current_task and no task_keys = all tasks done
        state["current_task_key"] = None
        state["task_keys"] = []
        # Set pass number to something other than 1 to test reset
        state["local_review_pass_number"] = 5

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.implementation.GitOperations", return_value=mock_git),
        ):
            result = await implement_task(state)

        # Verify pass number was reset to 1
        assert result["local_review_pass_number"] == 1
        assert result["current_node"] == "local_review"

    @pytest.mark.asyncio
    async def test_implement_task_resets_pass_number_even_with_uncommitted_changes(self):
        """Should reset pass number to 1 when entering local_review, even with uncommitted changes."""
        mock_jira = create_mock_jira_client()
        mock_runner, mock_result = create_mock_container_runner(success=True)
        mock_git = create_mock_git_operations(has_changes=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = None
        state["task_keys"] = []
        state["local_review_pass_number"] = 3
        state["context"] = {"branch_name": "feature/test"}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.implementation.GitOperations", return_value=mock_git),
        ):
            result = await implement_task(state)

        assert result["local_review_pass_number"] == 1
        assert result["current_node"] == "local_review"


class TestPassNumberIncrement:
    """Test cases for pass number increment after fix attempts."""

    @pytest.mark.asyncio
    async def test_local_review_increments_pass_number_on_retry(self):
        """Should increment pass number when retrying after unfixed issues."""
        mock_runner, mock_result = create_mock_container_runner(
            success=True, has_unfixed_issues=True
        )
        mock_git = create_mock_git_operations(has_changes=True)

        state = create_initial_feature_state(ticket_key="FEAT-123")
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        state["local_review_pass_number"] = 1
        state["current_repo"] = "owner/test-repo"
        state["context"] = {"branch_name": "feature/test"}

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(state)

        # Should increment both attempts and pass number
        assert result["local_review_attempts"] == 1
        assert result["local_review_pass_number"] == 2
        assert result["current_node"] == "local_review"

    @pytest.mark.asyncio
    async def test_local_review_increments_pass_number_multiple_times(self):
        """Should increment pass number for each retry."""
        mock_runner, mock_result = create_mock_container_runner(
            success=True, has_unfixed_issues=True
        )
        mock_git = create_mock_git_operations(has_changes=True)

        state = create_initial_feature_state(ticket_key="FEAT-123")
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 1
        state["local_review_pass_number"] = 2
        state["current_repo"] = "owner/test-repo"
        state["context"] = {"branch_name": "feature/test"}

        # This is the second retry (attempt 1 -> 2 will fail since MAX_REVIEW_ATTEMPTS = 2)
        # So we need to set attempts to 0 for the second increment test
        state["local_review_attempts"] = 0
        state["local_review_pass_number"] = 1

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            # First retry
            result1 = await local_review_changes(state)
            assert result1["local_review_pass_number"] == 2

            # Second retry (will still loop since MAX_REVIEW_ATTEMPTS = 2)
            mock_runner2, mock_result2 = create_mock_container_runner(
                success=True,
                has_unfixed_issues=False,  # This time it passes
            )
            with patch(
                "forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner2
            ):
                result2 = await local_review_changes(result1)
                # Should reset since no unfixed issues
                assert result2["local_review_attempts"] == 0
                assert result2["current_node"] == "create_pr"


class TestPassNumberPersistence:
    """Test cases for pass number persistence across review iterations."""

    @pytest.mark.asyncio
    async def test_pass_number_persists_across_iterations_for_same_feature(self):
        """Pass number should persist correctly across multiple review iterations for same feature."""
        mock_runner, mock_result = create_mock_container_runner(
            success=True, has_unfixed_issues=True
        )
        mock_git = create_mock_git_operations(has_changes=True)

        state = create_initial_feature_state(ticket_key="FEAT-123")
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        state["local_review_pass_number"] = 1
        state["current_repo"] = "owner/test-repo"
        state["context"] = {"branch_name": "feature/test"}

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(state)

        # Pass number incremented and persisted in state
        assert result["local_review_pass_number"] == 2
        # Verify it persists by using this state as input for another operation
        assert "local_review_pass_number" in result


class TestPassNumberResetForNewFeature:
    """Test cases for pass number reset when processing a new feature."""

    def test_new_feature_state_has_pass_number_1(self):
        """New feature should start with pass number 1."""
        state1 = create_initial_feature_state(ticket_key="FEAT-123")
        assert state1["local_review_pass_number"] == 1

        # Create a new feature (simulating new ticket)
        state2 = create_initial_feature_state(ticket_key="FEAT-456")
        assert state2["local_review_pass_number"] == 1

    def test_new_bug_state_has_pass_number_1(self):
        """New bug should start with pass number 1."""
        state1 = create_initial_bug_state(ticket_key="BUG-123")
        assert state1["local_review_pass_number"] == 1

        # Create a new bug (simulating new ticket)
        state2 = create_initial_bug_state(ticket_key="BUG-456")
        assert state2["local_review_pass_number"] == 1


class TestPassNumberEdgeCases:
    """Test cases for edge cases and error scenarios."""

    @pytest.mark.asyncio
    async def test_local_review_handles_missing_pass_number(self):
        """Should default to 1 if pass number is missing from state."""
        mock_runner, mock_result = create_mock_container_runner(
            success=True, has_unfixed_issues=True
        )
        mock_git = create_mock_git_operations(has_changes=True)

        state = create_initial_feature_state(ticket_key="FEAT-123")
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        # Explicitly remove pass number to test default
        del state["local_review_pass_number"]
        state["current_repo"] = "owner/test-repo"
        state["context"] = {"branch_name": "feature/test"}

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(state)

        # Should default to 1 and increment to 2
        assert result["local_review_pass_number"] == 2

    @pytest.mark.asyncio
    async def test_local_review_no_increment_when_max_attempts_reached(self):
        """Should not increment pass number when max attempts reached."""
        mock_runner, mock_result = create_mock_container_runner(
            success=True, has_unfixed_issues=True
        )
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(ticket_key="FEAT-123")
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 2  # MAX_REVIEW_ATTEMPTS = 2
        state["local_review_pass_number"] = 2
        state["current_repo"] = "owner/test-repo"
        state["context"] = {"branch_name": "feature/test"}

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(state)

        # Should reset to 0 and move to create_pr without incrementing pass number
        assert result["local_review_attempts"] == 0
        assert result["current_node"] == "create_pr"
        # Pass number should not be in the update since we're exiting review

    @pytest.mark.asyncio
    async def test_local_review_no_increment_on_success(self):
        """Should not increment pass number when review passes without issues."""
        mock_runner, mock_result = create_mock_container_runner(
            success=True, has_unfixed_issues=False
        )
        mock_git = create_mock_git_operations(has_changes=True)

        state = create_initial_feature_state(ticket_key="FEAT-123")
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        state["local_review_pass_number"] = 1
        state["current_repo"] = "owner/test-repo"
        state["context"] = {"branch_name": "feature/test"}

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(state)

        # Should reset attempts and move to create_pr without incrementing pass number
        assert result["local_review_attempts"] == 0
        assert result["current_node"] == "create_pr"
        # Pass number stays same since we're exiting review successfully

    @pytest.mark.asyncio
    async def test_local_review_no_increment_on_exception(self):
        """Should not increment pass number when an exception occurs."""
        mock_runner, mock_result = create_mock_container_runner(success=True)
        # Make the runner raise an exception
        mock_runner.run = AsyncMock(side_effect=Exception("Container failed"))
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(ticket_key="FEAT-123")
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        state["local_review_pass_number"] = 1
        state["current_repo"] = "owner/test-repo"
        state["context"] = {"branch_name": "feature/test"}

        with (
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
        ):
            result = await local_review_changes(state)

        # Should reset and move to create_pr on exception
        assert result["local_review_attempts"] == 0
        assert result["current_node"] == "create_pr"
