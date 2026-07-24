"""Tests for GitOperations.push_to_fork remote fallback behavior."""

from pathlib import Path
from subprocess import CompletedProcess
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


def test_push_to_fork_uses_fork_when_available(tmp_path):
    git = _git_ops(tmp_path)

    def fake_run_git(*args, **kwargs):
        if args == ("remote",):
            return CompletedProcess(args=[], returncode=0, stdout="origin\nfork\n")
        return CompletedProcess(args=[], returncode=0, stdout="")

    with patch.object(git, "_run_git", side_effect=fake_run_git) as run_git:
        git.push_to_fork()

    run_git.assert_any_call("push", "-u", "fork", "forge/test-123")


def test_push_to_fork_falls_back_to_origin(tmp_path):
    git = _git_ops(tmp_path)

    def fake_run_git(*args, **kwargs):
        if args == ("remote",):
            return CompletedProcess(args=[], returncode=0, stdout="origin\n")
        return CompletedProcess(args=[], returncode=0, stdout="")

    with patch.object(git, "_run_git", side_effect=fake_run_git) as run_git:
        git.push_to_fork()

    run_git.assert_any_call("push", "-u", "origin", "forge/test-123")


def test_push_to_fork_force_flag(tmp_path):
    git = _git_ops(tmp_path)

    def fake_run_git(*args, **kwargs):
        if args == ("remote",):
            return CompletedProcess(args=[], returncode=0, stdout="origin\nfork\n")
        return CompletedProcess(args=[], returncode=0, stdout="")

    with patch.object(git, "_run_git", side_effect=fake_run_git) as run_git:
        git.push_to_fork(force=True)

    run_git.assert_any_call("push", "--force", "-u", "fork", "forge/test-123")
