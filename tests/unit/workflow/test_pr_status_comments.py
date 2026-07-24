"""Unit tests for PR status comment and label transition logic.

These tests verify the core logic of PR creation status comments and label
transitions in the human_review_gate node, focusing on:
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
from forge.workflow.nodes.human_review import human_review_gate


def create_mock_jira_client():
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.remove_labels = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    return mock


def _initial_state(**overrides):
    """Build a minimal initial-entry state (ci_status=None)."""
    state = create_initial_feature_state(
        ticket_key=overrides.pop("ticket_key", "TEST-100"),
    )
    state["ci_fix_attempt"] = 0
    state.update(overrides)
    # Ensure ci_status is None unless explicitly overridden — this triggers
    # the initial-entry branch that posts the PR comment and swaps labels.
    state.setdefault("ci_status", None)
    return state


class TestPRNumberExtraction:
    """Test PR number extraction from workflow state."""

    @pytest.mark.asyncio
    async def test_pr_number_extraction_with_valid_response(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(ticket_key="TEST-100", current_pr_number=42)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        assert mock_jira.add_comment.call_count == 1
        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "#42" in comment_text

    @pytest.mark.asyncio
    async def test_pr_number_extraction_with_missing_pr_number(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(ticket_key="TEST-101", current_pr_number=None)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        assert mock_jira.add_comment.call_count == 1
        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "#" not in comment_text
        assert "Pull request created and submitted" in comment_text

    @pytest.mark.asyncio
    async def test_pr_number_extraction_with_key_absent(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(ticket_key="TEST-102")
        # create_initial_feature_state sets current_pr_number=None by default
        state.pop("current_pr_number", None)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        assert mock_jira.add_comment.call_count == 1
        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "Pull request created and submitted" in comment_text


class TestPRStatusCommentPosting:
    """Test PR status comment posting logic."""

    @pytest.mark.asyncio
    async def test_status_comment_posted_with_pr_number_present(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(ticket_key="TEST-200", current_pr_number=999)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args[0]
        assert call_args[0] == "TEST-200"
        assert "#999" in call_args[1]

    @pytest.mark.asyncio
    async def test_status_comment_posted_with_pr_number_absent(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(ticket_key="TEST-201", current_pr_number=None)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args[0]
        assert call_args[0] == "TEST-201"
        assert "#" not in call_args[1]

    @pytest.mark.asyncio
    async def test_status_comment_not_posted_on_reentry(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(
            ticket_key="TEST-202",
            current_pr_number=123,
            ci_status="pending",
        )

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        mock_jira.add_comment.assert_not_called()


class TestLabelRemoval:
    """Test forge:implementing label removal logic."""

    @pytest.mark.asyncio
    async def test_label_removal_success(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(ticket_key="TEST-300", current_pr_number=100)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        mock_jira.remove_labels.assert_called_once_with(
            "TEST-300",
            ["forge:implementing"],
        )
        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"

    @pytest.mark.asyncio
    async def test_label_removal_api_error_suppressed(self, caplog):
        mock_jira = create_mock_jira_client()
        mock_jira.remove_labels.side_effect = Exception("Jira API timeout")
        state = _initial_state(ticket_key="TEST-302", current_pr_number=102)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        assert any(
            "Failed to remove implementing label" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    @pytest.mark.asyncio
    async def test_label_removal_not_called_on_reentry(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(
            ticket_key="TEST-303",
            current_pr_number=103,
            ci_status="pending",
        )

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        mock_jira.remove_labels.assert_not_called()


class TestLabelAddition:
    """Test forge:ci-pending label addition logic."""

    @pytest.mark.asyncio
    async def test_label_addition_success(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(ticket_key="TEST-400", current_pr_number=200)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        from forge.models.workflow import ForgeLabel

        mock_jira.set_workflow_label.assert_called_once_with(
            "TEST-400",
            ForgeLabel.TASK_CI_PENDING,
        )
        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"

    @pytest.mark.asyncio
    async def test_label_addition_api_error_suppressed(self, caplog):
        mock_jira = create_mock_jira_client()
        mock_jira.set_workflow_label.side_effect = Exception("Jira API connection error")
        state = _initial_state(ticket_key="TEST-401", current_pr_number=201)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        assert any(
            "Failed to set ci-pending label" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    @pytest.mark.asyncio
    async def test_label_addition_not_called_on_reentry(self):
        mock_jira = create_mock_jira_client()
        state = _initial_state(
            ticket_key="TEST-402",
            current_pr_number=202,
            ci_status="passed",
        )

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        mock_jira.set_workflow_label.assert_not_called()


class TestErrorSuppressionAndLogging:
    """Test error suppression and logging for all label operations."""

    @pytest.mark.asyncio
    async def test_comment_posting_error_logged_and_suppressed(self, caplog):
        mock_jira = create_mock_jira_client()
        mock_jira.add_comment.side_effect = Exception("Comment API error")
        state = _initial_state(ticket_key="TEST-500", current_pr_number=300)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        assert any(
            "Failed to post status comment" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    @pytest.mark.asyncio
    async def test_label_removal_error_logged_and_suppressed(self, caplog):
        mock_jira = create_mock_jira_client()
        mock_jira.remove_labels.side_effect = Exception("Remove label API error")
        state = _initial_state(ticket_key="TEST-501", current_pr_number=301)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert any(
            "Failed to remove implementing label" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    @pytest.mark.asyncio
    async def test_label_addition_error_logged_and_suppressed(self, caplog):
        mock_jira = create_mock_jira_client()
        mock_jira.set_workflow_label.side_effect = Exception("Add label API error")
        state = _initial_state(ticket_key="TEST-502", current_pr_number=302)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert any(
            "Failed to set ci-pending label" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    @pytest.mark.asyncio
    async def test_all_operations_fail_workflow_still_continues(self, caplog):
        mock_jira = create_mock_jira_client()
        mock_jira.add_comment.side_effect = Exception("Comment failed")
        mock_jira.remove_labels.side_effect = Exception("Remove failed")
        mock_jira.set_workflow_label.side_effect = Exception("Add failed")
        state = _initial_state(ticket_key="TEST-503", current_pr_number=303)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("Failed to post status comment" in m for m in warning_messages)
        assert any("Failed to remove implementing label" in m for m in warning_messages)
        assert any("Failed to set ci-pending label" in m for m in warning_messages)


class TestWorkflowContinuation:
    """Test that workflow continues after comment/label failures."""

    @pytest.mark.asyncio
    async def test_workflow_continues_after_comment_failure(self):
        mock_jira = create_mock_jira_client()
        mock_jira.add_comment.side_effect = Exception("Comment API down")
        state = _initial_state(ticket_key="TEST-600", current_pr_number=400)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        assert result["ticket_key"] == "TEST-600"

    @pytest.mark.asyncio
    async def test_workflow_continues_after_label_failures(self):
        mock_jira = create_mock_jira_client()
        mock_jira.remove_labels.side_effect = Exception("Cannot remove")
        mock_jira.set_workflow_label.side_effect = Exception("Cannot add")
        state = _initial_state(ticket_key="TEST-601", current_pr_number=401)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        mock_jira.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_jira_client_closed_even_after_failures(self):
        mock_jira = create_mock_jira_client()
        mock_jira.add_comment.side_effect = Exception("Comment failed")
        mock_jira.remove_labels.side_effect = Exception("Remove failed")
        mock_jira.set_workflow_label.side_effect = Exception("Add failed")
        state = _initial_state(ticket_key="TEST-602", current_pr_number=402)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            await human_review_gate(state)

        mock_jira.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_workflow_continues_with_mixed_success_and_failure(self):
        mock_jira = create_mock_jira_client()
        mock_jira.remove_labels.side_effect = Exception("Remove failed")
        state = _initial_state(ticket_key="TEST-603", current_pr_number=403)

        with patch("forge.workflow.nodes.human_review.JiraClient", return_value=mock_jira):
            result = await human_review_gate(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        mock_jira.add_comment.assert_called_once()
        mock_jira.set_workflow_label.assert_called_once()
