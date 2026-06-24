"""Unit tests for PR status comment and label transition logic.

These tests verify the core logic of PR creation status comments and label
transitions in the wait_for_ci_gate node, focusing on:
- PR number extraction (valid, missing, malformed)
- PR status comment posting with/without PR number
- Label removal (forge:implementing) with success and failure cases
- Label addition (forge:ci-pending) with success and failure cases
- Error suppression and logging for all operations
- Workflow continuation after failures
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.ci_evaluator import wait_for_ci_gate


def create_mock_jira_client():
    """Create a mock JiraClient with all required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.remove_labels = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    return mock


class TestPRNumberExtraction:
    """Test PR number extraction from workflow state."""

    @pytest.mark.asyncio
    async def test_pr_number_extraction_with_valid_response(self):
        """Verify PR number is correctly extracted when present in state."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-100",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 42
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify PR number used in status comment
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert "Pull request #42" in comment_call[0][1]

    @pytest.mark.asyncio
    async def test_pr_number_extraction_with_missing_pr_number(self):
        """Verify fallback message when PR number is None."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-101",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = None
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify fallback message used
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][1] == "🚀 Pull request created and submitted. Waiting for CI checks to complete."
        assert "#" not in comment_call[0][1]

    @pytest.mark.asyncio
    async def test_pr_number_extraction_with_malformed_response(self):
        """Verify handling when current_pr_number key is missing from state."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-102",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        # Don't set current_pr_number at all
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify fallback message used when key is missing
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][1] == "🚀 Pull request created and submitted. Waiting for CI checks to complete."


class TestPRStatusCommentPosting:
    """Test PR status comment posting logic."""

    @pytest.mark.asyncio
    async def test_status_comment_posted_with_pr_number_present(self):
        """Verify status comment posted with PR number when available."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-200",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 999
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify comment posted to correct ticket with correct message
        mock_jira.add_comment.assert_called_once_with(
            "TEST-200",
            "🚀 Pull request #999 created and submitted. Waiting for CI checks to complete."
        )

    @pytest.mark.asyncio
    async def test_status_comment_posted_with_pr_number_absent(self):
        """Verify fallback status comment posted when PR number absent."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-201",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = None
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify fallback comment posted to correct ticket
        mock_jira.add_comment.assert_called_once_with(
            "TEST-201",
            "🚀 Pull request created and submitted. Waiting for CI checks to complete."
        )

    @pytest.mark.asyncio
    async def test_status_comment_not_posted_on_reentry(self):
        """Verify status comment only posted on initial entry, not after CI fix."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-202",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 1  # Re-entry after CI fix

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify NO comment posted on re-entry
        mock_jira.add_comment.assert_not_called()


class TestLabelRemoval:
    """Test forge:implementing label removal logic."""

    @pytest.mark.asyncio
    async def test_label_removal_success(self):
        """Verify forge:implementing label removed successfully."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-300",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 100
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify remove_labels called with correct parameters
        mock_jira.remove_labels.assert_called_once_with(
            "TEST-300",
            ["forge:implementing"]
        )
        # Verify workflow continues
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

    @pytest.mark.asyncio
    async def test_label_removal_label_not_present(self, caplog):
        """Verify workflow continues when label is not present on ticket."""
        mock_jira = create_mock_jira_client()
        # Simulate label not present (Jira API might raise exception)
        mock_jira.remove_labels.side_effect = Exception("Label not found")

        state = create_initial_feature_state(
            ticket_key="TEST-301",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 101
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues despite error
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        # Verify error logged (via post_status_comment utility)
        assert any("Failed to remove implementing label" in record.message 
                   for record in caplog.records if record.levelname == "WARNING")

    @pytest.mark.asyncio
    async def test_label_removal_api_error(self, caplog):
        """Verify workflow continues when label removal API fails."""
        mock_jira = create_mock_jira_client()
        # Simulate API error
        mock_jira.remove_labels.side_effect = Exception("Jira API timeout")

        state = create_initial_feature_state(
            ticket_key="TEST-302",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 102
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues despite API error
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        # Verify error logged at WARNING level
        assert any("Failed to remove implementing label" in record.message 
                   for record in caplog.records if record.levelname == "WARNING")

    @pytest.mark.asyncio
    async def test_label_removal_not_called_on_reentry(self):
        """Verify label removal only happens on initial entry, not after CI fix."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-303",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 103
        state["ci_fix_attempts"] = 2  # Re-entry after CI fix

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify remove_labels NOT called on re-entry
        mock_jira.remove_labels.assert_not_called()


class TestLabelAddition:
    """Test forge:ci-pending label addition logic."""

    @pytest.mark.asyncio
    async def test_label_addition_success(self):
        """Verify forge:ci-pending label added successfully."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-400",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 200
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify set_workflow_label called with forge:ci-pending
        from forge.models.workflow import ForgeLabel
        mock_jira.set_workflow_label.assert_called_once_with(
            "TEST-400",
            ForgeLabel.TASK_CI_PENDING
        )
        # Verify workflow continues
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

    @pytest.mark.asyncio
    async def test_label_addition_api_error(self, caplog):
        """Verify workflow continues when label addition API fails."""
        mock_jira = create_mock_jira_client()
        # Simulate API error
        mock_jira.set_workflow_label.side_effect = Exception("Jira API connection error")

        state = create_initial_feature_state(
            ticket_key="TEST-401",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 201
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues despite API error
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        # Verify error logged at WARNING level
        assert any("Failed to set ci-pending label" in record.message 
                   for record in caplog.records if record.levelname == "WARNING")

    @pytest.mark.asyncio
    async def test_label_addition_not_called_on_reentry(self):
        """Verify label addition only happens on initial entry, not after CI fix."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="TEST-402",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 202
        state["ci_fix_attempts"] = 3  # Re-entry after CI fix

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify set_workflow_label NOT called on re-entry
        mock_jira.set_workflow_label.assert_not_called()


class TestErrorSuppressionAndLogging:
    """Test error suppression and logging for all label operations."""

    @pytest.mark.asyncio
    async def test_comment_posting_error_logged_and_suppressed(self, caplog):
        """Verify comment posting errors are logged at WARNING level and suppressed."""
        mock_jira = create_mock_jira_client()
        mock_jira.add_comment.side_effect = Exception("Comment API error")

        state = create_initial_feature_state(
            ticket_key="TEST-500",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 300
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues (exception suppressed)
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        # Verify error logged
        assert any("Failed to post status comment" in record.message 
                   for record in caplog.records if record.levelname == "WARNING")

    @pytest.mark.asyncio
    async def test_label_removal_error_logged_and_suppressed(self, caplog):
        """Verify label removal errors are logged at WARNING level and suppressed."""
        mock_jira = create_mock_jira_client()
        mock_jira.remove_labels.side_effect = Exception("Remove label API error")

        state = create_initial_feature_state(
            ticket_key="TEST-501",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 301
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues
        assert result["is_paused"] is True
        # Verify error logged
        assert any("Failed to remove implementing label" in record.message 
                   for record in caplog.records if record.levelname == "WARNING")

    @pytest.mark.asyncio
    async def test_label_addition_error_logged_and_suppressed(self, caplog):
        """Verify label addition errors are logged at WARNING level and suppressed."""
        mock_jira = create_mock_jira_client()
        mock_jira.set_workflow_label.side_effect = Exception("Add label API error")

        state = create_initial_feature_state(
            ticket_key="TEST-502",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 302
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues
        assert result["is_paused"] is True
        # Verify error logged
        assert any("Failed to set ci-pending label" in record.message 
                   for record in caplog.records if record.levelname == "WARNING")

    @pytest.mark.asyncio
    async def test_all_operations_fail_workflow_still_continues(self, caplog):
        """Verify workflow continues when all Jira operations fail."""
        mock_jira = create_mock_jira_client()
        # Simulate all operations failing
        mock_jira.add_comment.side_effect = Exception("Comment failed")
        mock_jira.remove_labels.side_effect = Exception("Remove failed")
        mock_jira.set_workflow_label.side_effect = Exception("Add failed")

        state = create_initial_feature_state(
            ticket_key="TEST-503",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 303
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues despite all failures
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        # Verify all errors logged
        warning_messages = [record.message for record in caplog.records if record.levelname == "WARNING"]
        assert any("Failed to post status comment" in msg for msg in warning_messages)
        assert any("Failed to remove implementing label" in msg for msg in warning_messages)
        assert any("Failed to set ci-pending label" in msg for msg in warning_messages)


class TestWorkflowContinuation:
    """Test that workflow continues after comment/label failures."""

    @pytest.mark.asyncio
    async def test_workflow_continues_after_comment_failure(self):
        """Verify workflow continues to completion after comment posting fails."""
        mock_jira = create_mock_jira_client()
        mock_jira.add_comment.side_effect = Exception("Comment API down")

        state = create_initial_feature_state(
            ticket_key="TEST-600",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 400
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify state updated correctly
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        assert result["ticket_key"] == "TEST-600"

    @pytest.mark.asyncio
    async def test_workflow_continues_after_label_failures(self):
        """Verify workflow continues after both label operations fail."""
        mock_jira = create_mock_jira_client()
        mock_jira.remove_labels.side_effect = Exception("Cannot remove")
        mock_jira.set_workflow_label.side_effect = Exception("Cannot add")

        state = create_initial_feature_state(
            ticket_key="TEST-601",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 401
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify state progresses correctly
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        # Verify JiraClient still closed properly
        mock_jira.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_jira_client_closed_even_after_failures(self):
        """Verify JiraClient is properly closed even when operations fail."""
        mock_jira = create_mock_jira_client()
        mock_jira.add_comment.side_effect = Exception("Comment failed")
        mock_jira.remove_labels.side_effect = Exception("Remove failed")
        mock_jira.set_workflow_label.side_effect = Exception("Add failed")

        state = create_initial_feature_state(
            ticket_key="TEST-602",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 402
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify JiraClient.close() called in finally block
        mock_jira.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_workflow_continues_with_mixed_success_and_failure(self):
        """Verify workflow continues when some operations succeed and some fail."""
        mock_jira = create_mock_jira_client()
        # Comment succeeds, remove_labels fails, set_workflow_label succeeds
        mock_jira.remove_labels.side_effect = Exception("Remove failed")

        state = create_initial_feature_state(
            ticket_key="TEST-603",
            current_repo="owner/repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 403
        state["ci_fix_attempts"] = 0

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow completes successfully
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
        # Verify successful operations were called
        mock_jira.add_comment.assert_called_once()
        mock_jira.set_workflow_label.assert_called_once()
