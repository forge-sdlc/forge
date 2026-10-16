"""Regression tests for failed workspace recreation cleanup (#191)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.workflow.nodes.workspace_setup import _recreate_workspace_from_fork


def test_recreate_cleans_empty_target_when_checkout_fails(tmp_path):
    """Failed fork checkout must not leave orphan forge-* directories behind."""
    ticket = "AISOS-2119"
    created_dirs: list[Path] = []

    def fake_create_workspace(*, repo_name, ticket_key, branch_name=None):
        path = Path(tmp_path) / f"forge-{ticket_key}-orphan"
        path.mkdir(parents=True, exist_ok=True)
        created_dirs.append(path)
        ws = MagicMock()
        ws.path = path
        ws.repo_name = repo_name
        ws.ticket_key = ticket_key
        ws.branch_name = branch_name or f"forge/{ticket_key.lower()}"
        return ws

    mock_git = MagicMock()
    mock_git.clone = MagicMock()
    mock_git.add_fork_remote = MagicMock()
    mock_git.checkout_branch = MagicMock(
        side_effect=RuntimeError(
            "fatal: 'fork/forge/aisos-2119' is not a commit and a branch "
            "'forge/aisos-2119' cannot be created from it"
        )
    )

    with (
        patch(
            "forge.workflow.nodes.workspace_setup.get_settings",
            return_value=MagicMock(workspace_base_dir=None),
        ),
        patch(
            "forge.workflow.nodes.workspace_setup.WorkspaceManager"
        ) as mock_manager_cls,
        patch(
            "forge.workflow.nodes.workspace_setup.GitOperations",
            return_value=mock_git,
        ),
    ):
        manager = MagicMock()
        manager.create_workspace = MagicMock(side_effect=fake_create_workspace)
        manager.destroy_workspace = MagicMock()
        mock_manager_cls.return_value = manager

        with pytest.raises(RuntimeError, match="is not a commit"):
            _recreate_workspace_from_fork(
                ticket_key=ticket,
                current_repo="org/repo",
                branch_name="forge/aisos-2119",
                fork_owner="bot",
                fork_repo="repo",
            )

    assert created_dirs, "expected create_workspace to allocate a target dir"
    assert not created_dirs[0].exists(), "orphan target directory must be removed"
    manager.destroy_workspace.assert_called_once()
