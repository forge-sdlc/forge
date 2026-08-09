"""Git publication always passes through the trusted secret scan."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from forge.workspace.git_ops import GitOperations


def _operations(tmp_path: Path) -> GitOperations:
    operations = GitOperations.__new__(GitOperations)
    operations.workspace = SimpleNamespace(path=tmp_path, branch_name="forge/test")
    operations.settings = MagicMock()
    operations.output_validators = ()
    operations._run_git = MagicMock()
    return operations


def test_push_to_fork_uses_common_output_gate(tmp_path: Path) -> None:
    operations = _operations(tmp_path)
    operations.validate_output_for_push = MagicMock()
    operations.push_to_fork()

    operations.validate_output_for_push.assert_called_once_with()
    operations._run_git.assert_called_once_with("push", "-u", "fork", "forge/test")


def test_push_to_origin_uses_common_output_gate(tmp_path: Path) -> None:
    operations = _operations(tmp_path)
    operations.validate_output_for_push = MagicMock()
    operations.push(check_conflicts=False)

    operations.validate_output_for_push.assert_called_once_with()
    operations._run_git.assert_called_once_with("push", "-u", "origin", "forge/test")
