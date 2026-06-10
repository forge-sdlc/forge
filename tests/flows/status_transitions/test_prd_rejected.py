"""Tests for PRD rejection and revision cycles."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.gates import route_prd_approval
from forge.workflow.nodes import regenerate_prd_with_feedback
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state


class TestPrdRejectedOnce:
    """Tests for single PRD rejection cycle."""

    @pytest.fixture
    def prd_pending_state(self):
        """State with PRD pending approval."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "prd_approval_gate"
        state["is_paused"] = True
        state["prd_content"] = """# Product Requirements Document

## Overview
Initial PRD content without user personas.

## Goals
- Enable user login
- Secure authentication
"""
        return state

    def test_rejection_with_feedback_routes_to_regenerate(self, prd_pending_state):
        """PRD rejection with feedback routes to regenerate_prd."""
        prd_pending_state["context"] = {
            "labels": ["forge:managed", "forge:prd-pending"],
        }
        prd_pending_state["feedback_comment"] = "Please add user persona section."
        prd_pending_state["revision_requested"] = True

        result = route_prd_approval(prd_pending_state)

        assert result == "regenerate_prd"

    @pytest.mark.asyncio
    async def test_regeneration_incorporates_feedback(self, prd_pending_state):
        """Regenerated PRD incorporates the feedback."""
        prd_pending_state["feedback_comment"] = "Please add user persona section."

        mock_jira = MagicMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.regenerate_with_feedback = AsyncMock(
            return_value="""# Product Requirements Document

## Overview
Revised PRD with user personas.

## User Personas
- Admin: Manages user accounts
- End User: Logs in to access features

## Goals
- Enable user login
- Secure authentication
"""
        )
        mock_agent.close = AsyncMock()

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await regenerate_prd_with_feedback(prd_pending_state)

        # Verify agent was called with feedback
        mock_agent.regenerate_with_feedback.assert_called_once()
        call_kwargs = mock_agent.regenerate_with_feedback.call_args.kwargs
        assert "user persona" in call_kwargs["feedback"].lower()

        # Verify new content
        assert "User Personas" in result["prd_content"]

    @pytest.mark.asyncio
    async def test_after_regeneration_returns_to_pending(self, prd_pending_state):
        """After regeneration, state returns to PRD approval gate."""
        prd_pending_state["feedback_comment"] = "Add more detail."

        mock_jira = MagicMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.regenerate_with_feedback = AsyncMock(return_value="# Revised PRD")
        mock_agent.close = AsyncMock()

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await regenerate_prd_with_feedback(prd_pending_state)

        assert result["current_node"] == "prd_approval_gate"
        assert result["feedback_comment"] is None


class TestPrdRejectedMultiple:
    """Tests for multiple PRD rejection cycles."""

    @pytest.fixture
    def prd_state_first_revision(self):
        """State after first revision."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "prd_approval_gate"
        state["is_paused"] = True
        state["retry_count"] = 1  # Already revised once
        state["prd_content"] = "# PRD - Revision 1"
        return state

    def test_second_rejection_routes_to_regenerate(self, prd_state_first_revision):
        """Second rejection also routes to regenerate."""
        prd_state_first_revision["context"] = {
            "labels": ["forge:managed", "forge:prd-pending"],
        }
        prd_state_first_revision["feedback_comment"] = "Still missing success metrics."
        prd_state_first_revision["revision_requested"] = True

        result = route_prd_approval(prd_state_first_revision)

        assert result == "regenerate_prd"

    def test_third_rejection_routes_to_regenerate(self, prd_state_first_revision):
        """Third rejection also routes to regenerate."""
        prd_state_first_revision["retry_count"] = 2  # Already revised twice
        prd_state_first_revision["context"] = {
            "labels": ["forge:managed", "forge:prd-pending"],
        }
        prd_state_first_revision["feedback_comment"] = "Final adjustments needed."
        prd_state_first_revision["revision_requested"] = True

        result = route_prd_approval(prd_state_first_revision)

        assert result == "regenerate_prd"

    @pytest.mark.asyncio
    async def test_revision_count_increments(self, prd_state_first_revision):
        """Revision count increments with each rejection."""
        prd_state_first_revision["feedback_comment"] = "Need changes."
        initial_count = prd_state_first_revision["retry_count"]

        mock_jira = MagicMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        # Simulate error to increment retry count
        mock_agent.regenerate_with_feedback = AsyncMock(
            side_effect=Exception("Simulated error")
        )
        mock_agent.close = AsyncMock()

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                result = await regenerate_prd_with_feedback(prd_state_first_revision)

        # Error case increments retry count
        assert result["retry_count"] == initial_count + 1


class TestPrdRevisionPreservesContext:
    """Tests for context preservation during PRD revision."""

    @pytest.fixture
    def prd_with_context(self):
        """PRD state with rich context."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# Original PRD"
        state["context"] = {
            "project_key": "TEST",
            "summary": "User Authentication Feature",
            "labels": ["forge:managed", "forge:prd-pending"],
        }
        state["feedback_comment"] = "Add security requirements."
        return state

    @pytest.mark.asyncio
    async def test_regeneration_uses_original_prd(self, prd_with_context):
        """Regeneration passes original PRD to agent."""
        mock_jira = MagicMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.regenerate_with_feedback = AsyncMock(return_value="# Revised")
        mock_agent.close = AsyncMock()

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                await regenerate_prd_with_feedback(prd_with_context)

        call_kwargs = mock_agent.regenerate_with_feedback.call_args.kwargs
        assert call_kwargs["original_content"] == "# Original PRD"
        assert call_kwargs["content_type"] == "prd"

    @pytest.mark.asyncio
    async def test_feedback_is_passed_to_agent(self, prd_with_context):
        """Feedback comment is passed to agent."""
        mock_jira = MagicMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.regenerate_with_feedback = AsyncMock(return_value="# Revised")
        mock_agent.close = AsyncMock()

        with patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.prd_generation.ForgeAgent", return_value=mock_agent):
                await regenerate_prd_with_feedback(prd_with_context)

        call_kwargs = mock_agent.regenerate_with_feedback.call_args.kwargs
        assert "security" in call_kwargs["feedback"].lower()
