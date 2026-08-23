from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.workspace.git_ops import GitError, GitOperations
from forge.workspace.manager import Workspace
from forge.workspace.output_validation import OutputValidationError


def _operations(tmp_path: Path) -> GitOperations:
    settings = MagicMock()
    settings.protected_output_paths = ("CODEOWNERS",)
    settings.output_max_file_bytes = 100
    settings.output_max_total_bytes = 200
    settings.output_base_ref = ""
    workspace = Workspace(tmp_path, "org/repo", "forge/task-1", "TASK-1")
    with patch("forge.workspace.git_ops.get_settings", return_value=settings):
        return GitOperations(workspace)


@pytest.mark.parametrize("method", ["push_to_fork", "push"])
def test_push_methods_validate_before_running_git(tmp_path: Path, method: str) -> None:
    git = _operations(tmp_path)
    git._run_git = MagicMock()

    with patch(
        "forge.workspace.git_ops.validate_repository_output",
        side_effect=OutputValidationError("blocked"),
    ) as validate, pytest.raises(GitError, match="blocked"):
        getattr(git, method)()

    validate.assert_called_once()
    assert validate.call_args.kwargs["head_ref"] == "refs/heads/forge/task-1"
    git._run_git.assert_not_called()
