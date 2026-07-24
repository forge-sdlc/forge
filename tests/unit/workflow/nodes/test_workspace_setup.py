"""Integration tests for workspace setup node - Jira status updates."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from forge.models.workflow import ForgeLabel
from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.workspace_setup import prepare_workspace, setup_workspace


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.transition_issue = AsyncMock()
    return mock


def create_mock_workspace_manager():
    """Create a mock WorkspaceManager."""
    mock = MagicMock()
    workspace = MagicMock()
    # Mock path as a Path object with necessary methods
    mock_path = MagicMock(spec=Path)
    mock_path.__str__ = MagicMock(return_value="/tmp/test-workspace")
    mock_path.__truediv__ = MagicMock(
        side_effect=lambda _: MagicMock(
            mkdir=MagicMock(),
            exists=MagicMock(return_value=False),
            read_text=MagicMock(return_value=""),
            write_text=MagicMock(),
        )
    )
    workspace.path = mock_path
    workspace.branch_name = "feature/TEST-123"
    mock.create_workspace = MagicMock(return_value=workspace)
    return mock, workspace


def create_mock_git_operations():
    """Create a mock GitOperations."""
    mock = MagicMock()
    mock.clone = MagicMock()
    mock.add_fork_remote = MagicMock()
    mock.remote_branch_exists = MagicMock(return_value=False)
    mock.checkout_branch = MagicMock()
    mock.create_branch = MagicMock()
    mock.load_guardrails = MagicMock(return_value={})
    return mock


def create_mock_guardrails_loader():
    """Create a mock GuardrailsLoader."""
    mock = MagicMock()
    guardrails = MagicMock()
    guardrails.get_system_context = MagicMock(return_value={})
    mock.return_value.load = MagicMock(return_value=guardrails)
    return mock


def create_mock_github_client():
    """Create a mock GitHubClient for fork/repo operations."""
    mock = MagicMock()
    mock.get_repository = AsyncMock(return_value={"default_branch": "main"})
    mock.get_or_create_fork = AsyncMock(
        return_value={"owner": {"login": "fork-owner"}, "name": "test-repo"}
    )
    mock.sync_fork_with_upstream = AsyncMock()
    mock.close = AsyncMock()
    return mock


class TestWorkspaceSetupStatusComment:
    """Test cases for workspace setup posting status comments."""

    @pytest.mark.asyncio
    async def test_workspace_setup_posts_status_comment(self):
        """Should post status comment with correct format."""
        mock_jira = create_mock_jira_client()
        mock_manager, mock_workspace = create_mock_workspace_manager()
        mock_git = create_mock_git_operations()
        mock_guardrails_loader = create_mock_guardrails_loader()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            current_repo="owner/my-repo",
            task_keys=["TASK-1", "TASK-2"],
        )

        with (
            patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.workspace_setup.get_workspace_manager",
                return_value=mock_manager,
            ),
            patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader", mock_guardrails_loader),
            patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github),
        ):
            result = await setup_workspace(state)

        # Verify comment was posted with correct format
        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args
        assert call_args[0][0] == "TEST-123"
        assert "⚙️ Implementation starting for my-repo" in call_args[0][1]
        assert "Setting up workspace..." in call_args[0][1]

        # Verify JiraClient was closed
        mock_jira.close.assert_called_once()

        # Verify workspace was set up
        assert result["workspace_path"] == str(Path("/tmp/test-workspace"))

    @pytest.mark.asyncio
    async def test_workspace_setup_uses_local_git_exclude_for_forge_dir(self, tmp_path):
        """Forge internals should be ignored without modifying tracked .gitignore."""
        workspace_path = tmp_path / "repo"
        (workspace_path / ".git" / "info").mkdir(parents=True)
        (workspace_path / ".gitignore").write_text("*.log\n")
        workspace = SimpleNamespace(
            path=workspace_path,
            branch_name="feature/TEST-123",
        )
        manager = MagicMock()
        manager.create_workspace.return_value = workspace
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()
        mock_guardrails_loader = create_mock_guardrails_loader()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            current_repo="owner/my-repo",
            task_keys=[],
        )

        with (
            patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.workspace_setup.get_workspace_manager",
                return_value=manager,
            ),
            patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader", mock_guardrails_loader),
            patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github),
        ):
            await setup_workspace(state)

        assert (workspace_path / ".forge" / "history").is_dir()
        assert (workspace_path / ".gitignore").read_text() == "*.log\n"
        assert ".forge/" in (workspace_path / ".git" / "info" / "exclude").read_text()

    @pytest.mark.asyncio
    async def test_workspace_setup_handles_missing_repo_name(self):
        """Should use placeholder text when current_repo is None."""
        mock_jira = create_mock_jira_client()
        mock_manager, mock_workspace = create_mock_workspace_manager()
        mock_git = create_mock_git_operations()
        mock_guardrails_loader = create_mock_guardrails_loader()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="TEST-456",
            current_repo=None,
            tasks_by_repo={"owner/repo1": ["TASK-1"]},
        )

        with (
            patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.workspace_setup.get_workspace_manager",
                return_value=mock_manager,
            ),
            patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader", mock_guardrails_loader),
            patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github),
        ):
            await setup_workspace(state)

        # Verify placeholder was used in comment
        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args
        # When current_repo is None, the function picks from tasks_by_repo, so it's "repo1"
        assert "⚙️ Implementation starting for repo1" in call_args[0][1]


class TestWorkspaceSetupLabelAndTransitions:
    """Test cases for workspace setup setting labels and transitioning tasks."""

    @pytest.mark.asyncio
    async def test_workspace_setup_sets_implementing_label(self):
        """Should set forge:implementing label on feature ticket."""
        mock_jira = create_mock_jira_client()
        mock_manager, mock_workspace = create_mock_workspace_manager()
        mock_git = create_mock_git_operations()
        mock_guardrails_loader = create_mock_guardrails_loader()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="TEST-789",
            current_repo="owner/test-repo",
        )

        with (
            patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.workspace_setup.get_workspace_manager",
                return_value=mock_manager,
            ),
            patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader", mock_guardrails_loader),
            patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github),
        ):
            await setup_workspace(state)

        # Verify set_workflow_label was called with TASK_IMPLEMENTING
        mock_jira.set_workflow_label.assert_called_once_with(
            "TEST-789", ForgeLabel.TASK_IMPLEMENTING
        )

    @pytest.mark.asyncio
    async def test_workspace_setup_transitions_tasks(self):
        """Should transition all tasks to In Progress."""
        mock_jira = create_mock_jira_client()
        mock_manager, mock_workspace = create_mock_workspace_manager()
        mock_git = create_mock_git_operations()
        mock_guardrails_loader = create_mock_guardrails_loader()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="TEST-101",
            current_repo="owner/test-repo",
            task_keys=["AISOS-101", "AISOS-102"],
        )

        with (
            patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.workspace_setup.get_workspace_manager",
                return_value=mock_manager,
            ),
            patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader", mock_guardrails_loader),
            patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github),
        ):
            await setup_workspace(state)

        # Verify transition_issue was called twice with "In Progress"
        assert mock_jira.transition_issue.call_count == 2
        mock_jira.transition_issue.assert_any_call("AISOS-101", "In Progress")
        mock_jira.transition_issue.assert_any_call("AISOS-102", "In Progress")


class TestWorkspaceSetupErrorHandling:
    """Test cases for workspace setup error handling."""

    @pytest.mark.asyncio
    async def test_workspace_setup_continues_on_jira_failure(self, caplog):
        """Should continue workspace setup even if Jira operations fail."""
        mock_jira = create_mock_jira_client()
        # Mock add_comment to raise an HTTP error
        mock_jira.add_comment = AsyncMock(side_effect=httpx.HTTPError("API error"))
        mock_manager, mock_workspace = create_mock_workspace_manager()
        mock_git = create_mock_git_operations()
        mock_guardrails_loader = create_mock_guardrails_loader()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="TEST-999",
            current_repo="owner/test-repo",
        )

        with (
            patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.workspace_setup.get_workspace_manager",
                return_value=mock_manager,
            ),
            patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader", mock_guardrails_loader),
            patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github),
        ):
            # Should not raise an exception
            result = await setup_workspace(state)

        # Verify error was logged (from jira_status utilities)
        assert any(
            "Failed to post status comment to TEST-999" in record.message
            and record.levelname == "WARNING"
            for record in caplog.records
        )

        # Verify workspace setup continued successfully
        assert result["workspace_path"] == str(Path("/tmp/test-workspace"))
        mock_jira.close.assert_called_once()


class TestWorkspaceSetupForkCreation:
    """Test that setup_workspace ensures fork when fork_owner/fork_repo not in state."""

    @pytest.mark.asyncio
    async def test_workspace_setup_creates_fork_when_not_in_state(self):
        """Should call get_or_create_fork and persist fork info in state."""
        mock_jira = create_mock_jira_client()
        mock_manager, mock_workspace = create_mock_workspace_manager()
        mock_git = create_mock_git_operations()
        mock_guardrails_loader = create_mock_guardrails_loader()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="TEST-FORK",
            current_repo="upstream-org/my-repo",
        )
        assert not state.get("fork_owner")
        assert not state.get("fork_repo")

        with (
            patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.workspace_setup.get_workspace_manager",
                return_value=mock_manager,
            ),
            patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader", mock_guardrails_loader),
            patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github),
        ):
            result = await setup_workspace(state)

        mock_github.get_or_create_fork.assert_called_once_with(
            "upstream-org", "my-repo"
        )
        mock_github.sync_fork_with_upstream.assert_called_once_with(
            "fork-owner", "test-repo"
        )
        assert result["fork_owner"] == "fork-owner"
        assert result["fork_repo"] == "test-repo"
        mock_git.add_fork_remote.assert_called_once_with("fork-owner", "test-repo")
        mock_github.close.assert_called_once()


class TestPrepareWorkspaceRecovery:
    """Tests for prepare_workspace workspace sync/recreation behavior."""

    def test_sync_failure_recreates_workspace_from_fork(self, tmp_path):
        """A workspace that cannot sync is deleted and cloned fresh from the fork."""
        workspace_path = tmp_path / "forge-TEST-123-org-repo"
        workspace_path.mkdir()
        stale_file = workspace_path / "stale.txt"
        stale_file.write_text("dirty")

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            current_repo="org/repo",
            workspace_path=str(workspace_path),
            fork_owner="forge-bot",
            fork_repo="repo",
            context={"branch_name": "forge/test-123"},
        )

        old_git = MagicMock()
        old_git.pull_rebase.side_effect = RuntimeError("any workspace sync failure")
        new_git = MagicMock()
        settings = MagicMock(workspace_base_dir=str(tmp_path))

        with (
            patch("forge.workflow.nodes.workspace_setup.get_settings", return_value=settings),
            patch(
                "forge.workflow.nodes.workspace_setup.GitOperations",
                side_effect=[old_git, new_git],
            ),
        ):
            result_path, result_git = prepare_workspace(state)

        assert result_path == str(workspace_path)
        assert result_git is new_git
        assert not stale_file.exists()
        old_git.pull_rebase.assert_called_once_with(remote="fork")
        new_git.clone.assert_called_once()
        new_git.add_fork_remote.assert_called_once_with("forge-bot", "repo")
        new_git.checkout_branch.assert_called_once_with("forge/test-123", remote="fork")
