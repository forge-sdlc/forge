"""Integration tests for task implementation status comments (SC-002).

These tests verify that task implementation status comments are posted correctly
to Jira tickets during the workflow execution. They test scenarios TS-003 and TS-013
from the specification, ensuring proper comment isolation and error handling.

Test Coverage:
- TS-003: Single task receives start and completion comments
- TS-013: Multiple tasks receive independent comments (no cross-contamination)
- Failure scenarios: No completion comment when implementation fails
- Error handling: Workflow continues when comment posting fails
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.implementation import implement_task


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.get_issue = AsyncMock()

    # Mock issue with description and summary
    mock_issue = MagicMock()
    mock_issue.description = "Task description for testing"
    mock_issue.summary = "Task summary for testing"
    mock.get_issue.return_value = mock_issue

    return mock


def create_mock_container_runner(success=True, error_message=None):
    """Create a mock ContainerRunner with configurable result."""
    mock = MagicMock()

    # Mock result
    mock_result = MagicMock()
    mock_result.success = success
    mock_result.error_message = error_message

    mock.run = AsyncMock(return_value=mock_result)
    return mock


class TestTaskImplementationStatusCommentsTS003:
    """Test TS-003: Single task receives start and completion comments."""

    @pytest.mark.asyncio
    async def test_single_task_receives_start_comment(self):
        """TS-003: Verify start comment '🔨 Forge is implementing this task.' is posted."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(success=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-100",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = "TASK-001"
        state["tasks_by_repo"] = {"owner/test-repo": ["TASK-001"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
        ):
            await implement_task(state)

        # Verify start comment was posted with exact text
        assert mock_jira.add_comment.call_count >= 1
        start_call = mock_jira.add_comment.call_args_list[0]
        assert start_call[0][0] == "TASK-001"
        assert start_call[0][1] == "🔨 Forge is implementing this task."

    @pytest.mark.asyncio
    async def test_single_task_receives_completion_comment_on_success(self):
        """TS-003: Verify completion comment is posted when task succeeds."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(success=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-100",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = "TASK-001"
        state["tasks_by_repo"] = {"owner/test-repo": ["TASK-001"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
        ):
            result = await implement_task(state)

        # Verify both start and completion comments were posted
        assert mock_jira.add_comment.call_count == 2

        # Verify start comment
        start_call = mock_jira.add_comment.call_args_list[0]
        assert start_call[0][0] == "TASK-001"
        assert start_call[0][1] == "🔨 Forge is implementing this task."

        # Verify completion comment with exact text
        completion_call = mock_jira.add_comment.call_args_list[1]
        assert completion_call[0][0] == "TASK-001"
        assert completion_call[0][1] == "✅ Implementation complete. Running local code review before PR."

        # Verify task was marked as implemented
        assert "TASK-001" in result["implemented_tasks"]

    @pytest.mark.asyncio
    async def test_single_task_no_completion_comment_on_failure(self):
        """TS-003: Verify NO completion comment when task implementation fails."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner(success=False, error_message="Implementation error")

        state = create_initial_feature_state(
            ticket_key="FEAT-100",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_task_key"] = "TASK-001"
        state["tasks_by_repo"] = {"owner/test-repo": ["TASK-001"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.implementation.notify_error", new=AsyncMock()),
        ):
            result = await implement_task(state)

        # Verify ONLY start comment was posted (no completion comment)
        assert mock_jira.add_comment.call_count == 1
        start_call = mock_jira.add_comment.call_args_list[0]
        assert start_call[0][0] == "TASK-001"
        assert start_call[0][1] == "🔨 Forge is implementing this task."

        # Verify error state
        assert result["last_error"] == "Implementation error"
        assert "TASK-001" not in result.get("implemented_tasks", [])


class TestTaskImplementationStatusCommentsTS013:
    """Test TS-013: Multiple tasks receive independent comments (no cross-contamination)."""

    @pytest.mark.asyncio
    async def test_multiple_tasks_receive_independent_start_comments(self):
        """TS-013: Verify each task receives its own start comment with correct task_key."""
        mock_jira1 = create_mock_jira_client()
        mock_runner1 = create_mock_container_runner(success=True)

        # First task
        state1 = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/multi-repo",
            task_keys=["TASK-100", "TASK-101"],
        )
        state1["workspace_path"] = "/tmp/test-workspace-multi"
        state1["tasks_by_repo"] = {"owner/multi-repo": ["TASK-100", "TASK-101"]}
        state1["implemented_tasks"] = []

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira1),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner1),
        ):
            result1 = await implement_task(state1)

        # Verify first task got start and completion comments with correct task_key
        assert mock_jira1.add_comment.call_count == 2
        assert mock_jira1.add_comment.call_args_list[0][0][0] == "TASK-100"
        assert mock_jira1.add_comment.call_args_list[0][0][1] == "🔨 Forge is implementing this task."
        assert mock_jira1.add_comment.call_args_list[1][0][0] == "TASK-100"

        # Reset mock for second task
        mock_jira2 = create_mock_jira_client()
        mock_runner2 = create_mock_container_runner(success=True)

        # Second task
        state2 = result1.copy()
        state2["current_task_key"] = None  # Let node find next task

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira2),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner2),
        ):
            result2 = await implement_task(state2)

        # Verify second task got its own independent start and completion comments
        assert mock_jira2.add_comment.call_count == 2
        assert mock_jira2.add_comment.call_args_list[0][0][0] == "TASK-101"
        assert mock_jira2.add_comment.call_args_list[0][0][1] == "🔨 Forge is implementing this task."
        assert mock_jira2.add_comment.call_args_list[1][0][0] == "TASK-101"

    @pytest.mark.asyncio
    async def test_multiple_tasks_receive_independent_completion_comments(self):
        """TS-013: Verify each task receives its own completion comment (no cross-contamination)."""
        # First task
        mock_jira1 = create_mock_jira_client()
        mock_runner1 = create_mock_container_runner(success=True)

        state1 = create_initial_feature_state(
            ticket_key="FEAT-201",
            current_repo="owner/isolation-test",
            task_keys=["TASK-200", "TASK-201", "TASK-202"],
        )
        state1["workspace_path"] = "/tmp/test-isolation"
        state1["tasks_by_repo"] = {"owner/isolation-test": ["TASK-200", "TASK-201", "TASK-202"]}
        state1["implemented_tasks"] = []

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira1),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner1),
        ):
            result1 = await implement_task(state1)

        # Verify TASK-200 got both comments
        task200_calls = [
            call for call in mock_jira1.add_comment.call_args_list if call[0][0] == "TASK-200"
        ]
        assert len(task200_calls) == 2
        assert task200_calls[0][0][1] == "🔨 Forge is implementing this task."
        assert task200_calls[1][0][1] == "✅ Implementation complete. Running local code review before PR."

        # Second task
        mock_jira2 = create_mock_jira_client()
        mock_runner2 = create_mock_container_runner(success=True)

        state2 = result1.copy()
        state2["current_task_key"] = None

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira2),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner2),
        ):
            result2 = await implement_task(state2)

        # Verify TASK-201 got both comments independently
        task201_calls = [
            call for call in mock_jira2.add_comment.call_args_list if call[0][0] == "TASK-201"
        ]
        assert len(task201_calls) == 2
        assert task201_calls[0][0][1] == "🔨 Forge is implementing this task."
        assert task201_calls[1][0][1] == "✅ Implementation complete. Running local code review before PR."

        # Third task
        mock_jira3 = create_mock_jira_client()
        mock_runner3 = create_mock_container_runner(success=True)

        state3 = result2.copy()
        state3["current_task_key"] = None

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira3),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner3),
        ):
            result3 = await implement_task(state3)

        # Verify TASK-202 got both comments independently (no cross-contamination)
        task202_calls = [
            call for call in mock_jira3.add_comment.call_args_list if call[0][0] == "TASK-202"
        ]
        assert len(task202_calls) == 2
        assert task202_calls[0][0][1] == "🔨 Forge is implementing this task."
        assert task202_calls[1][0][1] == "✅ Implementation complete. Running local code review before PR."

        # Verify all three tasks are marked as implemented
        assert result3["implemented_tasks"] == ["TASK-200", "TASK-201", "TASK-202"]


class TestTaskImplementationFailureScenarios:
    """Test failure scenarios for task implementation status comments."""

    @pytest.mark.asyncio
    async def test_task_implementation_fails_midway_no_completion_comment(self):
        """Verify no completion comment when task implementation fails midway."""
        mock_jira = create_mock_jira_client()
        # Simulate container failure midway through implementation
        mock_runner = create_mock_container_runner(success=False, error_message="Container crashed")

        state = create_initial_feature_state(
            ticket_key="FEAT-300",
            current_repo="owner/fail-repo",
            task_keys=["TASK-300"],
        )
        state["workspace_path"] = "/tmp/test-fail"
        state["current_task_key"] = "TASK-300"
        state["tasks_by_repo"] = {"owner/fail-repo": ["TASK-300"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
            patch("forge.workflow.nodes.implementation.notify_error", new=AsyncMock()),
        ):
            result = await implement_task(state)

        # Verify only start comment, no completion comment
        assert mock_jira.add_comment.call_count == 1
        assert mock_jira.add_comment.call_args_list[0][0][0] == "TASK-300"
        assert mock_jira.add_comment.call_args_list[0][0][1] == "🔨 Forge is implementing this task."

        # Verify error is set and task not implemented
        assert "Container crashed" in result["last_error"]
        assert "TASK-300" not in result.get("implemented_tasks", [])

    @pytest.mark.asyncio
    async def test_multiple_tasks_partial_failure_only_successful_get_completion(self):
        """Verify only successful tasks get completion comments when some fail."""
        # First task succeeds
        mock_jira1 = create_mock_jira_client()
        mock_runner1 = create_mock_container_runner(success=True)

        state1 = create_initial_feature_state(
            ticket_key="FEAT-301",
            current_repo="owner/partial-fail",
            task_keys=["TASK-401", "TASK-402"],
        )
        state1["workspace_path"] = "/tmp/test-partial-fail"
        state1["tasks_by_repo"] = {"owner/partial-fail": ["TASK-401", "TASK-402"]}
        state1["implemented_tasks"] = []

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira1),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner1),
        ):
            result1 = await implement_task(state1)

        # Verify first task got both comments
        assert mock_jira1.add_comment.call_count == 2
        assert "TASK-401" in result1["implemented_tasks"]

        # Second task fails
        mock_jira2 = create_mock_jira_client()
        mock_runner2 = create_mock_container_runner(success=False, error_message="Tests failed")

        state2 = result1.copy()
        state2["current_task_key"] = None

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira2),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner2),
            patch("forge.workflow.nodes.implementation.notify_error", new=AsyncMock()),
        ):
            result2 = await implement_task(state2)

        # Verify second task got only start comment (no completion)
        assert mock_jira2.add_comment.call_count == 1
        assert mock_jira2.add_comment.call_args_list[0][0][0] == "TASK-402"
        assert "TASK-402" not in result2.get("implemented_tasks", [])


class TestTaskImplementationErrorHandling:
    """Test error handling for task implementation status comments."""

    @pytest.mark.asyncio
    async def test_workflow_continues_when_start_comment_posting_fails(self, caplog):
        """Verify workflow continues when start comment posting fails."""
        mock_jira = create_mock_jira_client()
        # Make add_comment fail for start comment
        mock_jira.add_comment = AsyncMock(side_effect=Exception("Jira API timeout"))
        mock_runner = create_mock_container_runner(success=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-400",
            current_repo="owner/error-repo",
            task_keys=["TASK-500"],
        )
        state["workspace_path"] = "/tmp/test-error"
        state["current_task_key"] = "TASK-500"
        state["tasks_by_repo"] = {"owner/error-repo": ["TASK-500"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
        ):
            result = await implement_task(state)

        # Verify workflow continued despite comment failure
        assert "TASK-500" in result["implemented_tasks"]
        assert result["last_error"] is None

        # Verify error was logged (from jira_status utility)
        assert any(
            "Failed to post status comment to TASK-500" in record.message for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_workflow_continues_when_completion_comment_posting_fails(self, caplog):
        """Verify workflow continues when completion comment posting fails."""
        mock_jira = create_mock_jira_client()

        # Make add_comment succeed first time (start), fail second time (completion)
        call_count = 0

        async def add_comment_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Second call (completion comment)
                raise Exception("Jira API error on completion")
            return None

        mock_jira.add_comment = AsyncMock(side_effect=add_comment_side_effect)
        mock_runner = create_mock_container_runner(success=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-401",
            current_repo="owner/error-repo2",
            task_keys=["TASK-501"],
        )
        state["workspace_path"] = "/tmp/test-error2"
        state["current_task_key"] = "TASK-501"
        state["tasks_by_repo"] = {"owner/error-repo2": ["TASK-501"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
        ):
            result = await implement_task(state)

        # Verify workflow completed successfully despite completion comment failure
        assert "TASK-501" in result["implemented_tasks"]
        assert result["last_error"] is None

        # Verify error was logged
        assert any(
            "Failed to post status comment to TASK-501" in record.message for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_workflow_continues_when_all_comment_posting_fails(self, caplog):
        """Verify workflow continues when all comment posting fails."""
        mock_jira = create_mock_jira_client()
        # Make all add_comment calls fail
        mock_jira.add_comment = AsyncMock(side_effect=Exception("Complete Jira outage"))
        mock_runner = create_mock_container_runner(success=True)

        state = create_initial_feature_state(
            ticket_key="FEAT-402",
            current_repo="owner/error-repo3",
            task_keys=["TASK-502"],
        )
        state["workspace_path"] = "/tmp/test-error3"
        state["current_task_key"] = "TASK-502"
        state["tasks_by_repo"] = {"owner/error-repo3": ["TASK-502"]}

        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
        ):
            result = await implement_task(state)

        # Verify workflow completed successfully despite all comment failures
        assert "TASK-502" in result["implemented_tasks"]
        assert result["last_error"] is None

        # Verify errors were logged for both start and completion attempts
        error_logs = [
            record for record in caplog.records if "Failed to post status comment to TASK-502" in record.message
        ]
        assert len(error_logs) == 2  # Both start and completion comments should have logged errors
