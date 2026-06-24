"""Tests for GitOperations secret redaction."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.workspace.git_ops import GitError, GitOperations
from forge.workspace.manager import Workspace


def _git_ops(tmp_path: Path) -> GitOperations:
    token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
    settings = MagicMock()
    settings.github_token.get_secret_value.return_value = token
    workspace = Workspace(
        path=tmp_path / "repo",
        repo_name="org/repo",
        branch_name="forge/test",
        ticket_key="TEST-1",
    )
    with patch("forge.workspace.git_ops.get_settings", return_value=settings):
        return GitOperations(workspace)


def test_clone_failure_redacts_token_from_git_error(tmp_path):
    """subprocess CalledProcessError includes the command; GitError must not."""
    git = _git_ops(tmp_path)
    token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
    raw_url = f"https://x-access-token:{token}@github.com/org/repo.git"
    stderr = f"fatal: Authentication failed for '{raw_url}'"

    with (
        patch(
            "forge.workspace.git_ops.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                returncode=128,
                cmd=["git", "clone", "--single-branch", raw_url, str(git.repo_path)],
                stderr=stderr,
            ),
        ),
        pytest.raises(GitError) as exc_info,
    ):
        git.clone()

    message = str(exc_info.value)
    assert "ghp_" not in message
    assert raw_url not in message
    assert "https://[REDACTED]@github.com/org/repo.git" in message


def test_git_error_constructor_redacts_tokens():
    token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
    error = GitError(
        f"remote: https://x-access-token:{token}@github.com/org/repo.git"
    )

    assert "ghp_" not in str(error)
    assert "https://[REDACTED]@github.com/org/repo.git" in str(error)
