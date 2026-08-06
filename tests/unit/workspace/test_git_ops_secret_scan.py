"""Git publication always passes through the trusted secret scan."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from forge.workspace.git_ops import GitOperations


def _operations(tmp_path: Path) -> GitOperations:
    operations = GitOperations.__new__(GitOperations)
    operations.workspace = SimpleNamespace(path=tmp_path, branch_name="forge/test")
    operations.settings = MagicMock()
    operations._run_git = MagicMock()
    return operations


def test_push_to_fork_scans_before_git(tmp_path: Path) -> None:
    operations = _operations(tmp_path)
    with patch("forge.workspace.git_ops.scan_repository") as scan:
        operations.push_to_fork()

    scan.assert_called_once_with(tmp_path)
    operations._run_git.assert_called_once_with("push", "-u", "fork", "forge/test")


def test_push_to_origin_scans_before_git(tmp_path: Path) -> None:
    operations = _operations(tmp_path)
    with patch("forge.workspace.git_ops.scan_repository") as scan:
        operations.push(check_conflicts=False)

    scan.assert_called_once_with(tmp_path)
    operations._run_git.assert_called_once_with("push", "-u", "origin", "forge/test")
