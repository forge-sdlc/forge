"""Integration tests for PR creation status comments.

These tests verify that PR creation status comments are posted correctly
to Jira feature tickets when PRs are created, including label transitions
from forge:implementing to forge:ci-pending.

Test Coverage:
- TS-006: PR creation posts comment with PR number and updates labels
- TS-014: Comment uses fallback text when PR number unavailable
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.ci_evaluator import wait_for_ci_gate


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.remove_labels = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    return mock


class TestPRCreationStatusCommentsTS006:
    """Test TS-006: PR creation posts comment with PR number and updates labels."""

    @pytest.mark.asyncio
    async def test_pr_creation_posts_comment_with_pr_number(self):
        """TS-006: Verify comment posted with PR number when available."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify status comment posted with PR number
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-200"
        assert comment_call[0][1] == "🚀 Pull request #123 created and submitted. Waiting for CI checks to complete."

        # Verify workflow paused
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

    @pytest.mark.asyncio
    async def test_pr_creation_removes_implementing_label(self):
        """TS-006: Verify forge:implementing label removed from feature ticket."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify forge:implementing label removed
        assert mock_jira.remove_labels.call_count == 1
        remove_call = mock_jira.remove_labels.call_args
        assert remove_call[0][0] == "FEAT-200"
        assert "forge:implementing" in remove_call[0][1]

    @pytest.mark.asyncio
    async def test_pr_creation_adds_ci_pending_label(self):
        """TS-006: Verify forge:ci-pending label added to feature ticket."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify forge:ci-pending label added
        assert mock_jira.set_workflow_label.call_count == 1
        label_call = mock_jira.set_workflow_label.call_args
        assert label_call[0][0] == "FEAT-200"
        # Check that it's the CI_PENDING label (value is "forge:ci-pending")
        from forge.models.workflow import ForgeLabel
        assert label_call[0][1] == ForgeLabel.TASK_CI_PENDING

    @pytest.mark.asyncio
    async def test_pr_creation_jira_client_closed(self):
        """TS-006: Verify JiraClient properly closed after operations."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify JiraClient closed
        assert mock_jira.close.call_count == 1


class TestPRCreationStatusCommentsTS014:
    """Test TS-014: Comment uses fallback text when PR number unavailable."""

    @pytest.mark.asyncio
    async def test_pr_creation_posts_comment_without_pr_number(self):
        """TS-014: Verify fallback comment posted when PR number unavailable."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-201",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        # PR number is None
        state["current_pr_number"] = None
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify fallback comment posted without PR number
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-201"
        assert comment_call[0][1] == "🚀 Pull request created and submitted. Waiting for CI checks to complete."

        # Verify workflow paused
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

    @pytest.mark.asyncio
    async def test_pr_creation_labels_updated_without_pr_number(self):
        """TS-014: Verify label transitions work even without PR number."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-201",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = None
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify forge:implementing label removed
        assert mock_jira.remove_labels.call_count == 1
        # Verify forge:ci-pending label added
        assert mock_jira.set_workflow_label.call_count == 1


class TestPRCreationErrorHandling:
    """Test error handling for PR creation status updates."""

    @pytest.mark.asyncio
    async def test_workflow_continues_when_comment_posting_fails(self, caplog):
        """Verify workflow continues when status comment posting fails."""
        mock_jira = create_mock_jira_client()
        # Simulate comment posting failure
        mock_jira.add_comment.side_effect = Exception("Jira API error")

        state = create_initial_feature_state(
            ticket_key="FEAT-202",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 456
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues (doesn't raise exception)
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

        # Verify error logged
        assert any("Failed to post status comment" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_workflow_continues_when_label_removal_fails(self, caplog):
        """Verify workflow continues when label removal fails."""
        mock_jira = create_mock_jira_client()
        # Simulate label removal failure
        mock_jira.remove_labels.side_effect = Exception("Jira API error")

        state = create_initial_feature_state(
            ticket_key="FEAT-202",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 456
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

        # Verify error logged
        assert any("Failed to remove implementing label" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_workflow_continues_when_label_setting_fails(self, caplog):
        """Verify workflow continues when label setting fails."""
        mock_jira = create_mock_jira_client()
        # Simulate label setting failure
        mock_jira.set_workflow_label.side_effect = Exception("Jira API error")

        state = create_initial_feature_state(
            ticket_key="FEAT-202",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 456
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

        # Verify error logged
        assert any("Failed to set ci-pending label" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_jira_client_closed_even_on_error(self):
        """Verify JiraClient closed even when operations fail."""
        mock_jira = create_mock_jira_client()
        # Simulate all operations failing
        mock_jira.add_comment.side_effect = Exception("Jira API error")
        mock_jira.remove_labels.side_effect = Exception("Jira API error")
        mock_jira.set_workflow_label.side_effect = Exception("Jira API error")

        state = create_initial_feature_state(
            ticket_key="FEAT-202",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 456
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify JiraClient closed despite errors
        assert mock_jira.close.call_count == 1


class TestCIFixReentryNoStatusUpdate:
    """Test that re-entry after CI fix does not post status updates."""

    @pytest.mark.asyncio
    async def test_ci_fix_reentry_no_comment_posted(self):
        """Verify no comment posted when re-entering after CI fix."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-203",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 789
        state["ci_fix_attempts"] = 1  # Re-entry after CI fix

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify no comment posted
        assert mock_jira.add_comment.call_count == 0
        # Verify no label operations
        assert mock_jira.remove_labels.call_count == 0
        assert mock_jira.set_workflow_label.call_count == 0

        # Verify workflow paused
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

    @pytest.mark.asyncio
    async def test_ci_fix_reentry_multiple_attempts_no_comment(self):
        """Verify no comment posted for multiple CI fix re-entries."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-203",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 789
        state["ci_fix_attempts"] = 3  # Multiple re-entries

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify no Jira operations performed
        assert mock_jira.add_comment.call_count == 0
        assert mock_jira.remove_labels.call_count == 0
        assert mock_jira.set_workflow_label.call_count == 0

        # Verify workflow paused
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"
