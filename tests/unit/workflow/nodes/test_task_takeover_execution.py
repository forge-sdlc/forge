"""Unit tests for task takeover execution node."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.nodes.task_takeover_execution import execute_task_changes


def _make_state(
    ticket_key="TASK-123",
    ticket_type=TicketType.TASK,
    workspace_path="/tmp/ws",
    current_repo="acme/backend",
    plan_content="This is the plan content.",
    implemented_tasks=None,
):
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


def _make_mock_jira(
    summary="Implement user authentication", description="Details of the authentication task"
):
    jira = AsyncMock()
    issue = MagicMock()
    issue.summary = summary
    issue.description = description
    jira.get_issue = AsyncMock(return_value=issue)
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()
    return jira


def _make_mock_runner(
    success=True, exit_code=0, stdout="Build successful", stderr="", error_message=None
):
    runner = MagicMock()
    result = MagicMock()
    result.success = success
    result.exit_code = exit_code
    result.stdout = stdout
    result.stderr = stderr
    result.error_message = error_message
    runner.run = AsyncMock(return_value=result)
    return runner


def _make_mock_git(has_changes=True, sha="abcdef1234567890"):
    git = MagicMock()
    git.has_uncommitted_changes = MagicMock(return_value=has_changes)
    git.stage_all = MagicMock()
    git.commit = MagicMock(return_value=True)
    git.get_current_sha = MagicMock(return_value=sha)
    return git


class TestTaskTakeoverExecutionNode:
    """Tests for execute_task_changes node in Task Takeover workflow."""

    @pytest.mark.asyncio
    async def test_successful_execution(self) -> None:
        """Test successful task takeover execution with code modifications and tests."""
        state = _make_state()
        mock_jira = _make_mock_jira()
        mock_runner = _make_mock_runner()
        mock_git = _make_mock_git()

        with (
            patch(
                "forge.workflow.nodes.task_takeover_execution.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.task_takeover_execution.ContainerRunner",
                return_value=mock_runner,
            ),
            patch(
                "forge.workflow.nodes.task_takeover_execution.prepare_workspace",
                return_value=("/tmp/ws", mock_git),
            ),
            patch("forge.workflow.nodes.task_takeover_execution.get_settings"),
        ):
            result_state = await execute_task_changes(state)

        # Assertions on state results
        assert result_state["task_execution_results"]["success"] is True
        assert result_state["task_execution_results"]["exit_code"] == 0
        assert result_state["task_execution_logs"]["stdout"] == "Build successful"
        assert result_state["commit_info"]["committed"] is True
        assert result_state["commit_info"]["sha"] == "abcdef1234567890"
        assert result_state["last_error"] is None
        assert result_state["retry_count"] == 0

        # Verify JIRA Client was called
        mock_jira.get_issue.assert_called_once_with("TASK-123")
        mock_jira.add_comment.assert_not_called()
        mock_jira.close.assert_called_once()

        # Verify ContainerRunner was called with correct parameters
        mock_runner.run.assert_called_once()
        kwargs = mock_runner.run.call_args.kwargs
        assert kwargs["workspace_path"] == Path("/tmp/ws")
        assert "Approved Implementation Plan" in kwargs["task_description"]
        assert "inject at least one new or modified test file" in kwargs["task_description"]

        # Verify GitOperations were performed
        mock_git.has_uncommitted_changes.assert_called_once()
        mock_git.stage_all.assert_called_once()
        mock_git.commit.assert_called_once()
        mock_git.get_current_sha.assert_called_once()
        mock_git.push_to_fork.assert_called_once()

    @pytest.mark.asyncio
    async def test_execution_failure(self) -> None:
        """Test that execution failures are recorded as non-blocking metrics/results in state."""
        state = _make_state()
        mock_jira = _make_mock_jira()
        mock_runner = _make_mock_runner(
            success=False, exit_code=2, stderr="Compilation error", error_message="Tests failed"
        )
        mock_git = _make_mock_git(has_changes=False)

        with (
            patch(
                "forge.workflow.nodes.task_takeover_execution.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.task_takeover_execution.ContainerRunner",
                return_value=mock_runner,
            ),
            patch(
                "forge.workflow.nodes.task_takeover_execution.prepare_workspace",
                return_value=("/tmp/ws", mock_git),
            ),
            patch("forge.workflow.nodes.task_takeover_execution.get_settings"),
        ):
            result_state = await execute_task_changes(state)

        # Non-blocking compilation and test execution failures: we update state and return it gracefully
        assert result_state["task_execution_results"]["success"] is False
        assert result_state["task_execution_results"]["exit_code"] == 2
        assert result_state["task_execution_results"]["error_message"] == "Tests failed"
        assert result_state["task_execution_logs"]["stderr"] == "Compilation error"
        assert result_state["commit_info"]["committed"] is False
        assert result_state["retry_count"] == 1

        mock_git.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_workspace_path(self) -> None:
        """Test graceful error handling when workspace_path is not set up."""
        state = _make_state(workspace_path=None)
        mock_jira = _make_mock_jira()

        with (
            patch(
                "forge.workflow.nodes.task_takeover_execution.JiraClient", return_value=mock_jira
            ),
            patch("forge.workflow.nodes.task_takeover_execution.get_settings"),
            patch(
                "forge.workflow.nodes.task_takeover_execution.prepare_workspace",
                side_effect=ValueError("Workspace not set up"),
            ),
        ):
            result_state = await execute_task_changes(state)

        assert result_state["last_error"] == "Workspace not set up"
        assert result_state["current_node"] == "execute_task_changes"

    @pytest.mark.asyncio
    async def test_unexpected_exception(self) -> None:
        """Test that unexpected exceptions are caught, logged, and updated in state."""
        state = _make_state()
        mock_jira = _make_mock_jira()
        mock_jira.get_issue.side_effect = Exception("Jira Connection Error")

        with (
            patch(
                "forge.workflow.nodes.task_takeover_execution.JiraClient", return_value=mock_jira
            ),
            patch("forge.workflow.nodes.task_takeover_execution.get_settings"),
            patch(
                "forge.workflow.nodes.task_takeover_execution.prepare_workspace",
                return_value=("/tmp/ws", _make_mock_git()),
            ),
        ):
            result_state = await execute_task_changes(state)

        assert result_state["last_error"] == "Jira Connection Error"
        assert result_state["current_node"] == "execute_task_changes"
        assert result_state["retry_count"] == 1
