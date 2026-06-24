"""Unit tests for local review fix pass status comment posting.

These tests verify that the local_review_changes node correctly posts fix pass
status comments on subsequent review iterations (pass_number > 1) with the
correct pass number in the message.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.local_reviewer import local_review_changes


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    return mock


def create_mock_container_runner(has_unfixed_issues=False):
    """Create a mock ContainerRunner."""
    mock = MagicMock()

    # Mock result
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.error_message = None

    # Set stdout/stderr to indicate unfixed issues if requested
    if has_unfixed_issues:
        mock_result.stdout = "unfixed breaking issues remain"
        mock_result.stderr = ""
    else:
        mock_result.stdout = "all issues fixed"
        mock_result.stderr = ""

    mock.run = AsyncMock(return_value=mock_result)
    return mock


def create_mock_git_operations(has_changes=False):
    """Create a mock GitOperations."""
    mock = MagicMock()
    mock.has_uncommitted_changes = MagicMock(return_value=has_changes)
    mock.stage_all = MagicMock()
    mock.commit = MagicMock()
    return mock


class TestLocalReviewFixPassComment:
    """Test cases for fix pass status comment on subsequent passes."""

    @pytest.mark.asyncio
    async def test_posts_fix_pass_comment_on_second_pass(self):
        """Should post fix pass comment with pass number 2."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 2
        state["local_review_attempts"] = 1

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            await local_review_changes(state)

        # Verify post_status_comment was called with correct parameters
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-123",
            "🔧 Local review found issues, applying fixes (pass 2).",
        )

        # Verify JiraClient was properly closed
        mock_jira.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_posts_fix_pass_comment_on_third_pass(self):
        """Should post fix pass comment with pass number 3."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-456",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 3
        state["local_review_attempts"] = 2

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            await local_review_changes(state)

        # Verify post_status_comment was called with pass number 3
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-456",
            "🔧 Local review found issues, applying fixes (pass 3).",
        )

    @pytest.mark.asyncio
    async def test_posts_fix_pass_comment_on_fifth_pass(self):
        """Should post fix pass comment with pass number 5+."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-789",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 5
        state["local_review_attempts"] = 4

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            await local_review_changes(state)

        # Verify post_status_comment was called with pass number 5
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-789",
            "🔧 Local review found issues, applying fixes (pass 5).",
        )

    @pytest.mark.asyncio
    async def test_no_fix_pass_comment_on_first_pass(self):
        """Should NOT post fix pass comment when pass_number == 1."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 1
        state["local_review_attempts"] = 0

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            await local_review_changes(state)

        # On first pass, initial comment is posted, but not fix pass comment
        # Verify the fix pass comment text is NOT in any call
        for call in mock_post_status.call_args_list:
            assert "🔧" not in str(call), "Fix pass comment should not be posted on first pass"


class TestLocalReviewFixPassCommentCallOrdering:
    """Test that fix pass comment is posted at the correct time."""

    @pytest.mark.asyncio
    async def test_fix_pass_comment_posted_before_container_execution(self):
        """Should post fix pass comment before container execution starts."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 2
        state["local_review_attempts"] = 1

        call_order = []

        async def track_post_status_comment(*args, **kwargs):
            call_order.append("post_status_comment")

        async def track_container_run(*args, **kwargs):
            call_order.append("container_run")
            return create_mock_container_runner(has_unfixed_issues=False).run.return_value

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner") as mock_runner_cls,
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment",
                side_effect=track_post_status_comment,
            ),
        ):
            mock_runner_instance = MagicMock()
            mock_runner_instance.run = AsyncMock(side_effect=track_container_run)
            mock_runner_cls.return_value = mock_runner_instance

            await local_review_changes(state)

        # Verify fix pass comment was posted before container execution
        assert call_order == ["post_status_comment", "container_run"]

    @pytest.mark.asyncio
    async def test_fix_pass_comment_posted_after_workspace_check(self):
        """Should skip fix pass comment if no workspace path (early return)."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = None  # No workspace
        state["local_review_pass_number"] = 2

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            result = await local_review_changes(state)

        # Verify no comment posted when workspace_path is None
        mock_post_status.assert_not_called()

        # Verify workflow skipped to create_pr
        assert result["current_node"] == "create_pr"

    @pytest.mark.asyncio
    async def test_fix_pass_comment_posted_before_max_attempts_check(self):
        """Should post fix pass comment before checking max attempts limit."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 2
        state["local_review_attempts"] = 2  # At max attempts

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            result = await local_review_changes(state)

        # Verify comment was posted even though max attempts reached
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-123",
            "🔧 Local review found issues, applying fixes (pass 2).",
        )

        # Verify workflow proceeded to create_pr due to max attempts
        assert result["current_node"] == "create_pr"


class TestLocalReviewFixPassCommentEdgeCases:
    """Test edge cases for fix pass comment posting."""

    @pytest.mark.asyncio
    async def test_fix_pass_comment_uses_correct_ticket_key(self):
        """Should use correct ticket_key from state for fix pass comment."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="CUSTOM-999",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 4
        state["local_review_attempts"] = 3

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            await local_review_changes(state)

        # Verify correct ticket_key used
        mock_post_status.assert_called_once_with(
            mock_jira,
            "CUSTOM-999",
            "🔧 Local review found issues, applying fixes (pass 4).",
        )

    @pytest.mark.asyncio
    async def test_fix_pass_comment_increments_correctly_across_retries(self):
        """Should use incrementing pass numbers across multiple review iterations."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=True)
        mock_git = create_mock_git_operations(has_changes=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 2
        state["local_review_attempts"] = 0  # First attempt at this pass

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            result = await local_review_changes(state)

        # Verify comment posted with pass 2
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-123",
            "🔧 Local review found issues, applying fixes (pass 2).",
        )

        # Verify state incremented pass number for retry
        assert result["local_review_pass_number"] == 3
        assert result["current_node"] == "local_review"  # Retrying
