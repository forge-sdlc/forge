"""Tests for synchronizing implementation workspaces with remote branches."""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace


def _git_ops(tmp_path: Path) -> GitOperations:
    workspace = Workspace(
        path=tmp_path / "repo",
        repo_name="org/repo",
        branch_name="forge/test-123",
        ticket_key="TEST-123",
    )
    with patch("forge.workspace.git_ops.get_settings", return_value=MagicMock()):
        return GitOperations(workspace)


def test_pull_rebase_skips_missing_remote_branch_before_first_push(tmp_path):
    """A new implementation branch is local-only until its first backup push."""
    git = _git_ops(tmp_path)

    with (
        patch.object(git, "_run_git") as run_git,
        patch.object(git, "remote_branch_exists", return_value=False) as branch_exists,
    ):
        git.pull_rebase(remote="origin")

    run_git.assert_called_once_with("fetch", "origin")
    branch_exists.assert_called_once_with("forge/test-123", remote="origin")


def test_pull_rebase_rebases_when_remote_branch_exists(tmp_path):
    """Existing backup branches are still incorporated before implementation."""
    git = _git_ops(tmp_path)

    with (
        patch.object(git, "_run_git") as run_git,
        patch.object(git, "remote_branch_exists", return_value=True) as branch_exists,
    ):
        git.pull_rebase(remote="fork")

    assert run_git.call_args_list == [
        call("fetch", "fork"),
        call("rebase", "fork/forge/test-123"),
    ]
    branch_exists.assert_called_once_with("forge/test-123", remote="fork")
