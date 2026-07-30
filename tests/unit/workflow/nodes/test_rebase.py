"""Tests for the pull-request rebase node."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.nodes.rebase import rebase_pr


@pytest.mark.asyncio
async def test_rebase_workspace_persists_identity_after_clone(tmp_path: Path) -> None:
    """A restarted worker must be able to safely tear down a rebase workspace."""
    workspace = SimpleNamespace(
        path=tmp_path / "forge-TASK-123-acme-backend",
        branch_name="forge/task-123",
    )
    workspace.path.mkdir()
    manager = MagicMock()
    manager.create_workspace.return_value = workspace
    git = MagicMock()
    git.remote_branch_exists.return_value = False
    jira = MagicMock(close=AsyncMock())
    github = MagicMock(close=AsyncMock())
    state = {
        "ticket_key": "TASK-123",
        "current_repo": "acme/backend",
        "fork_owner": "forge-bot",
        "fork_repo": "backend",
        "current_pr_number": 237,
        "rebase_return_node": "ci_evaluator",
    }

    with (
        patch("forge.workflow.nodes.rebase.get_settings"),
        patch("forge.workflow.nodes.rebase.JiraClient", return_value=jira),
        patch("forge.workflow.nodes.rebase.GitHubClient", return_value=github),
        patch("forge.workflow.nodes.rebase.get_workspace_manager", return_value=manager),
        patch("forge.workflow.nodes.rebase.GitOperations", return_value=git),
    ):
        await rebase_pr(state)

    assert (workspace.path / ".forge" / "workspace.json").read_text() == (
        '{"repo_name": "acme/backend", "ticket_key": "TASK-123"}\n'
    )
    git.clone.assert_called_once_with()
