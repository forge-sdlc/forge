"""Unit tests for ForgeAgent."""

import json
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import SimpleChatModel
from langchain_core.messages import BaseMessage

from forge.integrations.agents.agent import ForgeAgent


class MockChatModel(SimpleChatModel):
    response: str

    def _call(
        self,
        _messages: list[BaseMessage],
        _stop: list[str] | None = None,
        _run_manager: CallbackManagerForLLMRun | None = None,
        **_kwargs: Any,
    ) -> str:
        return self.response

    async def _acall(
        self,
        _messages: list[BaseMessage],
        _stop: list[str] | None = None,
        _run_manager: CallbackManagerForLLMRun | None = None,
        **_kwargs: Any,
    ) -> str:
        return self.response

    @property
    def _llm_type(self) -> str:
        return "mock"

def _model_agent(backend: str, model: str) -> ForgeAgent:
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = MagicMock(
        llm_backend=backend,
        llm_model=model,
        llm_max_tokens=16384,
        google_cloud_project="project",
        google_cloud_location="global",
    )
    agent.settings.google_api_key.get_secret_value.return_value = "google-key"
    agent.settings.anthropic_api_key.get_secret_value.return_value = "anthropic-key"
    return agent


def test_create_model_uses_google_genai_backend():
    agent = _model_agent("google-genai", "gemini-3.5-flash")

    with patch("forge.integrations.agents.agent.ChatGoogleGenerativeAI") as model_class:
        agent._create_model()

    model_class.assert_called_once_with(
        model="gemini-3.5-flash",
        api_key="google-key",
        max_output_tokens=16384,
    )


def test_create_model_uses_vertex_backend_for_gemini():
    agent = _model_agent("vertex-ai", "gemini-3.5-flash")

    with patch("forge.integrations.agents.agent.ChatGoogleGenerativeAI") as model_class:
        agent._create_model()

    model_class.assert_called_once_with(
        model="gemini-3.5-flash",
        project="project",
        location="global",
        vertexai=True,
        max_output_tokens=16384,
    )


def test_create_model_uses_vertex_backend_for_anthropic():
    agent = _model_agent("vertex-ai", "claude-sonnet-4-6")

    with patch("forge.integrations.agents.agent.ChatAnthropicVertex") as model_class:
        agent._create_model()

    model_class.assert_called_once_with(
        model_name="claude-sonnet-4-6",
        project="project",
        location="global",
        max_tokens=16384,
    )


def test_create_model_uses_anthropic_backend():
    agent = _model_agent("anthropic", "claude-sonnet-4-6")

    with patch("forge.integrations.agents.agent.ChatAnthropic") as model_class:
        agent._create_model()

    model_class.assert_called_once_with(
        model="claude-sonnet-4-6",
        api_key="anthropic-key",
        max_tokens=16384,
    )


def test_create_model_rejects_backend_model_mismatch():
    agent = _model_agent("anthropic", "gemini-3.5-flash")

    with pytest.raises(ValueError, match="not supported by anthropic"):
        agent._create_model()


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

    mock_resolver.assert_called_once_with("", ANY, skills_install_dir=ANY)
    assert result == ["skills/default/"]


@pytest.mark.asyncio
async def test_revise_draft_with_feedback_success():
    """Verify that revise_draft_with_feedback properly renders prompt and parses valid JSON."""
    agent = ForgeAgent()

    mock_model = MockChatModel(response='{"parent_key": "PROJ-1", "items": [{"id": 1, "summary": "Task 1"}]}')

    with patch.object(agent, "_create_model", return_value=mock_model):
        result = await agent.revise_draft_with_feedback(
            draft_content='{"items": []}',
            feedback="Add Task 1",
            context={"ticket_key": "PROJ-1"}
        )

    assert json.loads(result) == {"parent_key": "PROJ-1", "items": [{"id": 1, "summary": "Task 1"}]}
    await agent.close()


@pytest.mark.asyncio
async def test_revise_draft_with_feedback_markdown_stripping():
    """Verify that revise_draft_with_feedback strips markdown block and preamble."""
    agent = ForgeAgent()

    llm_response = """
    Certainly! Here is the updated JSON:
    ```json
    {
      "items": [
        {"id": 1, "summary": "Task 1"}
      ]
    }
    ```
    Hope this helps!
    """
    mock_model = MockChatModel(response=llm_response)

    with patch.object(agent, "_create_model", return_value=mock_model):
        result = await agent.revise_draft_with_feedback(
            draft_content='{"items": []}',
            feedback="Add Task 1",
            context={"ticket_key": "PROJ-1"}
        )

    assert json.loads(result) == {"items": [{"id": 1, "summary": "Task 1"}]}
    await agent.close()


@pytest.mark.asyncio
async def test_revise_draft_with_feedback_preamble_no_codeblock():
    """Verify that revise_draft_with_feedback strips preamble and postamble without markdown code block."""
    agent = ForgeAgent()

    llm_response = 'The corrected draft is: {"items": [{"id": 1, "summary": "Task 1"}]} please review.'
    mock_model = MockChatModel(response=llm_response)

    with patch.object(agent, "_create_model", return_value=mock_model):
        result = await agent.revise_draft_with_feedback(
            draft_content='{"items": []}',
            feedback="Add Task 1",
            context={"ticket_key": "PROJ-1"}
        )

    assert json.loads(result) == {"items": [{"id": 1, "summary": "Task 1"}]}
    await agent.close()


@pytest.mark.asyncio
async def test_revise_draft_with_feedback_invalid_json():
    """Verify that revise_draft_with_feedback raises ValueError on invalid JSON output."""
    agent = ForgeAgent()

    mock_model = MockChatModel(response="This is not JSON at all.")

    with (
        patch.object(agent, "_create_model", return_value=mock_model),
        pytest.raises(ValueError, match="Failed to parse revised draft as JSON"),
    ):
        await agent.revise_draft_with_feedback(
            draft_content='{"items": []}',
            feedback="Add Task 1",
            context={"ticket_key": "PROJ-1"}
        )

    await agent.close()


@pytest.mark.asyncio
async def test_revise_draft_with_feedback_prompt_formatting():
    """Verify that revise_draft_with_feedback properly renders the prompt with input variables."""
    agent = ForgeAgent()
    mock_model = MockChatModel(response='{"items": []}')

    with (
        patch("forge.integrations.agents.agent.load_prompt", return_value="FORMATTED PROMPT") as mock_load_prompt,
        patch.object(agent, "_create_model", return_value=mock_model),
    ):
        await agent.revise_draft_with_feedback(
            draft_content='{"some": "json"}',
            feedback="Do this",
            context={"ticket_key": "PROJ-123"}
        )

    mock_load_prompt.assert_called_once_with(
        "revision-draft",
        draft_content='{"some": "json"}',
        feedback="Do this",
        context=json.dumps({"ticket_key": "PROJ-123"}, indent=2),
    )
    await agent.close()


@pytest.mark.asyncio
async def test_revise_draft_with_feedback_fallback_matched_delimiters():
    """Verify that revise_draft_with_feedback correctly extracts matching boundaries when there is trailing mismatched punctuation."""
    agent = ForgeAgent()

    # Case 1: JSON Object starting with '{' but having a trailing ']' in the postamble
    llm_response_object = (
        'Here is the result: {"parent_key": "PROJ-1", "items": [{"id": 1}]} with an unmatched trailing bracket ]'
    )
    mock_model_object = MockChatModel(response=llm_response_object)

    with patch.object(agent, "_create_model", return_value=mock_model_object):
        result_object = await agent.revise_draft_with_feedback(
            draft_content='{"items": []}', feedback="Add Task 1", context={"ticket_key": "PROJ-1"}
        )
    assert json.loads(result_object) == {"parent_key": "PROJ-1", "items": [{"id": 1}]}

    # Case 2: JSON List starting with '[' but having a trailing '}' in the postamble
    llm_response_list = (
        'Here is the result: [{"id": 1}] with an unmatched trailing brace }'
    )
    mock_model_list = MockChatModel(response=llm_response_list)

    with patch.object(agent, "_create_model", return_value=mock_model_list):
        result_list = await agent.revise_draft_with_feedback(
            draft_content='{"items": []}', feedback="Add Task 1", context={"ticket_key": "PROJ-1"}
        )
    assert json.loads(result_list) == [{"id": 1}]

    await agent.close()
