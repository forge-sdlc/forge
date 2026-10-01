from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from pydantic import BaseModel, ConfigDict

from forge.integrations.agents.agent import ForgeAgent
from forge.integrations.agents.structured_outputs import ArtifactDocument, TaskGeneration
from forge.integrations.agents.vertex_anthropic import VertexResponseStopError


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


def _vertex_error(status: int, message: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://aiplatform.googleapis.com/mock")
    response = httpx.Response(status, request=request)
    error_type = "invalid_request_error" if status == 400 else "permission_error"
    error_class = {
        400: anthropic.BadRequestError,
        403: anthropic.PermissionDeniedError,
        429: anthropic.RateLimitError,
        503: anthropic.InternalServerError,
    }[status]
    return error_class(
        message,
        response=response,
        body={"error": {"type": error_type, "message": message}},
    )


def _vertex_agent() -> ForgeAgent:
    forge = ForgeAgent.__new__(ForgeAgent)
    forge.settings = MagicMock(llm_backend="vertex-ai", llm_model="claude-opus-4-8")
    return forge


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TypeError("unexpected keyword argument 'output_config'"),
        ValueError("Conflicting response_format and output_config.format"),
        _vertex_error(400, "schema is too complex for compilation"),
        _vertex_error(400, "invalid schema"),
        _vertex_error(403, "structured_outputs is disabled by project policy"),
        ValueError("invalid JSON in provider response"),
        VertexResponseStopError("max_tokens"),
        VertexResponseStopError("refusal"),
    ],
)
async def test_vertex_native_failure_does_not_downgrade_or_retry(error: Exception) -> None:
    forge = _vertex_agent()
    native = AsyncMock()
    native.ainvoke.side_effect = error
    with (
        patch.object(forge, "_create_agent_async", return_value=native) as create,
        patch("forge.integrations.agents.agent.asyncio.sleep", new_callable=AsyncMock) as sleep,
        pytest.raises(type(error)),
    ):
        await forge._run_agent("prompt", "system", response_schema=Decision)

    create.assert_awaited_once()
    native.ainvoke.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_vertex_native_fallback_requires_explicit_unsupported_feature_error() -> None:
    forge = _vertex_agent()
    native = AsyncMock()
    native.ainvoke.side_effect = _vertex_error(
        400, "output_config.format is not supported for this model"
    )
    fallback = AsyncMock()
    fallback.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": True, "reason": "valid"},
    }
    with patch.object(forge, "_create_agent_async", side_effect=[native, fallback]) as create:
        result = await forge._run_agent("prompt", "system", response_schema=Decision)

    assert result == Decision(accepted=True, reason="valid")
    assert isinstance(create.call_args_list[1].kwargs["response_format"], ToolStrategy)
    assert create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _vertex_error(429, "Rate limit exceeded"),
        _vertex_error(503, "Service unavailable"),
        anthropic.APITimeoutError(httpx.Request("POST", "https://aiplatform.googleapis.com/mock")),
    ],
)
async def test_vertex_transient_failure_retries_same_native_strategy(error: Exception) -> None:
    forge = _vertex_agent()
    native = AsyncMock()
    native.ainvoke.side_effect = [
        error,
        {"messages": [], "structured_response": {"accepted": True, "reason": "valid"}},
    ]
    with (
        patch.object(forge, "_create_agent_async", return_value=native) as create,
        patch("forge.integrations.agents.agent.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        result = await forge._run_agent("prompt", "system", response_schema=Decision)

    assert result == Decision(accepted=True, reason="valid")
    create.assert_awaited_once()
    assert native.ainvoke.await_count == 2
    sleep.assert_awaited_once()


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
