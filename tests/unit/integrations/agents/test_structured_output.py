from unittest.mock import AsyncMock, patch

import pytest
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from pydantic import BaseModel, ConfigDict

from forge.integrations.agents.agent import ForgeAgent
from forge.integrations.agents.structured_outputs import ArtifactDocument, TaskGeneration


def test_artifact_document_requires_repository_names() -> None:
    with pytest.raises(ValueError, match="owner/repository"):
        ArtifactDocument(content="Document", repositories=["not-a-repo"])


def test_task_generation_requires_owner_repository_repo_field() -> None:
    with pytest.raises(ValueError, match="owner/repository"):
        TaskGeneration.model_validate(
            {
                "tasks": [
                    {
                        "summary": "Implement it",
                        "description": "Implement the change.",
                        "repo": "unknown",
                    }
                ]
            }
        )


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    reason: str


@pytest.mark.asyncio
async def test_tool_loop_returns_validated_structured_response() -> None:
    forge = ForgeAgent()
    deep_agent = AsyncMock()
    deep_agent.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": True, "reason": "valid"},
    }

    with patch.object(forge, "_create_agent_async", return_value=deep_agent) as create:
        result = await forge._run_agent("prompt", "system", response_schema=Decision)

    assert result == Decision(accepted=True, reason="valid")
    assert isinstance(create.call_args.kwargs["response_format"], ProviderStrategy)
    deep_agent.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_native_response_retries_with_validated_tool_strategy() -> None:
    forge = ForgeAgent()
    native = AsyncMock()
    native.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": "not-a-boolean", "unexpected": True},
    }
    fallback = AsyncMock()
    fallback.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": False, "reason": "rejected"},
    }

    with patch.object(forge, "_create_agent_async", side_effect=[native, fallback]) as create:
        result = await forge._run_agent("prompt", "system", response_schema=Decision)

    assert result == Decision(accepted=False, reason="rejected")
    assert isinstance(create.call_args_list[0].kwargs["response_format"], ProviderStrategy)
    assert isinstance(create.call_args_list[1].kwargs["response_format"], ToolStrategy)


@pytest.mark.asyncio
async def test_invalid_fallback_response_raises_actionable_validation_error() -> None:
    forge = ForgeAgent()
    native = AsyncMock()
    native.ainvoke.side_effect = ValueError("provider schema unsupported")
    fallback = AsyncMock()
    fallback.ainvoke.return_value = {"messages": [], "structured_response": {"accepted": True}}

    with (
        patch.object(forge, "_create_agent_async", side_effect=[native, fallback]),
        pytest.raises(ValueError, match="reason"),
    ):
        await forge._run_agent("prompt", "system", response_schema=Decision)
