"""restore_forge_artifacts is called at workspace setup and recreation."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.nodes.workspace_setup import prepare_workspace, setup_workspace


@pytest.fixture
def base_state(tmp_path):
    return {
        "ticket_key": "TEST-1",
        "current_repo": "org/repo",
        "tasks_by_repo": {"org/repo": ["TEST-2"]},
        "context": {},
        "fork_owner": "",
        "fork_repo": "",
        "retry_count": 0,
        "is_paused": False,
        "pr_merged": False,
        "forge_artifacts": {"org/repo": {"handoff.md": "prior handoff"}},
    }


@pytest.mark.asyncio
async def test_setup_workspace_restores_artifacts(base_state, tmp_path):
    mock_workspace = MagicMock()
    mock_workspace.path = tmp_path
    mock_workspace.branch_name = "forge/test-1"

    mock_manager = MagicMock()
    mock_manager.create_workspace.return_value = mock_workspace

    mock_git = MagicMock()
    mock_git.remote_branch_exists.return_value = False

    mock_guardrails = MagicMock()
    mock_guardrails.get_system_context.return_value = ""

    mock_jira = MagicMock()
    mock_jira.close = AsyncMock()
    mock_jira.add_comment = AsyncMock()
    mock_jira.set_workflow_label = AsyncMock()
    mock_jira.transition_issue = AsyncMock()

    mock_github = MagicMock()
    mock_github.get_repository = AsyncMock(return_value={"default_branch": "main"})
    mock_github.get_or_create_fork = AsyncMock(
        return_value={"owner": {"login": "fork-owner"}, "name": "repo"}
    )
    mock_github.sync_fork_with_upstream = AsyncMock(return_value=True)
    mock_github.close = AsyncMock()

    with patch("forge.workflow.nodes.workspace_setup.get_workspace_manager",
               return_value=mock_manager), \
         patch("forge.workflow.nodes.workspace_setup.JiraClient", return_value=mock_jira), \
         patch("forge.workflow.nodes.workspace_setup.GitHubClient", return_value=mock_github), \
         patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git), \
         patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader",
               return_value=MagicMock(load=MagicMock(return_value=mock_guardrails))), \
         patch("forge.workflow.nodes.workspace_setup.restore_forge_artifacts") as mock_restore:
        await setup_workspace(base_state)

    mock_restore.assert_called_once_with(tmp_path, "org/repo", base_state)


def test_prepare_workspace_restores_artifacts_on_recreation(tmp_path):
    """When workspace is missing and recreated, artifacts are restored."""
    state = {
        "ticket_key": "TEST-1",
        "current_repo": "org/repo",
        "context": {"branch_name": "forge/test-1"},
        "fork_owner": "fork-org",
        "fork_repo": "repo",
        "workspace_path": "",  # missing — triggers recreation
        "forge_artifacts": {"org/repo": {"handoff.md": "prior handoff"}},
    }

    mock_workspace = MagicMock()
    mock_workspace.path = tmp_path
    mock_workspace.branch_name = "forge/test-1"

    mock_manager = MagicMock()
    mock_manager.create_workspace.return_value = mock_workspace

    mock_git = MagicMock()

    with patch("forge.workflow.nodes.workspace_setup.WorkspaceManager",
               return_value=mock_manager), \
         patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git), \
         patch("forge.workflow.nodes.workspace_setup.restore_forge_artifacts") as mock_restore:
        prepare_workspace(state)

    mock_restore.assert_called_once_with(tmp_path, "org/repo", state)


def test_prepare_workspace_does_not_restore_when_workspace_exists(tmp_path):
    """When workspace already exists on disk, no restore is needed."""
    existing_ws = tmp_path / "existing"
    existing_ws.mkdir()

    state = {
        "ticket_key": "TEST-1",
        "current_repo": "org/repo",
        "context": {"branch_name": "forge/test-1"},
        "fork_owner": "fork-org",
        "fork_repo": "repo",
        "workspace_path": str(existing_ws),
        "forge_artifacts": {"org/repo": {"handoff.md": "prior handoff"}},
    }

    mock_workspace = MagicMock()
    mock_workspace.path = existing_ws
    mock_workspace.branch_name = "forge/test-1"

    mock_git = MagicMock()

    with patch("forge.workflow.nodes.workspace_setup.GitOperations", return_value=mock_git), \
         patch("forge.workflow.nodes.workspace_setup.restore_forge_artifacts") as mock_restore:
        prepare_workspace(state)

    mock_restore.assert_not_called()
