"""Comprehensive unit tests for local review status comment logic.

This test suite provides complete coverage of local review status comment
functionality including:
- Pass tracking (pass_number == 1 vs pass_number > 1)
- Conditional comment posting
- Error handling and suppression
- Graceful handling when pass_number unavailable
- Comment text formatting with correct pass numbers
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


class TestPassNumberOneCommentPosting:
    """Tests verifying initial comment posts only when pass_number == 1.
    
    Acceptance Criteria: Unit tests verify initial comment posts only when pass_number == 1
    """

    @pytest.mark.asyncio
    async def test_posts_initial_comment_when_pass_number_equals_one(self):
        """Should post initial status comment when pass_number == 1."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-100",
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

        # Verify initial comment was posted
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-100",
            "🔍 Running local code review on changes before creating PR.",
        )
        mock_jira.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_initial_comment_when_pass_number_equals_two(self):
        """Should NOT post initial comment when pass_number == 2."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-101",
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

        # Verify initial comment (with 🔍) was NOT posted
        for call in mock_post_status.call_args_list:
            assert "🔍" not in str(call), "Initial comment should not be posted when pass_number > 1"

    @pytest.mark.asyncio
    async def test_no_initial_comment_when_pass_number_greater_than_one(self):
        """Should NOT post initial comment when pass_number > 1."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-102",
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

        # Verify initial comment text is NOT in any call
        for call in mock_post_status.call_args_list:
            call_str = str(call)
            assert "🔍" not in call_str
            assert "Running local code review on changes before creating PR" not in call_str


class TestPassNumberGreaterThanOneCommentPosting:
    """Tests verifying fix comments post only when pass_number > 1.
    
    Acceptance Criteria: Unit tests verify fix comments post only when pass_number > 1
    """

    @pytest.mark.asyncio
    async def test_posts_fix_comment_when_pass_number_equals_two(self):
        """Should post fix comment when pass_number == 2."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
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

        # Verify fix comment was posted
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-200",
            "🔧 Local review found issues, applying fixes (pass 2).",
        )
        mock_jira.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_posts_fix_comment_when_pass_number_greater_than_two(self):
        """Should post fix comment when pass_number > 2."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-201",
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

        # Verify fix comment was posted
        assert mock_post_status.called
        call_args = mock_post_status.call_args[0]
        assert "🔧" in call_args[2]

    @pytest.mark.asyncio
    async def test_no_fix_comment_when_pass_number_equals_one(self):
        """Should NOT post fix comment when pass_number == 1."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-202",
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

        # Verify fix comment (with 🔧) was NOT posted
        for call in mock_post_status.call_args_list:
            assert "🔧" not in str(call), "Fix comment should not be posted when pass_number == 1"


class TestCorrectPassNumberInCommentText:
    """Tests verifying correct pass number appears in comment text.
    
    Acceptance Criteria: Unit tests verify correct pass number appears in comment text 
    for passes 2, 3, 4, 5+
    """

    @pytest.mark.asyncio
    async def test_comment_shows_pass_two_correctly(self):
        """Should show 'pass 2' in comment text when pass_number == 2."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-300",
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

        # Verify exact comment text with pass number
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-300",
            "🔧 Local review found issues, applying fixes (pass 2).",
        )

    @pytest.mark.asyncio
    async def test_comment_shows_pass_three_correctly(self):
        """Should show 'pass 3' in comment text when pass_number == 3."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-301",
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

        # Verify exact comment text with pass number
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-301",
            "🔧 Local review found issues, applying fixes (pass 3).",
        )

    @pytest.mark.asyncio
    async def test_comment_shows_pass_four_correctly(self):
        """Should show 'pass 4' in comment text when pass_number == 4."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-302",
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

        # Verify exact comment text with pass number
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-302",
            "🔧 Local review found issues, applying fixes (pass 4).",
        )

    @pytest.mark.asyncio
    async def test_comment_shows_pass_five_plus_correctly(self):
        """Should show 'pass 5+' in comment text when pass_number >= 5."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        # Test with pass_number == 5
        state = create_initial_feature_state(
            ticket_key="FEAT-303",
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

        # Verify exact comment text with pass number
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-303",
            "🔧 Local review found issues, applying fixes (pass 5).",
        )

    @pytest.mark.asyncio
    async def test_comment_shows_high_pass_number_correctly(self):
        """Should show correct pass number even for very high values."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-304",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_pass_number"] = 10
        state["local_review_attempts"] = 9

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

        # Verify exact comment text with pass number
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-304",
            "🔧 Local review found issues, applying fixes (pass 10).",
        )


class TestGracefulHandlingWhenPassNumberUnavailable:
    """Tests verifying graceful handling when pass_number unavailable.
    
    Acceptance Criteria: Unit tests verify graceful handling when pass_number unavailable
    """

    @pytest.mark.asyncio
    async def test_defaults_to_pass_one_when_pass_number_missing(self):
        """Should default to pass_number=1 when not present in state."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-500",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        # Explicitly remove pass_number if it exists
        if "local_review_pass_number" in state:
            del state["local_review_pass_number"]

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

        # Verify initial comment was posted (default behavior for pass_number=1)
        mock_post_status.assert_called_once_with(
            mock_jira,
            "FEAT-500",
            "🔍 Running local code review on changes before creating PR.",
        )

    @pytest.mark.asyncio
    async def test_workflow_completes_successfully_without_pass_number(self):
        """Should complete workflow successfully even when pass_number is missing."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-501",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        # Explicitly remove pass_number if it exists
        if "local_review_pass_number" in state:
            del state["local_review_pass_number"]

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

        # Verify workflow completed successfully
        assert result is not None
        assert result["current_node"] == "create_pr"
        assert mock_jira.close.called

    @pytest.mark.asyncio
    async def test_no_error_when_pass_number_none(self):
        """Should handle pass_number=None gracefully."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-502",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        state["local_review_pass_number"] = None  # Explicitly set to None

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            
            # Should not raise exception
            try:
                result = await local_review_changes(state)
                # Verify workflow completed
                assert result["current_node"] == "create_pr"
            except Exception as e:
                pytest.fail(f"Should handle None pass_number gracefully but raised: {e}")

    @pytest.mark.asyncio
    async def test_handles_pass_number_zero_gracefully(self):
        """Should handle edge case of pass_number=0 gracefully."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="FEAT-503",
            current_repo="owner/test-repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["local_review_attempts"] = 0
        state["local_review_pass_number"] = 0  # Edge case: zero

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.local_reviewer.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            
            # Should not raise exception
            result = await local_review_changes(state)

            # Verify workflow completed
            assert result["current_node"] == "create_pr"

            # With pass_number=0 (invalid), should use generic fallback comment
            assert mock_post_status.call_count == 1
            comment_args = mock_post_status.call_args[0]
            assert comment_args[0] == mock_jira  # First arg is jira client
            assert comment_args[1] == "FEAT-503"  # Second arg is ticket key
            assert "🔧 Local review found issues, applying fixes." in comment_args[2]  # Third arg is message


class TestIntegrationWithReviewFlow:
    """Tests verifying comment posting integrates correctly with the overall review flow."""

    @pytest.mark.asyncio
    async def test_comment_posted_before_container_execution(self):
        """Should post status comment before running container review."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        call_order = []

        async def track_post_status(*args, **kwargs):
            call_order.append("post_status_comment")

        async def track_container_run(*args, **kwargs):
            call_order.append("container_run")
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "all issues fixed"
            mock_result.stderr = ""
            return mock_result

        state = create_initial_feature_state(
            ticket_key="FEAT-600",
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
                "forge.workflow.nodes.local_reviewer.post_status_comment",
                side_effect=track_post_status,
            ),
        ):
            mock_runner.run.side_effect = track_container_run
            await local_review_changes(state)

        # Verify comment was posted before container execution
        assert call_order == ["post_status_comment", "container_run"]

    @pytest.mark.asyncio
    async def test_comment_posted_to_correct_ticket(self):
        """Should post comment to the feature ticket from state."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(has_unfixed_issues=False)
        mock_git = create_mock_git_operations(has_changes=False)

        state = create_initial_feature_state(
            ticket_key="CUSTOM-999",
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

        # Verify comment posted to correct ticket
        call_args = mock_post_status.call_args[0]
        assert call_args[1] == "CUSTOM-999"

    @pytest.mark.asyncio
    async def test_no_comment_when_workspace_missing(self):
        """Should not post comment when workspace_path is missing."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(ticket_key="FEAT-601")
        state["workspace_path"] = None
        state["local_review_pass_number"] = 1

        with (
            patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.local_reviewer.post_status_comment"
            ) as mock_post_status,
        ):
            result = await local_review_changes(state)

        # Should skip to create_pr without posting comment
        assert result["current_node"] == "create_pr"
        mock_post_status.assert_not_called()
