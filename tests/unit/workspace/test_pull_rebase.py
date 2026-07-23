"""Tests for GitOperations.pull_rebase behavior."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace


def _git_ops(tmp_path: Path) -> GitOperations:
    settings = MagicMock()
    settings.github_token.get_secret_value.return_value = "ghp_fake"
    workspace = Workspace(
        path=tmp_path / "repo",
        repo_name="org/repo",
        branch_name="forge/test-123",
        ticket_key="TEST-123",
    )
    with patch("forge.workspace.git_ops.get_settings", return_value=settings):
        return GitOperations(workspace)


def test_pull_rebase_skips_when_remote_branch_missing(tmp_path):
    """First implementation run: remote branch doesn't exist yet, rebase must be skipped."""
    git = _git_ops(tmp_path)

    with (
        patch.object(git, "_run_git") as run_git,
        patch.object(git, "remote_branch_exists", return_value=False),
    ):
        git.pull_rebase(remote="origin")

    run_git.assert_called_once_with("fetch", "origin")


def test_pull_rebase_rebases_when_remote_branch_exists(tmp_path):
    """Subsequent runs: remote branch exists, rebase must happen."""
    git = _git_ops(tmp_path)

    with (
        patch.object(git, "_run_git") as run_git,
        patch.object(git, "remote_branch_exists", return_value=True),
    ):
        git.pull_rebase(remote="origin")

    assert run_git.call_args_list[0].args == ("fetch", "origin")
    assert run_git.call_args_list[1].args == ("rebase", "origin/forge/test-123")
