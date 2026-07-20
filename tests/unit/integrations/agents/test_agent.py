"""Unit tests for ForgeAgent."""

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from forge.integrations.agents.agent import ForgeAgent


@pytest.mark.asyncio
async def test_answer_question():
    """ForgeAgent can answer questions about artifacts."""
    agent = ForgeAgent()

    with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run_task:
        mock_run_task.return_value = "Because of performance"

        answer = await agent.answer_question(
            question="Why REST?",
            artifact_content="# PRD\n\nWe use REST",
            context={
                "artifact_type": "prd",
                "generation_context": {"raw_requirements": "Build API"},
            },
        )

    assert "performance" in answer
    mock_run_task.assert_called_once()
    call_kwargs = mock_run_task.call_args
    assert call_kwargs.kwargs["task"] == "answer-question"

    await agent.close()


@pytest.mark.asyncio
async def test_answer_question_includes_ticket_context_and_description_fallback():
    """Generic Q&A includes ticket details when generation context is unavailable."""
    agent = ForgeAgent()

    with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run_task:
        mock_run_task.return_value = "The failure is reproducible."

        await agent.answer_question(
            question="Can this be reproduced in unit tests?",
            artifact_content="# RCA\n\nThe parser mishandles empty input.",
            context={
                "artifact_type": "rca",
                "ticket_key": "BUG-123",
                "summary": "Parser fails on empty input",
                "description": "Calling parse with an empty payload raises ValueError.",
                "generation_context": {},
            },
        )

    prompt = mock_run_task.call_args.kwargs["prompt"]
    assert "Parser fails on empty input" in prompt
    assert "Calling parse with an empty payload raises ValueError." in prompt
    assert "original requirements were:\nCalling parse with an empty payload" in prompt
    assert "Not available" not in prompt

    await agent.close()


@pytest.mark.asyncio
async def test_answer_question_prefers_generation_raw_requirements():
    """Artifact generation requirements take precedence over the ticket description."""
    agent = ForgeAgent()

    with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run_task:
        mock_run_task.return_value = "The answer"

        await agent.answer_question(
            question="Why REST?",
            artifact_content="# PRD\n\nUse REST.",
            context={
                "artifact_type": "prd",
                "description": "Current ticket description",
                "generation_context": {"raw_requirements": "Original requirements"},
            },
        )

    prompt = mock_run_task.call_args.kwargs["prompt"]
    assert "original requirements were:\nOriginal requirements" in prompt

    await agent.close()


@pytest.mark.asyncio
async def test_answer_question_default_artifact_type():
    """ForgeAgent uses default artifact type when not provided."""
    agent = ForgeAgent()

    with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run_task:
        mock_run_task.return_value = "The answer"

        await agent.answer_question(
            question="What is this?",
            artifact_content="Some content",
            context={},  # No artifact_type provided
        )

    call_kwargs = mock_run_task.call_args
    # The prompt should use "document" as default artifact type
    assert call_kwargs.kwargs["context"]["artifact_type"] == "document"

    await agent.close()


@pytest.mark.asyncio
async def test_answer_question_empty_response():
    """ForgeAgent handles empty response gracefully."""
    agent = ForgeAgent()

    with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run_task:
        mock_run_task.return_value = ""

        answer = await agent.answer_question(
            question="Test?",
            artifact_content="Content",
            context={"artifact_type": "spec"},
        )

    assert answer == ""

    await agent.close()


def test_get_skill_paths_uses_resolver_when_ticket_key_given():
    """When ticket_key is provided, resolver is called and result returned."""
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = MagicMock()

    with patch("forge.integrations.agents.agent.resolve_skill_paths") as mock_resolver:
        mock_resolver.return_value = ["skills/default/", "skills/proj/"]
        result = agent._get_skill_paths("PROJ-123")

    mock_resolver.assert_called_once()
    assert result == ["skills/default/", "skills/proj/"]


def test_get_skill_paths_returns_default_without_ticket_key():
    """When ticket_key is None, resolver returns skills/default/ only."""
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = MagicMock()
    agent.settings.skills_dir = "skills/"

    with patch("forge.integrations.agents.agent.resolve_skill_paths") as mock_resolver:
        mock_resolver.return_value = ["skills/default/"]
        result = agent._get_skill_paths(None)

    mock_resolver.assert_called_once_with("", ANY)
    assert result == ["skills/default/"]
