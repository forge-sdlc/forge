"""Tests for WorkspaceManager.destroy_workspace with podman unshare fallback."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from forge.workspace.manager import Workspace, WorkspaceManager


def _workspace(tmp_path: Path) -> Workspace:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "file.txt").write_text("content")
    return Workspace(path=ws, repo_name="org/repo", branch_name="forge/x", ticket_key="T-1")


class TestDestroyWorkspace:
    def test_uses_podman_unshare_when_available(self, tmp_path):
        ws = _workspace(tmp_path)
        manager = WorkspaceManager()
        manager._workspaces["T-1:org/repo"] = ws

        with (
            patch("forge.workspace.manager.shutil.which", return_value="/usr/bin/podman"),
            patch("forge.workspace.manager.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            manager.destroy_workspace(ws)

        mock_run.assert_called_once_with(
            ["/usr/bin/podman", "unshare", "rm", "-rf", str(ws.path)],
            check=False,
        )
        assert not ws.is_active

    def test_falls_back_to_shutil_when_podman_unavailable(self, tmp_path):
        ws = _workspace(tmp_path)
        manager = WorkspaceManager()
        manager._workspaces["T-1:org/repo"] = ws

        with (
            patch("forge.workspace.manager.shutil.which", return_value=None),
            patch("forge.workspace.manager.shutil.rmtree") as mock_rmtree,
        ):
            manager.destroy_workspace(ws)

        mock_rmtree.assert_called_once_with(ws.path)
        assert not ws.is_active

    def test_falls_back_to_shutil_when_podman_fails(self, tmp_path):
        ws = _workspace(tmp_path)
        manager = WorkspaceManager()
        manager._workspaces["T-1:org/repo"] = ws

        with (
            patch("forge.workspace.manager.shutil.which", return_value="/usr/bin/podman"),
            patch("forge.workspace.manager.subprocess.run") as mock_run,
            patch("forge.workspace.manager.shutil.rmtree") as mock_rmtree,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            manager.destroy_workspace(ws)

        mock_rmtree.assert_called_once_with(ws.path)

    def test_falls_back_to_shutil_when_podman_cannot_start(self, tmp_path):
        ws = _workspace(tmp_path)
        manager = WorkspaceManager()

        with (
            patch("forge.workspace.manager.shutil.which", return_value="/usr/bin/podman"),
            patch(
                "forge.workspace.manager.subprocess.run",
                side_effect=OSError("podman disappeared"),
            ),
            patch("forge.workspace.manager.shutil.rmtree") as mock_rmtree,
        ):
            manager.destroy_workspace(ws)

        mock_rmtree.assert_called_once_with(ws.path)

    def test_workspace_marked_inactive_and_removed_from_registry(self, tmp_path):
        ws = _workspace(tmp_path)
        manager = WorkspaceManager()
        manager._workspaces["T-1:org/repo"] = ws

        with (
            patch("forge.workspace.manager.shutil.which", return_value=None),
            patch("forge.workspace.manager.shutil.rmtree"),
        ):
            manager.destroy_workspace(ws)

        assert not ws.is_active
        assert "T-1:org/repo" not in manager._workspaces
