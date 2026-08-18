"""Tests for amending commits via GitOperations."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "dev@example.com")
    _run_git(repo, "config", "user.name", "Dev")
    (repo / "file.txt").write_text("v1\n")
    _run_git(repo, "add", "file.txt")
    _run_git(repo, "commit", "-m", "openflow: fix drain pending messages")
    return repo


def test_amend_commit_preserves_original_message(tmp_path, monkeypatch):
    """Review fixes should fold into HEAD without creating a second commit."""
    repo = _init_repo_with_commit(tmp_path)
    original = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    original_msg = _run_git(repo, "log", "-1", "--format=%s").stdout.strip()

    (repo / "file.txt").write_text("v2\n")

    settings = MagicMock(
        git_user_name="Forge Bot",
        git_user_email="forge-bot@example.com",
    )
    workspace = Workspace(
        path=repo,
        repo_name="org/repo",
        branch_name="forge/test-123",
        ticket_key="TEST-123",
    )
    with patch("forge.workspace.git_ops.get_settings", return_value=settings):
        git = GitOperations(workspace)

    empty_global_config = tmp_path / "empty-gitconfig"
    empty_global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global_config))

    assert git.amend_commit() is True

    new_sha = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    new_msg = _run_git(repo, "log", "-1", "--format=%s").stdout.strip()
    count = int(_run_git(repo, "rev-list", "--count", "HEAD").stdout.strip())

    assert new_sha != original
    assert new_msg == original_msg == "openflow: fix drain pending messages"
    assert count == 1
    assert (repo / "file.txt").read_text() == "v2\n"


def test_amend_commit_noop_when_clean(tmp_path):
    """Amend with no changes and no message rewrite returns False."""
    repo = _init_repo_with_commit(tmp_path)
    settings = MagicMock(
        git_user_name="Forge Bot",
        git_user_email="forge-bot@example.com",
    )
    workspace = Workspace(
        path=repo,
        repo_name="org/repo",
        branch_name="forge/test-123",
        ticket_key="TEST-123",
    )
    with patch("forge.workspace.git_ops.get_settings", return_value=settings):
        git = GitOperations(workspace)

    assert git.amend_commit() is False
