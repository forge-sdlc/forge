"""Real-git tests for single-branch clones in direct mode.

These reproduce the direct-mode failure where ``git clone --single-branch``
restricts ``remote.origin.fetch`` to the default branch, so a later
unparameterized ``git fetch origin`` never creates the ``origin/<branch>``
tracking ref that ``checkout_branch``/``pull_rebase`` rely on. Mocking
``_run_git`` cannot catch this because the bug lives in git's refspec
behavior, not in Forge's argument strings.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from forge.integrations.source_control.contracts import GitCredentials
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _run(cwd: Path, *args: str) -> None:
    import os

    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _current_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare remote holding a single ``main`` branch.

    Returns (bare_remote_path, seed_working_clone) so the caller can push
    additional branches to the remote after the workspace has been cloned.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(seed, "init", "-b", "main")
    (seed / "README.md").write_text("hello\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-m", "initial")

    remote = tmp_path / "remote.git"
    _run(seed, "init", "--bare", str(remote))
    _run(seed, "remote", "add", "origin", str(remote))
    _run(seed, "push", "origin", "main")
    return remote, seed


def _git_ops(repo_path: Path, branch: str) -> GitOperations:
    workspace = Workspace(
        path=repo_path,
        repo_name="org/repo",
        branch_name=branch,
        ticket_key="TEST-123",
    )
    credentials = GitCredentials(host="github.com", token="test-token")
    with patch("forge.workspace.git_ops.get_settings", return_value=MagicMock()):
        return GitOperations(workspace, credentials)


def test_checkout_branch_fetches_branch_created_after_single_branch_clone(tmp_path):
    """A branch pushed to origin after a single-branch clone must still check out."""
    remote, seed = _make_remote(tmp_path)
    repo_path = tmp_path / "repo"
    git = _git_ops(repo_path, branch="forge/aisos-2420")
    git.clone(repo_url=str(remote))
    _run(repo_path, "config", "user.name", "Test")
    _run(repo_path, "config", "user.email", "test@example.com")

    # The feature branch appears on origin only after the single-branch clone,
    # so its origin/<branch> tracking ref does not exist locally yet.
    _run(seed, "checkout", "-b", "forge/aisos-2420")
    (seed / "feature.txt").write_text("feature\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-m", "feature commit")
    _run(seed, "push", "origin", "forge/aisos-2420")

    git.checkout_branch("forge/aisos-2420", remote="origin")

    assert _current_branch(repo_path) == "forge/aisos-2420"
    assert (repo_path / "feature.txt").exists()


def test_pull_rebase_direct_mode_rebases_after_single_branch_clone(tmp_path):
    """pull_rebase(origin) rebases onto a remote branch a single-branch clone missed."""
    remote, seed = _make_remote(tmp_path)
    repo_path = tmp_path / "repo"
    git = _git_ops(repo_path, branch="forge/aisos-2420")
    git.clone(repo_url=str(remote))
    _run(repo_path, "config", "user.name", "Test")
    _run(repo_path, "config", "user.email", "test@example.com")

    # Local feature branch with an unpushed commit.
    _run(repo_path, "checkout", "-b", "forge/aisos-2420")
    (repo_path / "local.txt").write_text("local\n")
    _run(repo_path, "add", ".")
    _run(repo_path, "commit", "-m", "local commit")

    # Remote feature branch advances independently.
    _run(seed, "checkout", "-b", "forge/aisos-2420")
    (seed / "remote.txt").write_text("remote\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-m", "remote commit")
    _run(seed, "push", "origin", "forge/aisos-2420")

    git.pull_rebase(remote="origin")

    # Rebase replayed the local commit on top of the fetched remote commit.
    assert (repo_path / "remote.txt").exists()
    assert (repo_path / "local.txt").exists()
