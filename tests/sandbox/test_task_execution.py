"""Integrated and sandbox tests for task execution in container environments."""

import json
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.sandbox.drivers.podman import PodmanDriver
from forge.sandbox.runner import ContainerConfig, ContainerResult, ContainerRunner
from forge.workflow.nodes.task_takeover_execution import execute_task_changes
from forge.workflow.nodes.workspace_setup import teardown_workspace


def _make_state(
    ticket_key: str = "TASK-123",
    ticket_type: TicketType = TicketType.TASK,
    workspace_path: str | None = "/tmp/ws",
    current_repo: str = "acme/backend",
    plan_content: str = "This is the approved plan.",
    implemented_tasks: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ticket_key": ticket_key,
        "ticket_type": ticket_type,
        "current_node": "execute_task_changes",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "workspace_path": workspace_path,
        "current_repo": current_repo,
        "plan_content": plan_content,
        "implemented_tasks": implemented_tasks or [],
        "context": {"branch_name": "forge/TASK-123", "guardrails": ""},
    }


def _make_mock_jira() -> AsyncMock:
    jira = AsyncMock()
    issue = MagicMock()
    issue.summary = "Fix validation bug"
    issue.description = "Validation logic in auth is failing"
    jira.get_issue = AsyncMock(return_value=issue)
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()
    return jira


def _make_mock_git(has_changes: bool = True, sha: str = "abcdef1234567890") -> MagicMock:
    git = MagicMock()
    git.has_uncommitted_changes = MagicMock(return_value=has_changes)
    git.stage_all = MagicMock()
    git.commit = MagicMock(return_value=True)
    git.get_current_sha = MagicMock(return_value=sha)
    return git


class TestTaskExecutionSandbox:
    """Integrated tests verifying ContainerRunner and workflow task execution."""

    @pytest.fixture(autouse=True)
    def mock_podman_exists(self) -> Generator[None, None, None]:
        with patch("shutil.which", return_value="/usr/bin/podman"):
            yield

    @pytest.mark.asyncio
    @patch("forge.sandbox.drivers.podman.asyncio.create_subprocess_exec")
    async def test_container_runner_successful_execution(self, mock_create_proc: AsyncMock) -> None:
        """Test ContainerRunner correctly runs a task with successful output."""
        # Arrange
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Agent finished successfully", b""))
        mock_proc.returncode = 0
        mock_create_proc.return_value = mock_proc

        runner = ContainerRunner(driver=PodmanDriver())
        config = ContainerConfig()

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)

            # Act
            result = await runner.run(
                workspace_path=workspace_path,
                task_summary="Add simple feature",
                task_description="Implement some changes",
                config=config,
                ticket_key="TASK-123",
                task_key="TASK-123",
                repo_name="acme/backend",
            )

            # Assert
            assert result.success is True
            assert result.exit_code == 0
            assert "Agent finished successfully" in result.stdout
            assert not (workspace_path / ".forge" / "task.json").exists()

            # Verify podman run command construction
            mock_create_proc.assert_called_once()
            cmd_args = mock_create_proc.call_args[0]
            assert cmd_args[0] == "podman"
            assert cmd_args[1] == "run"
            assert f"{workspace_path}:/workspace:Z" in cmd_args
            assert any("TASK-123" in arg for arg in cmd_args)
            assert "--memory" in cmd_args
            assert "--cpus" in cmd_args

    @pytest.mark.asyncio
    async def test_execute_task_changes_successful_workflow(self) -> None:
        """Test the execute_task_changes workflow node with successful container execution."""
        mock_jira = _make_mock_jira()
        mock_git = _make_mock_git(has_changes=True, sha="9876543210abcdef")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            state = _make_state(workspace_path=str(workspace_path))

            with (
                patch(
                    "forge.workflow.nodes.task_takeover_execution.JiraClient",
                    return_value=mock_jira,
                ),
                patch(
                    "forge.workflow.nodes.task_takeover_execution.prepare_workspace",
                    return_value=(str(workspace_path), mock_git),
                ),
                patch("forge.workflow.nodes.task_takeover_execution.get_settings"),
                patch(
                    "forge.workflow.nodes.task_takeover_execution.ContainerRunner",
                ) as MockRunner,
            ):
                mock_runner = MagicMock()
                mock_runner.run = AsyncMock(
                    return_value=ContainerResult(
                        success=True,
                        exit_code=0,
                        stdout="Implementing changes...\nTests passed!",
                        stderr="",
                    )
                )
                MockRunner.return_value = mock_runner

                # Act
                updated_state = await execute_task_changes(state)

            # Assert
            assert updated_state["task_execution_results"]["success"] is True
            assert updated_state["task_execution_results"]["exit_code"] == 0
            assert "Tests passed!" in updated_state["task_execution_logs"]["stdout"]
            assert updated_state["commit_info"]["committed"] is True
            assert updated_state["commit_info"]["sha"] == "9876543210abcdef"
            assert updated_state["last_error"] is None
            assert updated_state["retry_count"] == 0

            # Verify JIRA interactions
            mock_jira.get_issue.assert_called_once_with("TASK-123")
            mock_jira.close.assert_called_once()

            # Verify Git interactions on the host
            mock_git.has_uncommitted_changes.assert_called_once()
            mock_git.stage_all.assert_called_once()
            mock_git.commit.assert_called_once_with(
                "[TASK-123] feat: implement task takeover execution changes and tests"
            )

    @pytest.mark.asyncio
    async def test_build_and_test_recovery_workflow_iterative_self_correction(self) -> None:
        """Test build-and-test recovery workflow where compilation errors/test failures are fed back.

        We simulate a container execution that first fails (representing compilation/test failures),
        captures the failure logs back to the state, and on the subsequent retry/run,
        successfully implements self-correction and passes.
        """
        mock_jira = _make_mock_jira()
        mock_git_fail = _make_mock_git(has_changes=False)

        fail_result = ContainerResult(
            success=False,
            exit_code=2,
            stdout="Compiling and running tests...\nFailed!",
            stderr="SyntaxError: invalid syntax at auth.py line 25",
            error_message="Tests failed after max retries",
        )
        success_result = ContainerResult(
            success=True,
            exit_code=0,
            stdout="Self-corrected auth.py.\nAll compilation checks and tests passed successfully!",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            state_initial = _make_state(workspace_path=str(workspace_path))

            # --- FIRST RUN: Simulated compilation/test failure ---
            with (
                patch(
                    "forge.workflow.nodes.task_takeover_execution.JiraClient",
                    return_value=mock_jira,
                ),
                patch(
                    "forge.workflow.nodes.task_takeover_execution.prepare_workspace",
                    return_value=(str(workspace_path), mock_git_fail),
                ),
                patch("forge.workflow.nodes.task_takeover_execution.get_settings"),
                patch(
                    "forge.workflow.nodes.task_takeover_execution.ContainerRunner",
                ) as MockRunner,
            ):
                mock_runner = MagicMock()
                mock_runner.run = AsyncMock(return_value=fail_result)
                MockRunner.return_value = mock_runner
                state_after_fail = await execute_task_changes(state_initial)

            # Assert first run failed as expected
            assert state_after_fail["task_execution_results"]["success"] is False
            assert state_after_fail["task_execution_results"]["exit_code"] == 2
            assert "SyntaxError" in state_after_fail["task_execution_logs"]["stderr"]
            assert state_after_fail["retry_count"] == 1
            assert state_after_fail["commit_info"]["committed"] is False

            # --- SECOND RUN: Simulated self-correction and success ---
            mock_git_success = _make_mock_git(has_changes=True, sha="abcdef1234567890")

            with (
                patch(
                    "forge.workflow.nodes.task_takeover_execution.JiraClient",
                    return_value=mock_jira,
                ),
                patch(
                    "forge.workflow.nodes.task_takeover_execution.prepare_workspace",
                    return_value=(str(workspace_path), mock_git_success),
                ),
                patch("forge.workflow.nodes.task_takeover_execution.get_settings"),
                patch(
                    "forge.workflow.nodes.task_takeover_execution.ContainerRunner",
                ) as MockRunner2,
            ):
                mock_runner2 = MagicMock()
                mock_runner2.run = AsyncMock(return_value=success_result)
                MockRunner2.return_value = mock_runner2
                state_after_success = await execute_task_changes(state_after_fail)

            # Assert second run succeeded after self-correction
            assert state_after_success["task_execution_results"]["success"] is True
            assert state_after_success["task_execution_results"]["exit_code"] == 0
            assert "All compilation checks" in state_after_success["task_execution_logs"]["stdout"]
            assert state_after_success["retry_count"] == 0  # Reset after success
            assert state_after_success["commit_info"]["committed"] is True
            assert state_after_success["commit_info"]["sha"] == "abcdef1234567890"

    @pytest.mark.asyncio
    @patch("forge.workflow.nodes.workspace_setup.get_workspace_manager")
    async def test_teardown_workspace_secure_destruction(self, mock_get_manager: MagicMock) -> None:
        """Test teardown_workspace securely destroys the workspace and clears path in state."""
        # Arrange
        state = _make_state(workspace_path="/tmp/ws-to-teardown")
        mock_manager = MagicMock()
        mock_workspace = MagicMock()
        mock_manager.get_workspace.return_value = mock_workspace
        mock_get_manager.return_value = mock_manager

        # Act
        teardown_state = await teardown_workspace(state)

        # Assert
        assert teardown_state["workspace_path"] is None
        assert teardown_state["current_node"] == "workspace_complete"
        mock_manager.get_workspace.assert_called_once_with("TASK-123", "acme/backend")
        mock_manager.destroy_workspace.assert_called_once_with(mock_workspace)

    @pytest.mark.asyncio
    @patch("forge.workflow.nodes.workspace_setup.get_settings")
    @patch("forge.workflow.nodes.workspace_setup.get_workspace_manager")
    async def test_teardown_workspace_destroys_unregistered_state_path(
        self,
        mock_get_manager: MagicMock,
        mock_get_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A worker restart must not leak a workspace missing from the registry."""
        workspace_path = tmp_path / "forge-TASK-123-restarted"
        workspace_path.mkdir()
        forge_dir = workspace_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "workspace.json").write_text(
            json.dumps({"ticket_key": "TASK-123", "repo_name": "acme/backend"}, sort_keys=True)
        )
        state = _make_state(workspace_path=str(workspace_path))
        mock_manager = MagicMock()
        mock_manager.get_workspace.return_value = None
        mock_get_manager.return_value = mock_manager
        mock_get_settings.return_value.workspace_base_dir = str(tmp_path)

        teardown_state = await teardown_workspace(state)

        assert teardown_state["workspace_path"] is None
        recovered = mock_manager.destroy_workspace.call_args.args[0]
        assert recovered.path == workspace_path
        assert recovered.ticket_key == "TASK-123"
        assert recovered.repo_name == "acme/backend"

    @pytest.mark.asyncio
    @patch("forge.workflow.nodes.workspace_setup.get_settings")
    @patch("forge.workflow.nodes.workspace_setup.get_workspace_manager")
    async def test_teardown_workspace_rejects_unrecognized_state_path(
        self,
        mock_get_manager: MagicMock,
        mock_get_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The registry fallback cannot recursively delete an arbitrary path."""
        workspace_path = tmp_path / "not-a-forge-workspace"
        workspace_path.mkdir()
        state = _make_state(workspace_path=str(workspace_path))
        mock_manager = MagicMock()
        mock_manager.get_workspace.return_value = None
        mock_get_manager.return_value = mock_manager
        mock_get_settings.return_value.workspace_base_dir = str(tmp_path)

        teardown_state = await teardown_workspace(state)

        assert workspace_path.exists()
        assert teardown_state["workspace_path"] is None
        assert "Refusing to destroy unrecognized workspace path" in teardown_state["last_error"]
        mock_manager.destroy_workspace.assert_not_called()

    @pytest.mark.asyncio
    @patch("forge.workflow.nodes.workspace_setup.get_settings")
    @patch("forge.workflow.nodes.workspace_setup.get_workspace_manager")
    async def test_teardown_workspace_rejects_other_repo_identity(
        self,
        mock_get_manager: MagicMock,
        mock_get_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Cross-repo feature state cannot delete a different repo's workspace."""
        workspace_path = tmp_path / "forge-TASK-123-other-repo"
        workspace_path.mkdir()
        forge_dir = workspace_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "workspace.json").write_text(
            json.dumps({"ticket_key": "TASK-123", "repo_name": "acme/frontend"}, sort_keys=True)
        )
        state = _make_state(workspace_path=str(workspace_path), current_repo="acme/backend")
        mock_manager = MagicMock()
        mock_manager.get_workspace.return_value = None
        mock_get_manager.return_value = mock_manager
        mock_get_settings.return_value.workspace_base_dir = str(tmp_path)

        teardown_state = await teardown_workspace(state)

        assert workspace_path.exists()
        assert "Refusing to destroy unrecognized workspace path" in teardown_state["last_error"]
        mock_manager.destroy_workspace.assert_not_called()

    @pytest.mark.asyncio
    @patch("forge.workflow.nodes.workspace_setup.get_settings")
    @patch("forge.workflow.nodes.workspace_setup.get_workspace_manager")
    async def test_teardown_workspace_rejects_symlink_to_valid_workspace(
        self,
        mock_get_manager: MagicMock,
        mock_get_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A symlink pointing at a valid workspace must be rejected."""
        real_path = tmp_path / "forge-TASK-123-real"
        real_path.mkdir()
        forge_dir = real_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "workspace.json").write_text(
            json.dumps({"ticket_key": "TASK-123", "repo_name": "acme/backend"}, sort_keys=True)
        )
        symlink_path = tmp_path / "forge-TASK-123-link"
        symlink_path.symlink_to(real_path)
        state = _make_state(workspace_path=str(symlink_path))
        mock_manager = MagicMock()
        mock_manager.get_workspace.return_value = None
        mock_get_manager.return_value = mock_manager
        mock_get_settings.return_value.workspace_base_dir = str(tmp_path)

        teardown_state = await teardown_workspace(state)

        assert real_path.exists()
        assert "Refusing to destroy unrecognized workspace path" in teardown_state["last_error"]
        mock_manager.destroy_workspace.assert_not_called()
