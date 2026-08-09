"""Unit tests for informational PR command comments on PR creation."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.pr_creation import create_pull_request
from forge.integrations.github.client import PullRequestCreationResult


def create_mock_github_client(
    pr_number=123, pr_url="https://github.com/owner/repo/pull/123", is_new_pr=True
):
    """Create a mock GitHubClient with configurable PR data."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.get_or_create_fork = AsyncMock(
        return_value={
            "owner": {"login": "fork-owner"},
            "name": "repo",
        }
    )
    mock.sync_fork_with_upstream = AsyncMock()
    mock.create_issue_comment = AsyncMock()

    # PR creation response - can be configured for different scenarios
    pr_data = {
        "html_url": pr_url,
    }
    if pr_number is not None:
        pr_data["number"] = pr_number

    mock.create_pull_request = AsyncMock(
        return_value=PullRequestCreationResult(pr=pr_data, created=is_new_pr)
    )
    return mock


def create_mock_jira_client():
    """Create a mock JiraClient."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.create_remote_link = AsyncMock()
    mock.get_issue = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.is_repo_draft = AsyncMock(return_value=False)

    # Mock issue with summary
    mock_issue = MagicMock()
    mock_issue.summary = "Test feature"
    mock.get_issue.return_value = mock_issue

    return mock


def create_mock_git_operations():
    """Create a mock GitOperations."""
    mock = MagicMock()
    mock.add_fork_remote = MagicMock()
    mock.push_to_fork = MagicMock()

    # Mock git log for PR body generation
    mock_result = MagicMock()
    mock_result.stdout = "abc123 Test commit\n\nTest commit body"
    mock._run_git = MagicMock(return_value=mock_result)

    return mock


def create_mock_workspace():
    """Create a mock Workspace."""
    mock = MagicMock()
    mock.path = Path("/tmp/test-workspace")
    return mock


@pytest.fixture(autouse=True)
def mock_external_pr_creation_side_effects():
    """Keep PR creation tests from reaching agent or Redis-backed helpers."""
    with (
        patch(
            "forge.workflow.nodes.pr_creation._generate_pr_body_with_agent",
            new_callable=AsyncMock,
            return_value="Generated PR body",
        ),
        patch("forge.workflow.nodes.pr_creation.set_pr_ticket_index", new_callable=AsyncMock),
    ):
        yield


class TestPRInformationalComment:
    """Test cases for the informational PR command comment on PR creation."""

    @pytest.mark.asyncio
    async def test_posts_comment_on_new_pr(self):
        """Should post an informational comment when a new PR is created."""
        mock_github = create_mock_github_client(
            pr_number=456, pr_url="https://github.com/owner/repo/pull/456", is_new_pr=True
        )
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test-branch"}

        with (
            patch("forge.workflow.nodes.pr_creation.GitHubClient", return_value=mock_github),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            await create_pull_request(state)

        # Verify comment was posted
        mock_github.create_issue_comment.assert_called_once()
        call_args = mock_github.create_issue_comment.call_args
        assert call_args[1]["owner"] == "owner"
        assert call_args[1]["repo"] == "repo"
        assert call_args[1]["issue_number"] == 456
        assert "/forge rebase" in call_args[1]["body"]
        assert "/forge skip-gate" in call_args[1]["body"]
        assert "/forge unskip-gate" in call_args[1]["body"]

    @pytest.mark.asyncio
    async def test_does_not_post_comment_on_existing_pr(self):
        """Should NOT post an informational comment when an existing PR is returned."""
        mock_github = create_mock_github_client(
            pr_number=456, pr_url="https://github.com/owner/repo/pull/456", is_new_pr=False
        )
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test-branch"}

        with (
            patch("forge.workflow.nodes.pr_creation.GitHubClient", return_value=mock_github),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            await create_pull_request(state)

        # Verify comment was NOT posted
        mock_github.create_issue_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_failure_ignored(self):
        """Should successfully finish PR creation even if posting comment fails."""
        mock_github = create_mock_github_client(
            pr_number=456, pr_url="https://github.com/owner/repo/pull/456", is_new_pr=True
        )
        # Make the comment creation raise an exception
        mock_github.create_issue_comment.side_effect = Exception("GitHub API Error")
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test-branch"}

        with (
            patch("forge.workflow.nodes.pr_creation.GitHubClient", return_value=mock_github),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            result = await create_pull_request(state)

        # Ensure node completed and returned correct state
        assert result["current_pr_number"] == 456
        assert result["current_pr_url"] == "https://github.com/owner/repo/pull/456"
        assert result["current_node"] == "teardown_workspace"
