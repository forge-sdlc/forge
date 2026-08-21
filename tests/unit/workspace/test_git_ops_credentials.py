"""Tests for GitOperations' use of connection-scoped GitCredentials.

GitOperations must never hardcode github.com or the process-wide
GITHUB_TOKEN -- every clone/remote URL and TLS trust setting is derived
from the GitCredentials passed in at construction, so operations against a
non-default connection (GitHub Enterprise, or a second org with its own
token) hit the right host with the right credential.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from forge.integrations.source_control.contracts import GitCredentials
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace


def _git_ops(tmp_path: Path, credentials: GitCredentials) -> GitOperations:
    workspace = Workspace(
        path=tmp_path / "repo",
        repo_name="acme/widgets",
        branch_name="forge/test",
        ticket_key="TEST-1",
    )
    with patch("forge.workspace.git_ops.get_settings", return_value=MagicMock()):
        return GitOperations(workspace, credentials)


def test_clone_builds_url_from_enterprise_host_not_github_com(tmp_path):
    """A GitHub Enterprise connection's host must be used for the default
    clone URL, not a hardcoded github.com."""
    credentials = GitCredentials(host="ghe.example.com", token="ghe-token")
    git = _git_ops(tmp_path, credentials)

    with patch("forge.workspace.git_ops.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stderr="")
        git.clone()

    cmd = run.call_args.args[0]
    assert "https://x-access-token:ghe-token@ghe.example.com/acme/widgets.git" in cmd


def test_add_fork_remote_builds_url_from_credentials_host(tmp_path):
    """The fork remote URL must use this workspace's connection host/token,
    not the process-wide GITHUB_TOKEN against github.com."""
    credentials = GitCredentials(host="ghe.example.com", token="ghe-token")
    git = _git_ops(tmp_path, credentials)

    with patch.object(git, "_run_git") as run_git:
        run_git.return_value.stdout = ""
        git.add_fork_remote("forge-bot", "widgets")

    add_call = next(c for c in run_git.call_args_list if c.args[:2] == ("remote", "add"))
    fork_url = add_call.args[3]
    assert fork_url == "https://x-access-token:ghe-token@ghe.example.com/forge-bot/widgets.git"


def test_run_git_sets_ssl_cainfo_when_ca_path_configured(tmp_path):
    """A connection's CA bundle must be trusted via GIT_SSL_CAINFO, so a
    self-signed Enterprise Server cert doesn't fail TLS verification."""
    credentials = GitCredentials(
        host="ghe.example.com", token="ghe-token", ca_path="/etc/ssl/certs/ghe-ca.pem"
    )
    git = _git_ops(tmp_path, credentials)
    git.repo_path.mkdir(parents=True, exist_ok=True)

    with patch("forge.workspace.git_ops.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        git._run_git("status")

    assert run.call_args.kwargs["env"]["GIT_SSL_CAINFO"] == "/etc/ssl/certs/ghe-ca.pem"


def test_run_git_omits_env_override_when_no_ca_path(tmp_path):
    """The common case (no custom CA) must not override the subprocess
    environment at all -- inherits the process environment unmodified."""
    credentials = GitCredentials(host="github.com", token="test-token")
    git = _git_ops(tmp_path, credentials)
    git.repo_path.mkdir(parents=True, exist_ok=True)

    with patch("forge.workspace.git_ops.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        git._run_git("status")

    assert run.call_args.kwargs["env"] is None


def test_clone_sets_ssl_cainfo_when_ca_path_configured(tmp_path):
    """clone() bypasses _run_git (no repo to cwd into yet), so it must
    independently apply the same CA trust setting."""
    credentials = GitCredentials(
        host="ghe.example.com", token="ghe-token", ca_path="/etc/ssl/certs/ghe-ca.pem"
    )
    git = _git_ops(tmp_path, credentials)

    with patch("forge.workspace.git_ops.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stderr="")
        git.clone()

    assert run.call_args.kwargs["env"]["GIT_SSL_CAINFO"] == "/etc/ssl/certs/ghe-ca.pem"


def test_explicit_repo_url_is_used_as_is(tmp_path):
    """An explicit repo_url overrides the credentials-derived default."""
    credentials = GitCredentials(host="github.com", token="test-token")
    git = _git_ops(tmp_path, credentials)

    with patch("forge.workspace.git_ops.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stderr="")
        git.clone(repo_url="https://custom.example.com/some/repo.git")

    cmd = run.call_args.args[0]
    assert "https://custom.example.com/some/repo.git" in cmd
