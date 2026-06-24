"""Unit tests for task implementation status instrumentation.

These tests verify that the implement_task node correctly calls the
post_status_comment() utility function at the right times with the
correct parameters, independent of the Jira client implementation.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.implementation import implement_task


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.get_issue = AsyncMock()

    # Mock issue with description and summary
    mock_issue = MagicMock()
    mock_issue.description = "Task description"
    mock_issue.summary = "Task summary"
    mock.get_issue.return_value = mock_issue

    return mock


def create_mock_container_runner(success: bool = True, error_message: str | None = None):
    """Create a mock ContainerRunner.

    Args:
        success: Whether the container run should succeed
        error_message: Error message if success is False
    """
    mock = MagicMock()

    # Mock result
    mock_result = MagicMock()
    mock_result.success = success
    mock_result.error_message = error_message

    mock.run = AsyncMock(return_value=mock_result)
    return mock


class TestImplementationStatusInstrumentationStartComment:
    """Test cases for status comment at task implementation start."""

    @pytest.mark.asyncio
    async def test_post_status_comment_called_at_start_with_correct_params(self):
        """Should call post_status_comment() with correct task_key and start message."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(success=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
            task_keys=["TASK-1"],
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = "TASK-1"
        state["tasks_by_repo"] = {"owner/test-repo": ["TASK-1"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            result = await implement_task(state)

        # Verify post_status_comment was called at least once (for start)
        assert mock_post_status.call_count >= 1

        # Verify first call was the start comment
        first_call = mock_post_status.call_args_list[0]
        assert first_call[0][0] == mock_jira  # JiraClient instance
        assert first_call[0][1] == "TASK-1"  # task_key
        assert first_call[0][2] == "🔨 Forge started implementing [TASK-1]: Task summary"  # start message

    @pytest.mark.asyncio
    async def test_post_status_comment_called_before_container_execution(self):
        """Should call post_status_comment() before running the container."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(success=True)

        # Track call order
        call_order = []

        async def track_status_comment(*args, **kwargs):
            call_order.append("post_status_comment")

        async def track_container_run(*args, **kwargs):
            call_order.append("container_run")
            # Return the mock result
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.error_message = None
            return mock_result

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
            task_keys=["TASK-1"],
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = "TASK-1"
        state["tasks_by_repo"] = {"owner/test-repo": ["TASK-1"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment",
                side_effect=track_status_comment,
            ),
        ):
            mock_runner.run = AsyncMock(side_effect=track_container_run)
            result = await implement_task(state)

        # Verify post_status_comment was called before container_run
        assert call_order.index("post_status_comment") < call_order.index("container_run")


class TestImplementationStatusInstrumentationCompletionComment:
    """Test cases for status comment at task implementation completion."""

    @pytest.mark.asyncio
    async def test_post_status_comment_called_at_completion_on_success(self):
        """Should call post_status_comment() with completion message on successful implementation."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(success=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
            task_keys=["TASK-1"],
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = "TASK-1"
        state["tasks_by_repo"] = {"owner/test-repo": ["TASK-1"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status,
        ):
            mock_post_status.return_value = AsyncMock()
            result = await implement_task(state)

        # Verify post_status_comment was called twice (start + completion)
        assert mock_post_status.call_count == 2

        # Verify second call was the completion comment
        second_call = mock_post_status.call_args_list[1]
        assert second_call[0][0] == mock_jira  # JiraClient instance
        assert second_call[0][1] == "TASK-1"  # task_key
        assert (
            second_call[0][2]
            == "✅ Implementation complete. Running local code review before PR."
        )

    @pytest.mark.asyncio
    async def test_post_status_comment_not_called_at_completion_on_failure(self):
        """Should NOT call post_status_comment() with completion message when implementation fails."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(success=False, error_message="Container failed")

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
            task_keys=["TASK-1"],
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = "TASK-1"
        state["tasks_by_repo"] = {"owner/test-repo": ["TASK-1"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status,
            patch("forge.workflow.nodes.implementation.notify_error", new=AsyncMock()),
        ):
            mock_post_status.return_value = AsyncMock()
            result = await implement_task(state)

        # Verify post_status_comment was called only once (start, NOT completion)
        assert mock_post_status.call_count == 1

        # Verify only call was the start comment
        first_call = mock_post_status.call_args_list[0]
        assert first_call[0][2] == "🔨 Forge started implementing [TASK-1]: Task summary"


class TestImplementationStatusInstrumentationMultipleTasks:
    """Test cases for multiple sequential tasks."""

    @pytest.mark.asyncio
    async def test_multiple_tasks_use_correct_task_key_for_each_comment(self):
        """Should use the correct task_key for each task's status comments."""
        # First task
        mock_jira1 = create_mock_jira_client()
        mock_runner1 = create_mock_container_runner(success=True)

        state1 = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
            task_keys=["TASK-1", "TASK-2", "TASK-3"],
        )
        state1["workspace_path"] = "/tmp/test-workspace"
        state1["current_task_key"] = "TASK-1"
        state1["tasks_by_repo"] = {"owner/test-repo": ["TASK-1", "TASK-2", "TASK-3"]}
        state1["implemented_tasks"] = []

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira1),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner1),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status1,
        ):
            mock_post_status1.return_value = AsyncMock()
            result1 = await implement_task(state1)

        # Verify TASK-1 comments
        assert mock_post_status1.call_count == 2
        assert all(call[0][1] == "TASK-1" for call in mock_post_status1.call_args_list)

        # Second task
        mock_jira2 = create_mock_jira_client()
        mock_runner2 = create_mock_container_runner(success=True)

        state2 = result1.copy()
        state2["current_task_key"] = None  # Let node find next task

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira2),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner2),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status2,
        ):
            mock_post_status2.return_value = AsyncMock()
            result2 = await implement_task(state2)

        # Verify TASK-2 comments
        assert mock_post_status2.call_count == 2
        assert all(call[0][1] == "TASK-2" for call in mock_post_status2.call_args_list)

        # Third task
        mock_jira3 = create_mock_jira_client()
        mock_runner3 = create_mock_container_runner(success=True)

        state3 = result2.copy()
        state3["current_task_key"] = None  # Let node find next task

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira3),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner3),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status3,
        ):
            mock_post_status3.return_value = AsyncMock()
            result3 = await implement_task(state3)

        # Verify TASK-3 comments
        assert mock_post_status3.call_count == 2
        assert all(call[0][1] == "TASK-3" for call in mock_post_status3.call_args_list)

    @pytest.mark.asyncio
    async def test_multiple_tasks_mixed_success_failure_correct_task_keys(self):
        """Should use correct task_key even when some tasks succeed and some fail."""
        # First task - success
        mock_jira1 = create_mock_jira_client()
        mock_runner1 = create_mock_container_runner(success=True)

        state1 = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/test-repo",
            task_keys=["TASK-1", "TASK-2"],
        )
        state1["workspace_path"] = "/tmp/test-workspace"
        state1["current_task_key"] = "TASK-1"
        state1["tasks_by_repo"] = {"owner/test-repo": ["TASK-1", "TASK-2"]}
        state1["implemented_tasks"] = []

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira1),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner1),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status1,
        ):
            mock_post_status1.return_value = AsyncMock()
            result1 = await implement_task(state1)

        # Verify TASK-1 got both comments
        assert mock_post_status1.call_count == 2
        assert all(call[0][1] == "TASK-1" for call in mock_post_status1.call_args_list)

        # Second task - failure
        mock_jira2 = create_mock_jira_client()
        mock_runner2 = create_mock_container_runner(
            success=False, error_message="Implementation failed"
        )

        state2 = result1.copy()
        state2["current_task_key"] = None  # Let node find next task

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira2),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner2),
            patch(
                "forge.workflow.nodes.implementation.post_status_comment"
            ) as mock_post_status2,
            patch("forge.workflow.nodes.implementation.notify_error", new=AsyncMock()),
        ):
            mock_post_status2.return_value = AsyncMock()
            result2 = await implement_task(state2)

        # Verify TASK-2 got only start comment (not completion)
        assert mock_post_status2.call_count == 1
        assert mock_post_status2.call_args_list[0][0][1] == "TASK-2"
        assert (
            mock_post_status2.call_args_list[0][0][2] == "🔨 Forge started implementing [TASK-2]: Task summary"
        )


