"""Unit tests for PRD generation node."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import ForgeLabel, TicketType
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state
from forge.workflow.nodes import generate_prd, regenerate_prd_with_feedback


class TestGeneratePrd:
    """Tests for generate_prd node."""

    @pytest.fixture
    def initial_state(self):
        """Create initial state for PRD generation."""
        return create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )

    @pytest.fixture
    def mock_jira(self):
        """Mock Jira client."""
        from forge.integrations.jira.models import JiraIssue

        mock = MagicMock()
        mock.get_issue = AsyncMock(
            return_value=JiraIssue(
                key="TEST-123",
                id="10001",
                summary="Test Feature",
                description="Raw requirements for the feature.",
                status="New",
                issue_type="Feature",
                labels=["forge:managed"],
            )
        )
        mock.update_description = AsyncMock()
        mock.set_workflow_label = AsyncMock()
        mock.add_structured_comment = AsyncMock()
        mock.close = AsyncMock()
        return mock

    @pytest.fixture
    def mock_agent(self):
        """Mock ForgeAgent."""
        mock = MagicMock()
        mock.generate_prd = AsyncMock(
            return_value="# PRD\n\n## Overview\nGenerated PRD content."
        )
        mock.close = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_generates_prd_from_description(self, initial_state, mock_jira, mock_agent):
        """PRD is generated from issue description."""
        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await generate_prd(initial_state)

        assert result["prd_content"] != ""
        assert "# PRD" in result["prd_content"]

    @pytest.mark.asyncio
    async def test_updates_current_node(self, initial_state, mock_jira, mock_agent):
        """Current node is updated after generation."""
        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await generate_prd(initial_state)

        assert result["current_node"] == "prd_approval_gate"

    @pytest.mark.asyncio
    async def test_sets_prd_pending_label(self, initial_state, mock_jira, mock_agent):
        """PRD pending label is set on Jira issue."""
        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                await generate_prd(initial_state)

        mock_jira.set_workflow_label.assert_called_once()
        call_args = mock_jira.set_workflow_label.call_args
        assert call_args[0][1] == ForgeLabel.PRD_PENDING

    @pytest.mark.asyncio
    async def test_clears_previous_error(self, initial_state, mock_jira, mock_agent):
        """Previous error is cleared on success."""
        initial_state["last_error"] = "Previous error"

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await generate_prd(initial_state)

        assert result["last_error"] is None

    @pytest.mark.asyncio
    async def test_handles_empty_description(self, initial_state, mock_jira, mock_agent):
        """Empty description results in error state."""
        from forge.integrations.jira.models import JiraIssue

        mock_jira.get_issue = AsyncMock(
            return_value=JiraIssue(
                key="TEST-123",
                id="10001",
                summary="Test Feature",
                description="",  # Empty description
                status="New",
                issue_type="Feature",
                labels=["forge:managed"],
            )
        )

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await generate_prd(initial_state)

        assert result["last_error"] is not None
        assert "requirements" in result["last_error"].lower()

    @pytest.mark.asyncio
    async def test_handles_agent_error(self, initial_state, mock_jira, mock_agent):
        """Agent error results in error state."""
        mock_agent.generate_prd = AsyncMock(side_effect=Exception("API error"))

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await generate_prd(initial_state)

        assert result["last_error"] is not None
        assert result["retry_count"] == 1


class TestRegeneratePrdWithFeedback:
    """Tests for regenerate_prd_with_feedback node."""

    @pytest.fixture
    def state_with_feedback(self):
        """State with PRD and feedback."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# Original PRD\n\nOriginal content."
        state["feedback_comment"] = "Please add more detail about the user persona."
        return state

    @pytest.fixture
    def mock_jira(self):
        """Mock Jira client."""
        mock = MagicMock()
        mock.update_description = AsyncMock()
        mock.add_structured_comment = AsyncMock()
        mock.add_comment = AsyncMock()
        mock.close = AsyncMock()
        return mock

    @pytest.fixture
    def mock_agent(self):
        """Mock ForgeAgent."""
        mock = MagicMock()
        mock.regenerate_with_feedback = AsyncMock(
            return_value="# Revised PRD\n\n## User Persona\nDetailed user persona."
        )
        mock.close = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_regenerates_with_feedback(self, state_with_feedback, mock_jira, mock_agent):
        """PRD is regenerated incorporating feedback."""
        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                await regenerate_prd_with_feedback(state_with_feedback)

        mock_agent.regenerate_with_feedback.assert_called_once()
        call_args = mock_agent.regenerate_with_feedback.call_args
        assert "user persona" in call_args.kwargs["feedback"].lower()

    @pytest.mark.asyncio
    async def test_clears_feedback_after_regeneration(self, state_with_feedback, mock_jira, mock_agent):
        """Feedback is cleared after regeneration."""
        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await regenerate_prd_with_feedback(state_with_feedback)

        assert result["feedback_comment"] is None
        assert result["revision_requested"] is False

    @pytest.mark.asyncio
    async def test_returns_to_approval_gate(self, state_with_feedback, mock_jira, mock_agent):
        """Node returns to PRD approval gate."""
        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await regenerate_prd_with_feedback(state_with_feedback)

        assert result["current_node"] == "prd_approval_gate"

    @pytest.mark.asyncio
    async def test_stores_in_comment_when_configured(self, state_with_feedback, mock_jira, mock_agent):
        """Regenerated PRD is stored as structured comment when jira_store_in_comments is true."""
        mock_settings = MagicMock()
        mock_settings.jira_store_in_comments = True

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                with patch("forge.workflow.nodes.prd_generation.get_settings", return_value=mock_settings):
                    await regenerate_prd_with_feedback(state_with_feedback)

        mock_jira.add_structured_comment.assert_called_once_with(
            "TEST-123",
            "Product Requirements Document (PRD)",
            "# Revised PRD\n\n## User Persona\nDetailed user persona.",
            comment_type="prd",
        )
        mock_jira.update_description.assert_not_called()

    @pytest.mark.asyncio
    async def test_stores_in_description_when_configured(self, state_with_feedback, mock_jira, mock_agent):
        """Regenerated PRD updates description when jira_store_in_comments is false."""
        mock_settings = MagicMock()
        mock_settings.jira_store_in_comments = False

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                with patch("forge.workflow.nodes.prd_generation.get_settings", return_value=mock_settings):
                    await regenerate_prd_with_feedback(state_with_feedback)

        mock_jira.update_description.assert_called_once_with(
            "TEST-123", "# Revised PRD\n\n## User Persona\nDetailed user persona."
        )
        mock_jira.add_structured_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_feedback_returns_unchanged(self, mock_jira, mock_agent):
        """Missing feedback returns state unchanged."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# Original PRD"
        # No feedback_comment set

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                await regenerate_prd_with_feedback(state)

        # Agent should not be called
        mock_agent.regenerate_with_feedback.assert_not_called()
