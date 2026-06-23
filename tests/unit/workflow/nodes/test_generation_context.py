"""Tests for generation context storage in PRD and Spec generation nodes."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.update_description = AsyncMock()
    mock.add_structured_comment = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.get_prd_proposals_repo = AsyncMock(return_value=None)
    mock.get_proposals_path = AsyncMock(return_value=None)
    mock.get_issue = AsyncMock(
        return_value=MagicMock(
            summary="Test Feature",
            description="Test description",
            project_key="TEST",
        )
    )
    return mock


def create_mock_forge_agent():
    """Create a mock ForgeAgent with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    return mock


class TestPRDGenerationContext:
    """Tests for PRD generation context storage."""

    @pytest.mark.asyncio
    async def test_generate_prd_stores_generation_context(self):
        """generate_prd stores generation_context['prd'] with raw_requirements."""
        from forge.workflow.nodes.prd_generation import generate_prd

        # Setup mocks
        mock_jira = create_mock_jira_client()
        mock_jira.get_issue = AsyncMock(
            return_value=MagicMock(
                summary="Test Feature",
                description="Raw requirements text",
                project_key="TEST",
            )
        )

        mock_agent = create_mock_forge_agent()
        mock_agent.generate_prd = AsyncMock(
            return_value="# Generated PRD\n\nContent here."
        )

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )

        with (
            patch(
                "forge.workflow.nodes.prd_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.prd_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await generate_prd(state)

        # Verify generation_context was stored
        assert "generation_context" in result
        assert "prd" in result["generation_context"]

        prd_context = result["generation_context"]["prd"]
        assert prd_context["raw_requirements"] == "Raw requirements text"
        assert prd_context["summary"] == "Test Feature"
        assert "generated_at" in prd_context

        # Verify generated_at is a valid ISO timestamp
        generated_at = datetime.fromisoformat(prd_context["generated_at"])
        assert generated_at.tzinfo is not None  # Should be timezone-aware

    @pytest.mark.asyncio
    async def test_generate_prd_preserves_existing_context(self):
        """generate_prd preserves existing generation_context entries."""
        from forge.workflow.nodes.prd_generation import generate_prd

        mock_jira = create_mock_jira_client()
        mock_jira.get_issue = AsyncMock(
            return_value=MagicMock(
                summary="Test Feature",
                description="Raw requirements",
                project_key="TEST",
            )
        )

        mock_agent = create_mock_forge_agent()
        mock_agent.generate_prd = AsyncMock(
            return_value="# PRD Content"
        )

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        # Pre-populate with existing context
        state["generation_context"] = {"existing_key": "existing_value"}

        with (
            patch(
                "forge.workflow.nodes.prd_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.prd_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await generate_prd(state)

        # Verify both old and new context exist
        assert result["generation_context"]["existing_key"] == "existing_value"
        assert "prd" in result["generation_context"]


class TestSpecGenerationContext:
    """Tests for Spec generation context storage."""

    @pytest.mark.asyncio
    async def test_generate_spec_stores_generation_context(self):
        """generate_spec stores generation_context['spec'] with prd_content."""
        from forge.workflow.nodes.spec_generation import generate_spec

        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()
        mock_agent.generate_spec = AsyncMock(
            return_value="# Generated Spec\n\nContent here."
        )

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD\n\nPRD content used for spec."

        with (
            patch(
                "forge.workflow.nodes.spec_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.spec_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await generate_spec(state)

        # Verify generation_context was stored
        assert "generation_context" in result
        assert "spec" in result["generation_context"]

        spec_context = result["generation_context"]["spec"]
        assert spec_context["prd_content"] == "# PRD\n\nPRD content used for spec."
        assert "generated_at" in spec_context

        # Verify generated_at is a valid ISO timestamp
        generated_at = datetime.fromisoformat(spec_context["generated_at"])
        assert generated_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_generate_spec_preserves_prd_context(self):
        """generate_spec preserves existing prd context in generation_context."""
        from forge.workflow.nodes.spec_generation import generate_spec

        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()
        mock_agent.generate_spec = AsyncMock(
            return_value="# Spec Content"
        )

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD Content"
        # Pre-populate with PRD context (as would happen in real workflow)
        state["generation_context"] = {
            "prd": {
                "raw_requirements": "Original requirements",
                "summary": "Test Feature",
                "generated_at": "2024-01-01T00:00:00+00:00",
            }
        }

        with (
            patch(
                "forge.workflow.nodes.spec_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.spec_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await generate_spec(state)

        # Verify both prd and spec context exist
        assert "prd" in result["generation_context"]
        assert result["generation_context"]["prd"]["raw_requirements"] == "Original requirements"
        assert "spec" in result["generation_context"]
        assert "prd_content" in result["generation_context"]["spec"]
