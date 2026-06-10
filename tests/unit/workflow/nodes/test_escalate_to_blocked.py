"""Unit tests for escalate_to_blocked node."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.fixtures.workflow_states import make_workflow_state


@pytest.fixture
def state_at_ci():
    return make_workflow_state(
        current_node="ci_evaluator",
        ci_fix_attempts=5,
        ci_failed_checks=[{"name": "tests", "conclusion": "failure"}],
        last_error="CI exhausted",
    )


@pytest.fixture
def state_at_workspace():
    return make_workflow_state(
        current_node="setup_workspace",
        last_error="Failed to clone repository",
    )


@pytest.fixture
def mock_jira():
    jira = MagicMock()
    jira.set_workflow_label = AsyncMock()
    jira.add_comment = AsyncMock()
    jira.get_issue = AsyncMock(return_value=MagicMock(
        reporter="reporter@example.com",
        assignee="assignee@example.com",
    ))
    jira.close = AsyncMock()
    return jira


class TestEscalateToBlockedSetsIsBlocked:
    """escalate_to_blocked must set is_blocked=True."""

    @pytest.mark.asyncio
    async def test_sets_is_blocked_true(self, state_at_ci, mock_jira):
        """Result state has is_blocked=True."""
        from forge.workflow.nodes.ci_evaluator import escalate_to_blocked

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.error_handler.notify_error", AsyncMock()):
            result = await escalate_to_blocked(state_at_ci)

        assert result.get("is_blocked") is True

    @pytest.mark.asyncio
    async def test_sets_is_blocked_from_workspace_failure(self, state_at_workspace, mock_jira):
        """is_blocked=True regardless of which node triggered escalation."""
        from forge.workflow.nodes.ci_evaluator import escalate_to_blocked

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.error_handler.notify_error", AsyncMock()):
            result = await escalate_to_blocked(state_at_workspace)

        assert result.get("is_blocked") is True


class TestEscalateToBlockedPreservesCurrentNode:
    """escalate_to_blocked must NOT overwrite current_node with 'complete'."""

    @pytest.mark.asyncio
    async def test_preserves_current_node_at_ci(self, state_at_ci, mock_jira):
        """current_node stays 'ci_evaluator' after CI exhaustion escalation."""
        from forge.workflow.nodes.ci_evaluator import escalate_to_blocked

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.error_handler.notify_error", AsyncMock()):
            result = await escalate_to_blocked(state_at_ci)

        assert result["current_node"] == "ci_evaluator"

    @pytest.mark.asyncio
    async def test_preserves_current_node_at_workspace(self, state_at_workspace, mock_jira):
        """current_node stays 'setup_workspace' after workspace failure."""
        from forge.workflow.nodes.ci_evaluator import escalate_to_blocked

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.error_handler.notify_error", AsyncMock()):
            result = await escalate_to_blocked(state_at_workspace)

        assert result["current_node"] == "setup_workspace"

    @pytest.mark.asyncio
    async def test_does_not_set_current_node_to_complete(self, state_at_ci, mock_jira):
        """current_node must never be set to 'complete' by escalation."""
        from forge.workflow.nodes.ci_evaluator import escalate_to_blocked

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.error_handler.notify_error", AsyncMock()):
            result = await escalate_to_blocked(state_at_ci)

        assert result["current_node"] != "complete"


class TestEscalateToBlockedExistingBehaviourUnchanged:
    """Existing Jira side-effects must still occur."""

    @pytest.mark.asyncio
    async def test_sets_blocked_jira_label(self, state_at_ci, mock_jira):
        """forge:blocked label is still applied to the Jira ticket."""
        from forge.models.workflow import ForgeLabel
        from forge.workflow.nodes.ci_evaluator import escalate_to_blocked

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.error_handler.notify_error", AsyncMock()):
            await escalate_to_blocked(state_at_ci)

        mock_jira.set_workflow_label.assert_called_once_with(
            state_at_ci["ticket_key"], ForgeLabel.BLOCKED
        )

    @pytest.mark.asyncio
    async def test_sets_ci_status_to_blocked(self, state_at_ci, mock_jira):
        """ci_status is set to 'blocked' in the returned state."""
        from forge.workflow.nodes.ci_evaluator import escalate_to_blocked

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.error_handler.notify_error", AsyncMock()):
            result = await escalate_to_blocked(state_at_ci)

        assert result.get("ci_status") == "blocked"
