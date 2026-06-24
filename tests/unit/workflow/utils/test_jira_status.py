"""Tests for Jira status utility functions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from forge.models.workflow import ForgeLabel
from forge.workflow.utils.jira_status import (
    post_status_comment,
    set_implementing_label,
    transition_tasks_to_in_progress,
)


class TestPostStatusComment:
    """Test cases for the post_status_comment function."""

    @pytest.mark.asyncio
    async def test_post_status_comment_success(self) -> None:
        """Should successfully post a comment to Jira."""
        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()

        await post_status_comment(mock_jira, "TEST-123", "Test message")

        mock_jira.add_comment.assert_called_once_with("TEST-123", "Test message")

    @pytest.mark.asyncio
    async def test_post_status_comment_api_failure(self, caplog) -> None:
        """Should suppress HTTPError and log warning when API fails."""
        mock_jira = MagicMock()
        http_error = httpx.HTTPError("API error")
        mock_jira.add_comment = AsyncMock(side_effect=http_error)

        # Should not raise
        await post_status_comment(mock_jira, "TEST-456", "Test message")

        # Verify error was logged
        assert any(
            "Failed to post status comment to TEST-456" in record.message
            and record.levelname == "WARNING"
            for record in caplog.records
        )
        # Verify the exception message is in the log
        assert any("API error" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_post_status_comment_timeout(self, caplog) -> None:
        """Should suppress TimeoutError and log warning."""
        mock_jira = MagicMock()
        timeout_error = asyncio.TimeoutError()
        mock_jira.add_comment = AsyncMock(side_effect=timeout_error)

        # Should not raise
        await post_status_comment(mock_jira, "TEST-789", "Test message")

        # Verify error was logged
        assert any(
            "Failed to post status comment to TEST-789" in record.message
            and record.levelname == "WARNING"
            for record in caplog.records
        )


class TestTransitionTasksToInProgress:
    """Test cases for the transition_tasks_to_in_progress function."""

    @pytest.mark.asyncio
    async def test_transition_tasks_success(self, caplog) -> None:
        """Should successfully transition all tasks to In Progress."""
        mock_jira = MagicMock()
        mock_jira.transition_issue = AsyncMock()

        task_keys = ["TASK-1", "TASK-2", "TASK-3"]
        await transition_tasks_to_in_progress(mock_jira, task_keys)

        # Verify all tasks were transitioned
        assert mock_jira.transition_issue.call_count == 3
        mock_jira.transition_issue.assert_has_calls(
            [
                call("TASK-1", "In Progress"),
                call("TASK-2", "In Progress"),
                call("TASK-3", "In Progress"),
            ]
        )

        # Verify success logs for each task
        assert any(
            "Transitioned TASK-1 to In Progress" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )
        assert any(
            "Transitioned TASK-2 to In Progress" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )
        assert any(
            "Transitioned TASK-3 to In Progress" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_transition_tasks_partial_failure(self, caplog) -> None:
        """Should continue transitioning other tasks when some fail."""
        mock_jira = MagicMock()

        # Make the second task fail, but others succeed
        async def transition_side_effect(task_key: str, status: str):
            if task_key == "TASK-2":
                raise httpx.HTTPError("API error")

        mock_jira.transition_issue = AsyncMock(side_effect=transition_side_effect)

        task_keys = ["TASK-1", "TASK-2", "TASK-3"]
        await transition_tasks_to_in_progress(mock_jira, task_keys)

        # Verify all tasks were attempted
        assert mock_jira.transition_issue.call_count == 3

        # Verify success logs for tasks 1 and 3
        assert any(
            "Transitioned TASK-1 to In Progress" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )
        assert any(
            "Transitioned TASK-3 to In Progress" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )

        # Verify warning log for failed task 2
        assert any(
            "Failed to transition TASK-2 to In Progress" in record.message
            and record.levelname == "WARNING"
            and "API error" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_transition_tasks_transition_unavailable(self, caplog) -> None:
        """Should log warning and continue when transition is not available."""
        mock_jira = MagicMock()

        # Make the transition unavailable for TASK-2
        async def transition_side_effect(task_key: str, status: str):
            if task_key == "TASK-2":
                raise ValueError("Transition 'In Progress' not available")

        mock_jira.transition_issue = AsyncMock(side_effect=transition_side_effect)

        task_keys = ["TASK-1", "TASK-2", "TASK-3"]
        await transition_tasks_to_in_progress(mock_jira, task_keys)

        # Verify all tasks were attempted
        assert mock_jira.transition_issue.call_count == 3

        # Verify success logs for tasks 1 and 3
        assert any(
            "Transitioned TASK-1 to In Progress" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )
        assert any(
            "Transitioned TASK-3 to In Progress" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )

        # Verify specific warning for unavailable transition
        assert any(
            "Cannot transition TASK-2 to In Progress" in record.message
            and record.levelname == "WARNING"
            and "not available" in record.message
            for record in caplog.records
        )


class TestSetImplementingLabel:
    """Test cases for the set_implementing_label function."""

    @pytest.mark.asyncio
    async def test_set_implementing_label_success(self) -> None:
        """Should successfully set the implementing label on feature."""
        mock_jira = MagicMock()
        mock_jira.set_workflow_label = AsyncMock()

        await set_implementing_label(mock_jira, "FEATURE-123")

        mock_jira.set_workflow_label.assert_called_once_with(
            "FEATURE-123", ForgeLabel.TASK_IMPLEMENTING
        )

    @pytest.mark.asyncio
    async def test_set_implementing_label_failure(self, caplog) -> None:
        """Should suppress exceptions and log warning when label setting fails."""
        mock_jira = MagicMock()
        http_error = httpx.HTTPError("API error")
        mock_jira.set_workflow_label = AsyncMock(side_effect=http_error)

        # Should not raise
        await set_implementing_label(mock_jira, "FEATURE-456")

        # Verify error was logged with correct format
        assert any(
            "Failed to set implementing label on FEATURE-456" in record.message
            and record.levelname == "WARNING"
            and "API error" in record.message
            for record in caplog.records
        )
