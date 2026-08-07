"""Tests for commits created by GitOperations."""

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


def test_commit_supplies_identity_without_global_git_config(tmp_path, monkeypatch):
    """Host commits must not depend on the worker account's Git configuration."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")

    changed_file = repo / "change.txt"
    changed_file.write_text("change\n")
    _run_git(repo, "add", "change.txt")

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
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)

    assert git.commit("Test commit", author_name="Task Agent") is True

    identity = _run_git(
        repo,
        "show",
        "-s",
        "--format=%an <%ae>%n%cn <%ce>",
        "HEAD",
    ).stdout.strip()
    assert identity == ("Task Agent <forge-bot@example.com>\nForge Bot <forge-bot@example.com>")


def test_commit_ignores_untracked_forge_metadata(tmp_path):
    """Internal metadata must not cause an empty commit attempt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    forge_dir = repo / ".forge"
    forge_dir.mkdir()
    (forge_dir / "handoff.md").write_text("internal\n")

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

    assert git.commit("Test commit") is False


def test_has_uncommitted_changes_excludes_forge_metadata(tmp_path):
    """Forge metadata alone must not trigger host-side staging and commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")

    settings = MagicMock()
    workspace = Workspace(
        path=repo,
        repo_name="org/repo",
        branch_name="forge/test-123",
        ticket_key="TEST-123",
    )
    with patch("forge.workspace.git_ops.get_settings", return_value=settings):
        git = GitOperations(workspace)

    forge_dir = repo / ".forge"
    forge_dir.mkdir()
    (forge_dir / "handoff.md").write_text("internal\n")
    assert git.has_uncommitted_changes() is False

    (repo / "implementation.py").write_text("changed = True\n")
    assert git.has_uncommitted_changes() is True
